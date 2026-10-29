# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Đường vận chuyển của ``kb_lookup``: local-first và cầu dao cho endpoint từ xa.

**Vì sao có file này.** Đo thật 29/10/2026: ``ARTEMIS_KB_API`` trỏ vào console trên VPS nhưng bản
console ở đó không có router ``/api/kb/*`` ⇒ **mọi** lần tra đều trả 404, và mỗi lần như vậy tốn một
vòng HTTP (~1 giây) *trước khi* rơi về KB local — trong khi hai đường đọc cùng một cơ sở dữ liệu.
Với KB theo công đoạn (ít nhất một lần tra mỗi công đoạn), độ trễ đó lặp lại suốt run.

Hai tính chất phải giữ, và đây là thứ các test dưới khoá lại:

1. **Local trước, không có vòng HTTP nào khi local trả lời được** — không gọi mạng thì không có độ
   trễ mạng.
2. **Vẫn dùng được remote khi nó hoạt động** — cầu dao chỉ *tạm* ngừng gọi, hết cooldown thì thử lại
   một lần, thành công là dùng lại bình thường (không phải bỏ remote vĩnh viễn).
"""

import json
import urllib.error

from artemis.knowledge.models import RetrievalResult, Workflow, WorkflowHit, WorkflowStep
from artemis.tools import knowledge as kb_tool


class FakeService:
    available = True
    degraded_reason = ""

    def __init__(self, result: RetrievalResult):
        self.result = result
        self.calls: list[dict] = []

    def retrieve(self, query, app_id, version_id=None, top_k=None, run_id=None, filters=None):
        self.calls.append({"query": query, "app_id": app_id})
        return self.result


class FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _workflow() -> Workflow:
    return Workflow(
        id="device_action.switch",
        app_id="hunonic",
        name="Chọn hành động công tắc",
        status="draft",
        metadata={"type": "device_action"},
        steps=[WorkflowStep(action="tap", target="Bật", expected_state="Nhà đổi")],
    )


def _local_hit() -> RetrievalResult:
    return RetrievalResult(
        found=True,
        query="",
        app_id="hunonic",
        hits=[WorkflowHit(workflow=_workflow(), score=1.0, channels={"filtered": 1})],
        filters={},
    )


def _local_miss() -> RetrievalResult:
    return RetrievalResult(
        found=False, query="", app_id="hunonic", hits=[], filters={}, reason="không"
    )


def _patch_local(monkeypatch, service) -> None:
    import artemis.knowledge.integration as integration
    import artemis.knowledge.service as service_module

    monkeypatch.setattr(service_module, "get_service", lambda: service)
    monkeypatch.setattr(integration, "resolve_app_id", lambda package, svc: "hunonic")


def _patch_health(monkeypatch, **kwargs):
    """Cầu dao riêng cho từng test (mặc định 900s/60s là quá dài để chờ trong test)."""
    health = kb_tool.RemoteHealth(**kwargs)
    monkeypatch.setattr(kb_tool, "remote_health", lambda: health)
    return health


def _counting_urlopen(monkeypatch, *, payload=None, code=0):
    """``urlopen`` giả có đếm số lần gọi; ``code`` khác 0 ⇒ ném ``HTTPError`` mã đó."""
    calls: list[str] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        if code:
            raise urllib.error.HTTPError(request.full_url, code, "boom", None, None)
        return FakeResponse(payload or {"found": True, "reason": "", "filters": {}, "hits": []})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


REMOTE = "https://kb.example/artemis"


# ── 1. local-first: không có vòng HTTP nào khi local trả lời được ────────
def test_a_local_answer_costs_no_network_round_trip(monkeypatch):
    """Đây chính là độ trễ được cắt: local trả lời ⇒ **không** gọi remote."""
    _patch_local(monkeypatch, FakeService(_local_hit()))
    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: REMOTE)
    calls = _counting_urlopen(monkeypatch)

    out = kb_tool.lookup_knowledge(workflow_id="device_action.switch")

    assert "device_action.switch" in out
    assert calls == []


def test_the_remote_endpoint_is_still_used_when_the_local_kb_is_unavailable(monkeypatch):
    """Máy tester không có DSN ⇒ remote vẫn là đường chính, không bị thay bằng gì khác."""
    service = FakeService(_local_miss())
    service.available = False
    service.degraded_reason = "Chưa cấu hình ARTEMIS_KB_DSN"
    _patch_local(monkeypatch, service)
    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: REMOTE)
    calls = _counting_urlopen(
        monkeypatch,
        payload={
            "found": True,
            "reason": "",
            "filters": {},
            "hits": [
                {
                    "workflow_id": "device_action.switch",
                    "name": "Chọn hành động công tắc",
                    "status": "draft",
                    "steps": [],
                }
            ],
        },
    )

    out = kb_tool.lookup_knowledge(workflow_id="device_action.switch")

    assert calls == [f"{REMOTE}/api/kb/retrieve"]
    assert "device_action.switch" in out


def test_local_first_can_be_switched_off(monkeypatch):
    """Có cách quay lại thứ tự cũ (remote trước) nếu console dùng cơ sở dữ liệu khác."""
    service = FakeService(_local_hit())
    _patch_local(monkeypatch, service)
    monkeypatch.setenv("ARTEMIS_KB_LOCAL_FIRST", "0")
    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: REMOTE)
    calls = _counting_urlopen(
        monkeypatch,
        payload={"found": False, "reason": "không có", "filters": {}, "hits": []},
    )

    out = kb_tool.lookup_knowledge(workflow_id="device_action.switch")

    assert calls  # remote được hỏi trước
    assert "không có hướng dẫn nào khớp" in out  # và câu trả lời của remote được tôn trọng
    assert service.calls == []  # local không bị hỏi thêm


# ── 2. cầu dao: ngắt tạm, không bỏ remote vĩnh viễn ─────────────────────
def test_a_404_opens_the_circuit_and_later_calls_skip_the_network(monkeypatch):
    """Một lần 404 là đủ để ngừng gọi lại trong cooldown — không trả giá vòng HTTP mọi lần tra."""
    service = FakeService(_local_miss())
    service.available = False
    _patch_local(monkeypatch, service)
    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: REMOTE)
    calls = _counting_urlopen(monkeypatch, code=404)
    health = _patch_health(monkeypatch, unavailable_cooldown=900.0)

    first = kb_tool.lookup_knowledge(workflow_id="device_action.switch")
    second = kb_tool.lookup_knowledge(workflow_id="device_action.selector")

    assert len(calls) == 1  # lần thứ hai KHÔNG chạm mạng
    assert health.snapshot()["open"] is True
    assert health.snapshot()["last_kind"] == "unavailable"
    assert "ngắt mạch" in second
    assert "trả mã 404" in first


def test_the_circuit_half_opens_after_the_cooldown_and_closes_on_success(monkeypatch):
    """Hết cooldown ⇒ đúng một lần thử lại; thành công ⇒ remote được dùng lại bình thường."""
    service = FakeService(_local_miss())
    service.available = False
    _patch_local(monkeypatch, service)
    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: REMOTE)
    health = _patch_health(monkeypatch, unavailable_cooldown=60.0)

    _counting_urlopen(monkeypatch, code=404)
    kb_tool.lookup_knowledge(workflow_id="device_action.switch")

    assert health.allows() is False  # mạch đang mở
    health.open_until -= 61.0  # tua qua cooldown (không chờ thật 60 giây)
    calls = _counting_urlopen(
        monkeypatch,
        payload={
            "found": True,
            "reason": "",
            "filters": {},
            "hits": [{"workflow_id": "device_action.switch", "name": "x", "status": "draft"}],
        },
    )

    out = kb_tool.lookup_knowledge(workflow_id="device_action.switch")

    assert len(calls) == 1  # đúng một lần thử lại
    assert "device_action.switch" in out
    assert health.snapshot()["open"] is False  # mạch đóng lại
    assert health.snapshot()["probes"] == 1


def test_a_single_transient_error_does_not_open_the_circuit(monkeypatch):
    """5xx một lần không được coi là endpoint chết — tránh bỏ remote vì một lần trục trặc."""
    service = FakeService(_local_miss())
    service.available = False
    _patch_local(monkeypatch, service)
    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: REMOTE)
    calls = _counting_urlopen(monkeypatch, code=500)
    health = _patch_health(monkeypatch, transient_threshold=2, transient_cooldown=30.0)

    kb_tool.lookup_knowledge(workflow_id="device_action.switch")
    assert health.allows() is True

    kb_tool.lookup_knowledge(workflow_id="device_action.switch")

    assert len(calls) == 2  # cả hai lần đều thử
    assert health.snapshot()["open"] is True
    assert health.snapshot()["last_kind"] == "transient"


def test_both_sources_failing_yields_one_readable_answer(monkeypatch):
    """Khi không nguồn nào trả lời, agent nhận **một** câu nêu cả hai lý do, không phải hai lỗi."""
    service = FakeService(_local_miss())
    service.available = False
    service.degraded_reason = "Chưa cấu hình ARTEMIS_KB_DSN"
    _patch_local(monkeypatch, service)
    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: REMOTE)
    _counting_urlopen(monkeypatch, code=404)
    _patch_health(monkeypatch)

    out = kb_tool.lookup_knowledge(workflow_id="device_action.switch")

    assert out.startswith("[KB] không lấy được hướng dẫn")
    assert "chưa sẵn sàng" in out and "404" in out
