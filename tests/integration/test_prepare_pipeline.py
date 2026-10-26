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

"""Chuẩn bị prompt đầu-cuối: truy xuất thật → phân loại → tổng hợp → duyệt → chạy.

Chạy được không cần PostgreSQL (store trong bộ nhớ) và không cần mạng (embedder và LLM
đều giả), nên đây là acceptance test cho *luồng*, không phải cho hạ tầng::

    pytest tests/integration/test_prepare_pipeline.py -m integration -v

Bốn câu hỏi phải trả lời được ở đây, và không chỗ nào khác trả lời được:

1. Truy xuất thật (RRF + trust policy) cho ra kiến thức mà bước chuẩn bị dùng.
2. Workflow luật/modal là bối cảnh, không bao giờ trở thành việc phải làm.
3. Bản tester sửa tay là bản được chạy, cho cả Flash và Pro.
4. Sau khi duyệt, ARTEMIS không truy xuất knowledge base lần thứ hai.
"""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from artemis.knowledge.intent import (
    SECTION_CONTEXT,
    SECTION_CRITERIA,
    SECTION_GAP,
    SECTION_KNOWLEDGE,
    SECTION_PLAN,
)
from artemis.knowledge.models import Workflow, WorkflowStep
from artemis.knowledge.prepare import PrepareCache, PrepareOptions, prepare_prompt
from artemis.knowledge.service import KnowledgeService
from artemis.knowledge.stores.base import EmbeddingRow

console_server = importlib.import_module("apps.test_console.server")
integration = importlib.import_module("artemis.knowledge.integration")

PROMPT = "mở app và đăng nhập rồi chuyển sang nhà dled"
PLAN = {
    "tasks": [
        {"action": "mở app", "condition": "chỉ khi app chưa mở", "required": True},
        {"action": "đăng nhập", "condition": "chỉ khi chưa đăng nhập", "required": True},
        {"action": "chuyển nhà", "target": "dled", "required": True},
    ],
    "final_goal": {"type": "nhà hiện tại", "expected_value": "dled"},
}
OPTS = PrepareOptions(
    cache_ttl=0.0,
    max_knowledge_chars=1600,
    max_enhanced_chars=4000,
    intent_timeout=5.0,
)


def logout_workflow() -> Workflow:
    return Workflow(
        id="auth.logout",
        app_id="hunonic",
        name="Đăng xuất tài khoản",
        description="Đăng xuất khỏi tài khoản đang đăng nhập",
        aliases=["đăng xuất", "thoát tài khoản"],
        tags=["auth"],
        preconditions=["Người dùng đã đăng nhập"],
        steps=[
            WorkflowStep(action="tap", target="Tab cuối cùng trên bottom navigation"),
            WorkflowStep(action="tap", target="Đăng xuất"),
        ],
        success_criteria=["Màn hình đăng nhập xuất hiện"],
        status="verified",
        revision_no=2,
        updated_at="2026-10-26T10:00:00",
    )


def modal_rule() -> Workflow:
    return Workflow(
        id="home.switch_house_modal",
        app_id="hunonic",
        name="Xử lý modal 'Chuyển nhà' hiện sau khi vào Trang chủ",
        description="Modal 'Chuyển nhà' là GỢI Ý ĐỔI NHÀ của app, KHÔNG phải lỗi.",
        aliases=["chuyển nhà", "đổi nhà"],
        tags=["rule", "modal"],
        steps=[WorkflowStep(action="tap", target="nút 'Huỷ' trong modal 'Chuyển nhà'")],
        success_criteria=["Không còn modal 'Chuyển nhà' che màn hình"],
        status="verified",
        revision_no=1,
        updated_at="2026-10-26T10:00:00",
    )


class FakeStore:
    """Store trong bộ nhớ: đủ cho ``KnowledgeService.retrieve``."""

    def __init__(self, workflows) -> None:
        self.workflows = {workflow.id: workflow for workflow in workflows}
        self.exact: list[str] = []
        self.keyword = [(workflow.id, 1.0) for workflow in workflows]
        self.vector = [(workflow.id, 0.8) for workflow in workflows]
        self.logs: list = []

    def find_app_by_package(self, package_name: str) -> str | None:
        return "hunonic" if package_name == "com.iot.hunonic" else None

    def search_exact(self, query, app_id, version_id, limit=50):
        return list(self.exact)

    def search_keyword(self, query, app_id, version_id, limit=50):
        return list(self.keyword)

    def search_vector(self, vector, model, app_id, version_id, limit=50):
        return list(self.vector)

    def get_workflow(self, workflow_id):
        return self.workflows.get(workflow_id)

    def list_workflows(self, app_id=None, status=None):
        return list(self.workflows.values())

    def load_embeddings(self, model, app_id=None, version_id=None):
        return {}

    def save_embedding(self, workflow_id, model, content_hash, vector):
        self.embeddings[workflow_id] = EmbeddingRow(
            workflow_id=workflow_id,
            model=model,
            dim=len(vector),
            content_hash=content_hash,
            vector=vector,
        )

    def log_retrieval(self, entry):
        self.logs.append(entry)


class FakeEmbedder:
    model = "fake-embedding"

    def embed_query(self, text):
        return [1.0, 0.0]

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def service() -> KnowledgeService:
    return KnowledgeService(
        store=FakeStore([logout_workflow(), modal_rule()]), embedder=FakeEmbedder(), top_k=3
    )


def synthesizer(plan: dict | None = None):
    """LLM giả hai lượt; lượt tổng hợp trả lại đúng phần kiến thức đã nhận."""

    def call(messages, *, max_tokens, timeout):
        call.calls.append(messages)
        if "tách một yêu cầu" in messages[0]["content"]:
            return json.dumps(plan if plan is not None else PLAN, ensure_ascii=False)
        body = messages[1]["content"].split("nguồn duy nhất được dùng]", 1)[-1]
        return body.split("[PROFILE CHẠY]", 1)[0].strip()

    call.calls = []
    return call


# ── 1. Truy xuất thật → kiến thức đã phân loại ───────────────────────────
def test_real_retrieval_feeds_the_synthesis(service):
    call = synthesizer()

    outcome = prepare_prompt(
        PROMPT,
        app_package="com.iot.hunonic",
        profile="pro",
        service=service,
        synthesizer=call,
        options=OPTS,
        cache=PrepareCache(),
        model="test-model",
    )

    assert outcome.status == "llm"
    assert outcome.source["retrieval"] == "knowledge-service"
    assert outcome.source["per_query"]
    roles = {row["workflow_id"]: row["role"] for row in outcome.matched_workflows}
    assert roles["auth.logout"] == "supporting"
    assert roles["home.switch_house_modal"] == "supporting"
    assert "chuyển nhà dled" in [gap["task"]["query"] for gap in outcome.knowledge_gaps]
    assert [task["query"] for task in outcome.tasks] == ["mở app", "đăng nhập", "chuyển nhà dled"]

    brief = call.calls[1][1]["content"]
    assert "KẾ HOẠCH:" in brief
    assert "chuyển nhà dled" in brief
    assert "KB chưa có workflow thực hiện việc này" in brief
    # Bước của workflow không bao giờ vào prompt: agent tự tìm phần tử trên UI thật.
    assert "bottom navigation" not in brief
    assert "Các bước" not in brief
    assert "Không còn modal" not in brief


def test_the_rule_workflow_never_becomes_a_step(service):
    outcome = prepare_prompt(
        PROMPT,
        app_package="com.iot.hunonic",
        service=service,
        synthesizer=synthesizer(),
        options=OPTS,
        cache=PrepareCache(),
    )

    instructions = outcome.enhanced_prompt.split(SECTION_CONTEXT, 1)[0]
    assert "Huỷ" not in instructions
    assert SECTION_CONTEXT in outcome.enhanced_prompt
    assert "KHÔNG phải lỗi" in outcome.enhanced_prompt


def test_each_task_is_logged_as_its_own_retrieval(service):
    """Bước chuẩn bị dùng chính service của RAG, nên mỗi việc có một dòng log."""
    prepare_prompt(
        PROMPT,
        app_package="com.iot.hunonic",
        service=service,
        synthesizer=synthesizer(),
        options=OPTS,
        cache=PrepareCache(),
        run_id="run-1",
    )

    assert len(service._store.logs) == 3
    assert {log.run_id for log in service._store.logs} == {"run-1"}


def test_the_prompt_has_the_five_sections_and_stays_short(service):
    outcome = prepare_prompt(
        PROMPT,
        app_package="com.iot.hunonic",
        service=service,
        synthesizer=synthesizer(),
        options=OPTS,
        cache=PrepareCache(),
    )

    for section in (SECTION_PLAN, SECTION_CRITERIA, SECTION_GAP):
        assert section in outcome.enhanced_prompt
    assert outcome.enhanced_prompt.startswith(PROMPT)
    assert len(outcome.enhanced_prompt) < len(PROMPT) + OPTS.max_knowledge_chars


# ── 2 + 3. Duyệt, sửa, rồi chạy — Flash và Pro ──────────────────────────
class FakeProcess:
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
    monkeypatch.setattr(console_server, "LOG_DIR", tmp_path)
    monkeypatch.setattr(console_server, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(console_server, "BUDGET_FILE", tmp_path / "budget.json")
    monkeypatch.setattr(console_server, "budget", console_server.Budget())
    monkeypatch.setattr(console_server, "artemis_launcher", lambda: ["artemis"])
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
def test_prepare_review_edit_then_run(service, runner_env, profile):
    """Luồng đầy đủ: nhập → chuẩn bị → sửa → chạy, cho cả hai profile."""
    runner, captured = runner_env
    prepared = prepare_prompt(
        PROMPT,
        app_package="com.iot.hunonic",
        profile=profile,
        service=service,
        synthesizer=synthesizer(),
        options=OPTS,
        cache=PrepareCache(),
    )
    assert prepared.status == "llm"

    # Tester đọc rồi thêm một dòng của riêng mình trước khi duyệt.
    edited = prepared.enhanced_prompt + "\nChỉ thao tác trong app hunonic."

    task = runner.start(
        console_server.RunRequest(
            goal=PROMPT, profile=profile, package="com.iot.hunonic", prompt_override=edited
        )
    )

    assert task.goal_sent == edited
    assert edited in captured["cmd"]
    assert captured["env"]["ARTEMIS_KB_PRESUPPLIED"] == "1"


def test_both_profiles_receive_the_same_approved_prompt(service, runner_env):
    runner, captured = runner_env
    prepared = prepare_prompt(
        PROMPT,
        app_package="com.iot.hunonic",
        service=service,
        synthesizer=synthesizer(),
        options=OPTS,
        cache=PrepareCache(),
    )

    commands = []
    for profile in ("pro", "flash"):
        runner.start(
            console_server.RunRequest(
                goal=PROMPT,
                profile=profile,
                package="com.iot.hunonic",
                prompt_override=prepared.enhanced_prompt,
            )
        )
        commands.append(list(captured["cmd"]))

    assert commands[0].count(prepared.enhanced_prompt) == 1
    assert commands[1].count(prepared.enhanced_prompt) == 1


# ── 4. Không truy xuất lần thứ hai ───────────────────────────────────────
def test_artemis_does_not_retrieve_again_when_the_prompt_is_presupplied(monkeypatch, service):
    monkeypatch.setenv(integration.PRESUPPLIED_ENV, "1")
    monkeypatch.setattr(integration, "get_service", lambda: service)

    context = integration.retrieve_for_run(PROMPT, app_package="com.iot.hunonic", run_id="r1")

    assert context.injected is False
    assert context.text == ""
    assert context.source == "console"
    assert service._store.logs == []


def test_the_server_endpoint_runs_the_whole_pipeline(monkeypatch, service):
    """Endpoint với truy xuất thật (store trong bộ nhớ) và LLM giả."""
    monkeypatch.setenv("CONSOLE_ACCESS_KEY", "khoa")
    monkeypatch.setattr(console_server, "ACCESS_TOKENS_CACHE", None)

    import artemis.knowledge.prepare as prepare_module
    import artemis.knowledge.service as service_module

    monkeypatch.setattr(service_module, "get_service", lambda: service)
    monkeypatch.setattr(prepare_module, "get_cache", lambda: PrepareCache(ttl=0.0))
    monkeypatch.setattr(
        prepare_module, "get_rate_limiter", lambda: prepare_module.RateLimiter(50, 60.0)
    )
    monkeypatch.setattr(console_server, "console_llm_chat", synthesizer())

    body = (
        TestClient(console_server.app)
        .post(
            "/api/knowledge/prepare",
            json={"prompt": PROMPT, "app_package": "com.iot.hunonic", "profile": "pro"},
            headers={"Authorization": "Bearer khoa"},
        )
        .json()
    )

    assert body["status"] == "llm"
    assert body["ready"] is True
    assert body["original_prompt"] == PROMPT
    assert body["enhanced_prompt"].startswith(PROMPT)
    assert [task["query"] for task in body["tasks"]] == [
        "mở app",
        "đăng nhập",
        "chuyển nhà dled",
    ]
    assert "chuyển nhà dled" in [gap["task"]["query"] for gap in body["knowledge_gaps"]]
    assert body["llm"]["used"] is True
    assert all(check["ok"] for check in body["checks"])
