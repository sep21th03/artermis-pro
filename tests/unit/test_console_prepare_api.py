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

"""``POST /api/knowledge/prepare`` — endpoint trung tâm của bước chuẩn bị prompt.

Ba thứ được khoá ở đây, và cả ba đều là ranh giới chứ không phải tiện ích:

1. **Vai trò.** Route tồn tại ở ``server`` (VPS) và ``all`` (máy dev), và *không
   tồn tại* ở ``edge`` — máy cắm điện thoại không giữ key LLM.
2. **Không bao giờ chặn tester.** LLM hỏng ⇒ 200 kèm ``status="fallback"``, không
   phải 5xx. Chỉ hạn mức (429) và dữ liệu vào sai (422) mới là lỗi.
3. **Prompt đã duyệt là prompt được chạy.** ``prompt_override`` đi thẳng vào CLI và
   ``ARTEMIS_KB_PRESUPPLIED=1`` báo ARTEMIS đừng truy xuất KB lần thứ hai.
"""

from __future__ import annotations

import importlib
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

console_server = importlib.import_module("apps.test_console.server")
prepare_module = importlib.import_module("artemis.knowledge.prepare")
service_module = importlib.import_module("artemis.knowledge.service")

KEY = "khoa-thu-nghiem"
PROMPT = "Test đăng xuất tài khoản 0987654321 trên app hunonic"
#: Kế hoạch giả: một việc, bám vào chính câu của tester.
PLAN = {
    "tasks": [
        {"action": "Test đăng xuất tài khoản", "target": "0987654321", "required": True},
    ],
    "final_goal": {"type": "trạng thái tài khoản", "expected_value": "đã đăng xuất"},
}
#: Câu trả lời tổng hợp giả — đủ ba mục mà kế hoạch đòi (không có bối cảnh/khoảng trống).
ENHANCED = (
    "KẾ HOẠCH:\n"
    "1. Test đăng xuất tài khoản 0987654321\n"
    "KIẾN THỨC ỨNG DỤNG:\n"
    "- auth.logout là chức năng đăng xuất khỏi tài khoản đang đăng nhập.\n"
    "  Điều kiện phải kiểm tra trên UI (không được mặc định là đúng): người dùng đã đăng nhập.\n"
    "  Tiêu chí hoàn thành: màn hình đăng nhập xuất hiện.\n"
    "TIÊU CHÍ XÁC MINH:\n"
    "- Màn hình đăng nhập xuất hiện."
)


class FakeHit:
    def __init__(self, workflow_id: str = "auth.logout", trusted: bool = True) -> None:
        self.workflow = FakeWorkflow(workflow_id)
        self.trusted = trusted
        self.score = 0.5
        self.channels = {"exact": 1}


class FakeWorkflow:
    def __init__(self, workflow_id: str) -> None:
        self.id = workflow_id
        self.name = "Đăng xuất tài khoản"
        self.status = "verified"
        self.description = "Đăng xuất khỏi tài khoản đang đăng nhập"
        self.aliases = ["thoát tài khoản"]
        self.preconditions = ["Người dùng đã đăng nhập"]
        self.success_criteria = ["Màn hình đăng nhập xuất hiện"]
        self.tags: list[str] = []
        self.metadata: dict = {}
        self.steps: list = []
        self.revision_no = 1
        self.updated_at = "2026-10-26T10:00:00"

    def search_text(self) -> str:
        return f"{self.id} {self.name} {self.description}"

    def embedding_text(self) -> str:
        return " ".join(
            [self.name, self.description, *self.aliases, *self.tags]
            + [step.get("target", "") for step in self.steps]
        ).strip()


class FakeResult:
    def __init__(self, hits: list[FakeHit]) -> None:
        self.found = any(hit.trusted for hit in hits)
        self.hits = hits
        self.degraded = False
        self.reason = ""
        self.app_id = "hunonic"
        self.version_id = None
        self.timings_ms = {"total_ms": 1.0}


class FakeService:
    """Chỉ đủ để endpoint trả về một kết quả có thật, không cần database."""

    available = True
    degraded_reason = ""

    def __init__(self, hits=None) -> None:
        self.hits = hits if hits is not None else [FakeHit()]
        self._store = self
        self.calls: list[tuple] = []

    def find_app_by_package(self, package: str) -> str | None:
        return "hunonic" if package == "com.iot.hunonic" else None

    def get_workflow(self, workflow_id: str):
        """Cache xác nhận phiên bản KB bằng hàm này (store thật cũng có)."""
        return next((hit.workflow for hit in self.hits if hit.workflow.id == workflow_id), None)

    def retrieve(self, query, app_id, version_id=None, top_k=None, run_id=None) -> FakeResult:
        self.calls.append((query, app_id, version_id, top_k, run_id))
        return FakeResult(self.hits)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CONSOLE_ACCESS_KEY", KEY)
    monkeypatch.setattr(console_server, "ACCESS_TOKENS_CACHE", None)
    return TestClient(console_server.app)


@pytest.fixture
def auth():
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def fake_service(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(service_module, "get_service", lambda: service)
    return service


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    """Cache/limiter dùng chung của tiến trình không được rò giữa các test."""
    cache = prepare_module.PrepareCache(ttl=60.0, max_entries=8)
    monkeypatch.setattr(prepare_module, "get_cache", lambda: cache)
    monkeypatch.setattr(
        prepare_module, "get_rate_limiter", lambda: prepare_module.RateLimiter(50, 60.0)
    )
    return cache


@pytest.fixture
def synthesizer(monkeypatch):
    """Thay LLM thật: lượt phân tích mục tiêu trả JSON, lượt tổng hợp trả ``ENHANCED``."""

    def install(
        text: str = ENHANCED,
        error: Exception | None = None,
        plan: dict | None = None,
    ):
        calls: list[dict] = []

        def call(messages, *, max_tokens, timeout):
            calls.append({"messages": messages, "max_tokens": max_tokens, "timeout": timeout})
            if error is not None:
                raise error
            if "tách một yêu cầu" in messages[0]["content"]:
                return json.dumps(plan if plan is not None else PLAN, ensure_ascii=False)
            return text

        monkeypatch.setattr(console_server, "console_llm_chat", call)
        return calls

    return install


def paths(module) -> set[str]:
    return set(module.app.openapi()["paths"])


@pytest.fixture
def load_role(monkeypatch):
    """Nạp lại module console dưới một vai trò (giống test_console_role_split)."""

    def _load(role: str):
        monkeypatch.setenv("CONSOLE_ROLE", role)
        return importlib.reload(console_server)

    yield _load
    monkeypatch.delenv("CONSOLE_ROLE", raising=False)
    importlib.reload(console_server)


# ── Vai trò: route tồn tại ở đâu ─────────────────────────────────────────
def test_the_server_role_serves_the_prepare_endpoint(load_role):
    assert "/api/knowledge/prepare" in paths(load_role("server"))


def test_the_all_role_serves_the_prepare_endpoint(load_role):
    assert "/api/knowledge/prepare" in paths(load_role("all"))


def test_the_edge_role_does_not_register_the_prepare_endpoint(load_role):
    """Máy cắm điện thoại không giữ key LLM ⇒ route phải KHÔNG tồn tại."""
    assert "/api/knowledge/prepare" not in paths(load_role("edge"))


def test_the_edge_role_answers_404_for_prepare(load_role):
    module = load_role("edge")
    response = TestClient(module.app).post(
        "/api/knowledge/prepare", json={"prompt": PROMPT, "app_package": "com.iot.hunonic"}
    )

    assert response.status_code == 404


# ── Xác thực ─────────────────────────────────────────────────────────────
def test_the_shared_server_requires_the_access_key(load_role, monkeypatch):
    monkeypatch.setenv("CONSOLE_ACCESS_KEY", KEY)
    monkeypatch.setattr(console_server, "ACCESS_TOKENS_CACHE", None)
    module = load_role("server")

    response = TestClient(module.app).post(
        "/api/knowledge/prepare", json={"prompt": PROMPT, "app_package": "com.iot.hunonic"}
    )

    assert response.status_code == 401


def test_the_shared_server_rejects_a_wrong_key(load_role, monkeypatch, fake_service, synthesizer):
    monkeypatch.setenv("CONSOLE_ACCESS_KEY", KEY)
    monkeypatch.setattr(console_server, "ACCESS_TOKENS_CACHE", None)
    module = load_role("server")
    synthesizer()

    response = TestClient(module.app).post(
        "/api/knowledge/prepare",
        json={"prompt": PROMPT, "app_package": "com.iot.hunonic"},
        headers={"Authorization": "Bearer sai-khoa"},
    )

    assert response.status_code == 401


def test_the_shared_server_accepts_the_right_key(load_role, monkeypatch, fake_service, synthesizer):
    monkeypatch.setenv("CONSOLE_ACCESS_KEY", KEY)
    monkeypatch.setattr(console_server, "ACCESS_TOKENS_CACHE", None)
    module = load_role("server")
    synthesizer()

    response = TestClient(module.app).post(
        "/api/knowledge/prepare",
        json={"prompt": PROMPT, "app_package": "com.iot.hunonic"},
        headers={"Authorization": f"Bearer {KEY}"},
    )

    assert response.status_code == 200


def test_the_dev_machine_needs_no_key(client, fake_service, synthesizer):
    """Vai trò ``all`` là công cụ tại chỗ; phần còn lại của /api/* cũng không có khoá."""
    synthesizer()

    response = client.post(
        "/api/knowledge/prepare", json={"prompt": PROMPT, "app_package": "com.iot.hunonic"}
    )

    assert response.status_code == 200


# ── Hợp đồng trả về ──────────────────────────────────────────────────────
def test_the_response_carries_everything_the_tester_needs(client, fake_service, synthesizer):
    synthesizer()

    body = client.post(
        "/api/knowledge/prepare",
        json={"prompt": PROMPT, "app_package": "com.iot.hunonic", "profile": "pro"},
    ).json()

    assert body["status"] == "llm"
    assert body["ready"] is True
    assert body["original_prompt"] == PROMPT
    assert body["enhanced_prompt"].startswith(PROMPT)
    assert body["matched_workflows"][0]["workflow_id"] == "auth.logout"
    assert body["matched_workflows"][0]["role"] == "primary"
    assert body["excluded_workflows"] == []
    assert body["knowledge_gaps"] == []
    assert body["warnings"] == []
    assert [task["query"] for task in body["tasks"]] == ["Test đăng xuất tài khoản 0987654321"]
    assert body["plan"]["final_goal"]["expected_value"] == "đã đăng xuất"
    assert body["metrics"]["llm_calls"] == 2
    assert body["metrics"]["queries"] == ["Test đăng xuất tài khoản 0987654321"]
    assert body["source"]["app_id"] == "hunonic"
    assert body["source"]["synthesis"] == "llm"
    assert body["source"]["per_query"][0]["found"] is True
    assert body["llm"]["used"] is True
    assert body["found"] is True
    assert body["cached"] is False
    assert body["fallback_reason"] == ""
    assert all(check["ok"] for check in body["checks"])


def test_the_task_query_reaches_retrieval_once(client, fake_service, synthesizer):
    synthesizer()

    client.post(
        "/api/knowledge/prepare",
        json={"prompt": PROMPT, "app_package": "com.iot.hunonic", "run_id": "r9"},
    )

    # Truy xuất chạy theo *việc* trong kế hoạch, không theo cả câu.
    assert fake_service.calls == [
        ("Test đăng xuất tài khoản 0987654321", "hunonic", None, None, "r9")
    ]


def test_both_llm_stages_carry_what_they_need(client, fake_service, synthesizer):
    calls = synthesizer()

    client.post("/api/knowledge/prepare", json={"prompt": PROMPT, "app_package": "com.iot.hunonic"})

    assert len(calls) == 2
    analysis, synthesis = calls
    # Lượt 1: phân tích mục tiêu — chỉ cần câu của tester và yêu cầu JSON.
    assert "JSON" in analysis["messages"][0]["content"]
    assert PROMPT in analysis["messages"][1]["content"]
    assert analysis["max_tokens"] > 0
    # Lượt 2: tổng hợp — có mục tiêu gốc và kiến thức đã phân loại.
    assert PROMPT in synthesis["messages"][1]["content"]
    assert "auth.logout" in synthesis["messages"][1]["content"]
    assert "KẾ HOẠCH:" in synthesis["messages"][1]["content"]
    assert synthesis["timeout"] > 0


# ── Không bao giờ chặn tester ────────────────────────────────────────────
def test_a_broken_llm_still_answers_200_with_a_fallback(client, fake_service, synthesizer):
    synthesizer(error=RuntimeError("AI proxy trả 502"))

    response = client.post(
        "/api/knowledge/prepare", json={"prompt": PROMPT, "app_package": "com.iot.hunonic"}
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "fallback"
    assert "502" in body["fallback_reason"]
    assert body["enhanced_prompt"].startswith(PROMPT)
    assert "auth.logout" in body["enhanced_prompt"]
    assert body["llm"]["synthesis"] == "deterministic"


def test_a_degraded_knowledge_base_still_answers_200(client, monkeypatch, synthesizer):
    monkeypatch.setattr(service_module, "get_service", lambda: service_module.KnowledgeService())
    synthesizer()

    response = client.post(
        "/api/knowledge/prepare", json={"prompt": PROMPT, "app_package": "com.iot.hunonic"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["enhanced_prompt"] == PROMPT


def test_an_unknown_app_keeps_the_original_prompt(client, fake_service, synthesizer):
    calls = synthesizer()

    body = client.post(
        "/api/knowledge/prepare", json={"prompt": PROMPT, "app_package": "com.la.hoac"}
    ).json()

    assert body["status"] == "no_knowledge"
    assert body["enhanced_prompt"] == PROMPT
    assert calls == []


def test_prepare_can_be_switched_off_without_touching_the_route(
    client, monkeypatch, fake_service, synthesizer
):
    """``ARTEMIS_PREPARE_ENABLED=0`` ⇒ bản rút gọn xác định, không gọi LLM."""
    from artemis.config.settings import settings as artemis_settings

    calls = synthesizer()
    monkeypatch.setattr(artemis_settings, "ARTEMIS_PREPARE_ENABLED", False)

    body = client.post(
        "/api/knowledge/prepare", json={"prompt": PROMPT, "app_package": "com.iot.hunonic"}
    ).json()

    assert body["status"] == "fallback"
    assert "Chưa cấu hình LLM" in body["fallback_reason"]
    assert calls == []


# ── Hạn mức và xác thực dữ liệu vào ──────────────────────────────────────
def test_the_rate_limiter_answers_429_with_retry_after(
    client, monkeypatch, fake_service, synthesizer
):
    # Một limiter dùng chung cho cả hai request — ``get_rate_limiter`` được gọi mỗi
    # request, nên tạo mới trong lambda là vô hiệu hoá hạn mức.
    limiter = prepare_module.RateLimiter(1, 60.0)
    monkeypatch.setattr(prepare_module, "get_rate_limiter", lambda: limiter)
    synthesizer()

    first = client.post(
        "/api/knowledge/prepare", json={"prompt": PROMPT, "app_package": "com.iot.hunonic"}
    )
    second = client.post(
        "/api/knowledge/prepare", json={"prompt": PROMPT, "app_package": "com.iot.hunonic"}
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1


def test_a_short_prompt_is_rejected_by_validation(client):
    response = client.post("/api/knowledge/prepare", json={"prompt": "hi"})

    assert response.status_code == 422


def test_an_overlong_prompt_is_rejected_by_validation(client):
    response = client.post("/api/knowledge/prepare", json={"prompt": "x" * 5000})

    assert response.status_code == 422


def test_an_unknown_profile_is_rejected_by_validation(client):
    response = client.post("/api/knowledge/prepare", json={"prompt": PROMPT, "profile": "ultra"})

    assert response.status_code == 422


# ── Cache và làm mới ─────────────────────────────────────────────────────
def test_the_second_request_is_served_from_cache(client, fake_service, synthesizer):
    calls = synthesizer()
    payload = {"prompt": PROMPT, "app_package": "com.iot.hunonic"}

    first = client.post("/api/knowledge/prepare", json=payload).json()
    second = client.post("/api/knowledge/prepare", json=payload).json()

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["metrics"]["llm_calls"] == 0
    # Lần đầu tốn 2 lượt LLM (phân tích + tổng hợp); lần lặp lại không tốn lượt nào.
    assert len(calls) == 2


def test_refresh_bypasses_the_cache(client, fake_service, synthesizer):
    calls = synthesizer()
    payload = {"prompt": PROMPT, "app_package": "com.iot.hunonic", "refresh": True}

    first = client.post("/api/knowledge/prepare", json=payload).json()
    second = client.post("/api/knowledge/prepare", json=payload).json()

    assert first["cached"] is False
    assert second["cached"] is False
    # Mỗi lần "Chuẩn bị lại" tốn 2 lượt LLM (phân tích + tổng hợp).
    assert len(calls) == 4


# ── /api/health: máy edge biết máy chủ ở đâu ─────────────────────────────
def test_health_tells_an_edge_machine_where_the_knowledge_server_is(load_role, monkeypatch):
    monkeypatch.setenv("ARTEMIS_KB_API", "https://sep21th03.com/artemis")
    module = load_role("edge")

    body = TestClient(module.app).get("/api/health").json()

    assert body["kb_api"] == "https://sep21th03.com/artemis"


def test_health_on_the_shared_server_does_not_advertise_a_kb_api(load_role):
    body = TestClient(load_role("server").app).get("/api/health").json()

    assert "kb_api" not in body


# ── Tên model: nội suy như ARTEMIS ───────────────────────────────────────
def test_an_interpolated_model_name_is_resolved(monkeypatch):
    """``config/artemis.jsonc`` trên VPS dùng ``${env:...:-default}``."""
    monkeypatch.setenv("GEMINI35FLASHLITE_DEFAULT", "ts/gemini-3.5-flash-lite")

    resolved = console_server.resolve_model_name("${env:GEMINI35FLASHLITE_DEFAULT:-fallback}")

    assert resolved == "ts/gemini-3.5-flash-lite"


def test_an_interpolated_model_name_falls_back_to_its_default(monkeypatch):
    monkeypatch.delenv("GEMINI35FLASHLITE_DEFAULT", raising=False)

    resolved = console_server.resolve_model_name(
        "${env:GEMINI35FLASHLITE_DEFAULT:-ts/gemini-3.5-flash-lite}"
    )

    assert resolved == "ts/gemini-3.5-flash-lite"


def test_a_plain_model_name_is_untouched():
    assert console_server.resolve_model_name("gpt-4o-mini") == "gpt-4o-mini"


# ── Bộ chạy CLI: phải có trên mọi nền tảng ───────────────────────────────
def test_the_launcher_prefers_the_repo_venv(monkeypatch, tmp_path):
    binary = tmp_path / "artemis"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(console_server, "ARTEMIS_BIN", binary)

    assert console_server.artemis_launcher() == [str(binary)]


def test_the_launcher_uses_uv_when_there_is_no_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(console_server, "ARTEMIS_BIN", tmp_path / "missing")
    monkeypatch.setattr(console_server, "shutil", SimpleNamespace(which=lambda name: "C:/uv.exe"))

    launcher = console_server.artemis_launcher()

    assert launcher is not None and launcher[:2] == ["C:/uv.exe", "run"]


def test_the_launcher_falls_back_to_this_interpreter(monkeypatch, tmp_path):
    """Windows không có `.venv/bin/artemis` và thường không có `uv`.

    Không có fallback này thì nút *Chạy* trả về 500 trên mọi máy Windows, dù
    ARTEMIS chạy được ngay bằng chính interpreter đang phục vụ console.
    """
    monkeypatch.setattr(console_server, "ARTEMIS_BIN", tmp_path / "missing")
    monkeypatch.setattr(console_server, "shutil", SimpleNamespace(which=lambda name: None))

    assert console_server.artemis_launcher() == [sys.executable, "-m", "artemis"]


def test_the_launcher_reports_nothing_when_artemis_is_not_importable(monkeypatch, tmp_path):
    monkeypatch.setattr(console_server, "ARTEMIS_BIN", tmp_path / "missing")
    monkeypatch.setattr(console_server, "shutil", SimpleNamespace(which=lambda name: None))
    monkeypatch.setattr(
        console_server,
        "importlib",
        SimpleNamespace(util=SimpleNamespace(find_spec=lambda name: None)),
    )

    assert console_server.artemis_launcher() is None


# ── Prompt đã duyệt là prompt được chạy (Flash và Pro) ──────────────────
class FakeProcess:
    """``Popen`` giả: ghi lại lệnh + môi trường, kết thúc ngay."""

    def __init__(self, cmd, **kwargs) -> None:
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdout: list[str] = []
        self.returncode = 0

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def runner_env(monkeypatch, tmp_path):
    """Một ``Runner`` chạy trong tmp_path, với tiến trình con được thay bằng giả."""
    monkeypatch.setattr(console_server, "LOG_DIR", tmp_path)
    monkeypatch.setattr(console_server, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(console_server, "BUDGET_FILE", tmp_path / "budget.json")
    monkeypatch.setattr(console_server, "budget", console_server.Budget())
    monkeypatch.setattr(console_server, "artemis_launcher", lambda: ["artemis"])
    # Không để test chạy luồng nền: chỉ cần lệnh và môi trường của tiến trình con.
    monkeypatch.setattr(console_server.Runner, "_pump", lambda self, proc, task: None)
    monkeypatch.setattr(console_server.Runner, "_watchdog", lambda self, proc, task: None)
    monkeypatch.setattr(console_server.Runner, "_tail_trace", lambda self, task: None)

    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProcess(cmd, **kwargs)

    monkeypatch.setattr(console_server.subprocess, "Popen", fake_popen)
    runner = console_server.Runner()
    monkeypatch.setattr(console_server, "runner", runner)
    return runner, captured


@pytest.mark.parametrize("profile", ["pro", "flash"])
def test_the_approved_prompt_is_the_one_the_agent_runs(runner_env, profile):
    runner, captured = runner_env
    request = console_server.RunRequest(
        goal="Test đăng xuất",
        profile=profile,
        package="com.iot.hunonic",
        prompt_override=ENHANCED,
    )

    task = runner.start(request)

    assert task.goal == "Test đăng xuất"
    assert task.goal_sent == ENHANCED
    assert ENHANCED in captured["cmd"]
    assert captured["cmd"][-1] != "Test đăng xuất"


@pytest.mark.parametrize("profile", ["pro", "flash"])
def test_the_approved_prompt_tells_artemis_not_to_retrieve_again(runner_env, profile):
    """``ARTEMIS_KB_PRESUPPLIED=1`` là chốt chặn truy xuất lần thứ hai."""
    runner, captured = runner_env
    request = console_server.RunRequest(
        goal="Test đăng xuất",
        profile=profile,
        package="com.iot.hunonic",
        prompt_override=ENHANCED,
    )

    runner.start(request)

    assert captured["env"]["ARTEMIS_KB_PRESUPPLIED"] == "1"


def test_without_an_approved_prompt_the_agent_gets_the_original(runner_env):
    runner, captured = runner_env
    request = console_server.RunRequest(
        goal="Test đăng xuất", profile="pro", package="com.iot.hunonic"
    )

    task = runner.start(request)

    assert task.goal_sent == "Test đăng xuất"
    assert "ARTEMIS_KB_PRESUPPLIED" not in captured["env"]


def test_a_flash_run_is_still_disabled_outputter_style(runner_env):
    runner, captured = runner_env
    request = console_server.RunRequest(
        goal="Test đăng xuất",
        profile="flash",
        package="com.iot.hunonic",
        prompt_override=ENHANCED,
    )

    runner.start(request)

    assert "--disable-outputter" in captured["cmd"]
    assert "--profile" in captured["cmd"]
