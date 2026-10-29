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

"""Tool ``kb_lookup``: đường tra cứu KB giữa lúc chạy.

Ba tính chất phải giữ, vì chúng là lý do tool này tồn tại:

1. **Không bao giờ ném lỗi** — KB hỏng chỉ được làm câu trả lời nghèo đi, không được làm hỏng run.
2. **Không tìm thấy là một câu trả lời** — im lặng thì agent tự nghĩ ra cách làm (đúng lỗi đã xảy
   ra với bước chuyển tài khoản ngày 29/10/2026).
3. **Đi đúng đường của bản triển khai hiện tại** — có ``ARTEMIS_KB_API`` thì hỏi console, không thì
   dùng KB local; không dựng KB thứ hai.
"""

import json
import urllib.error

import pytest

from artemis.knowledge.models import RetrievalResult, Workflow, WorkflowHit, WorkflowStep
from artemis.tools import knowledge as kb_tool


class FakeService:
    available = True
    degraded_reason = ""
    context_max_chars = 4000

    def __init__(self, result: RetrievalResult):
        self.result = result
        self.calls: list[dict] = []

    def retrieve(self, query, app_id, version_id=None, top_k=None, run_id=None, filters=None):
        self.calls.append(
            {"query": query, "app_id": app_id, "top_k": top_k, "filters": filters, "run_id": run_id}
        )
        return self.result


def _workflow(**metadata) -> Workflow:
    return Workflow(
        id="modal.home.switch_house",
        app_id="hunonic",
        name="Modal chuyển nhà",
        status="draft",
        metadata={"type": "modal", "screen": "home", **(metadata or {})},
        steps=[WorkflowStep(action="tap", target="Chọn nhà Mesh Tạo 2", expected_state="Nhà đổi")],
    )


def _result(workflow: Workflow | None, *, found: bool = True, reason: str = "") -> RetrievalResult:
    return RetrievalResult(
        found=found,
        query="",
        app_id="hunonic",
        hits=[WorkflowHit(workflow=workflow, score=1.0, channels={"filtered": 1})]
        if workflow
        else [],
        reason=reason,
        filters={"screens": ["home"]},
    )


def _patch_local(monkeypatch, service: FakeService, *, app_id: str = "hunonic") -> None:
    import artemis.knowledge.integration as integration
    import artemis.knowledge.service as service_module

    monkeypatch.setattr(service_module, "get_service", lambda: service)
    monkeypatch.setattr(integration, "resolve_app_id", lambda package, svc: app_id)
    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: "")


# ── đầu vào tối thiểu ────────────────────────────────────────────────────
def test_lookup_without_any_criterion_asks_for_one():
    out = kb_tool.lookup_knowledge()
    assert "cần ít nhất một trong" in out


def test_local_lookup_renders_scope_conditions_and_steps(monkeypatch):
    workflow = _workflow()
    service = FakeService(_result(workflow))
    _patch_local(monkeypatch, service)

    out = kb_tool.lookup_knowledge(screen="home", ui_type="modal", max_results=3)

    assert "modal.home.switch_house" in out
    assert "loại modal" in out and "màn hình home" in out
    assert "bước: tap «Chọn nhà Mesh Tạo 2» ⇒ Nhà đổi" in out
    assert service.calls[0]["filters"].screens == ("home",)
    assert service.calls[0]["filters"].types == ("modal",)
    assert service.calls[0]["app_id"] == "hunonic"


def test_a_draft_entry_is_flagged_as_needing_verification(monkeypatch):
    workflow = _workflow(nav_edge={"verified": False, "guards": [{"text": "isLoggedIn"}]})
    _patch_local(monkeypatch, FakeService(_result(workflow)))

    out = kb_tool.lookup_knowledge(namespace="navigation")

    assert "điều kiện theo source: isLoggedIn" in out
    assert "chưa xác minh trên thiết bị" in out


def test_no_match_is_an_explicit_answer_not_silence(monkeypatch):
    _patch_local(
        monkeypatch, FakeService(_result(None, found=False, reason="Không có mục nào khớp"))
    )

    out = kb_tool.lookup_knowledge(screen="khong_ton_tai")

    assert "không có hướng dẫn nào khớp" in out
    assert "Không được tự nghĩ ra cách làm khác" in out


def test_missing_app_is_reported_instead_of_querying_blindly(monkeypatch):
    service = FakeService(_result(None))
    _patch_local(monkeypatch, service, app_id="")

    out = kb_tool.lookup_knowledge(query="chuyển nhà", app_package="")

    assert "chưa xác định được app" in out
    assert not service.calls


def test_unavailable_service_degrades_instead_of_raising(monkeypatch):
    service = FakeService(_result(None))
    service.available = False
    service.degraded_reason = "Chưa cấu hình ARTEMIS_KB_DSN"
    _patch_local(monkeypatch, service)

    out = kb_tool.lookup_knowledge(query="chuyển nhà")

    assert "chưa sẵn sàng" in out


def test_service_exception_becomes_a_tool_answer(monkeypatch):
    class Boom(FakeService):
        def retrieve(self, *args, **kwargs):
            raise RuntimeError("database is down")

    _patch_local(monkeypatch, Boom(_result(None)))

    out = kb_tool.lookup_knowledge(query="chuyển nhà")

    assert "tra cứu lỗi" in out


# ── đường từ xa (máy tester không có cơ sở dữ liệu) ──────────────────────
class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_remote_lookup_asks_the_console_with_the_same_filters(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "found": True,
                "reason": "",
                "filters": {"screens": ["home"]},
                "hits": [
                    {
                        "workflow_id": "modal.home.switch_house",
                        "name": "Modal chuyển nhà",
                        "status": "draft",
                        "type": "modal",
                        "screen": "home",
                        "guards": [],
                        "verified": False,
                        "steps": [{"action": "tap", "target": "Chọn nhà", "expected_state": ""}],
                    }
                ],
            }
        )

    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: "https://kb.example/artemis")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    out = kb_tool.lookup_knowledge(screen="home", app_package="com.hunonic.app")

    assert captured["url"].endswith("/api/kb/retrieve")
    assert captured["body"]["screen"] == ["home"]
    assert captured["body"]["app_package"] == "com.hunonic.app"
    assert "modal.home.switch_house" in out
    assert "chưa xác minh trên thiết bị" in out


def test_an_unusable_remote_endpoint_falls_back_to_the_local_kb(monkeypatch):
    """Đo thật 29/10/2026: console trên VPS trả 404 cho cả router ``/api/kb/*`` ⇒ nếu tool coi đó là
    câu trả lời thì mọi lần hỏi KB trong run thật đều thất bại, dù máy tester vẫn có KB local dùng
    được (DSN + tunnel). Endpoint hỏng phải rơi về KB local, không phải trả lỗi cho agent.
    """

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "not found", None, None)

    service = FakeService(_result(_workflow()))
    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: "https://kb.example/artemis")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    import artemis.knowledge.integration as integration
    import artemis.knowledge.service as service_module

    monkeypatch.setattr(service_module, "get_service", lambda: service)
    monkeypatch.setattr(integration, "resolve_app_id", lambda package, svc: "hunonic")

    out = kb_tool.lookup_knowledge(query="chuyển nhà")

    assert "modal.home.switch_house" in out  # câu trả lời của KB local
    assert service.calls  # và nó thật sự được hỏi


def test_a_remote_answer_is_not_overridden_by_a_local_lookup(monkeypatch):
    """Remote *trả lời* (kể cả không tìm thấy) thì không hỏi thêm KB local — tránh hai nguồn sự thật."""

    def fake_urlopen(request, timeout=None):
        return _FakeResponse({"found": False, "reason": "không có", "filters": {}, "hits": []})

    service = FakeService(_result(_workflow()))
    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: "https://kb.example/artemis")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    import artemis.knowledge.service as service_module

    monkeypatch.setattr(service_module, "get_service", lambda: service)

    out = kb_tool.lookup_knowledge(query="chuyển nhà")

    assert "không có hướng dẫn nào khớp" in out
    assert service.calls == []


# ── khai báo tool ───────────────────────────────────────────────────────
def test_declaration_is_available_to_flash_with_the_same_filters():
    declaration = kb_tool.kb_lookup_declaration()
    assert declaration.name == "kb_lookup"
    properties = declaration.parameters["properties"]
    assert {"query", "workflow_id", "namespace", "type", "screen", "tags"} <= set(properties)


def test_tool_is_not_advertised_without_a_knowledge_base(monkeypatch):
    """Prompt không được quảng cáo tool gọi vào hư không."""
    import artemis.knowledge.service as service_module

    class Unavailable:
        available = False

    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: "")
    monkeypatch.setattr(service_module, "get_service", lambda: Unavailable())
    assert kb_tool.kb_lookup_available() is False

    monkeypatch.setattr(kb_tool, "remote_api_url", lambda: "https://kb.example/artemis")
    assert kb_tool.kb_lookup_available() is True


@pytest.mark.asyncio
async def test_tool_passes_the_locked_app_package_from_the_context(monkeypatch):
    """Máy tester chỉ biết package đang test; tool phải chuyển nó xuống để tra ra app_id."""
    seen: dict = {}

    def fake_lookup(query="", **kwargs):
        seen.update(kwargs)
        return "[KB] ok"

    class Setup:
        def get_locked_app_package(self):
            return "com.hunonic.app"

    class Ctx:
        execution_setup = Setup()
        run_id = "run-1"

    monkeypatch.setattr(kb_tool, "lookup_knowledge", fake_lookup)

    out = await kb_tool.KbLookupTool().execute(ctx=Ctx(), query="chuyển nhà", namespace="modal")

    assert out == "[KB] ok"
    assert seen["app_package"] == "com.hunonic.app"
    assert seen["namespace"] == "modal"
    assert seen["run_id"] == "run-1"
