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

"""End-to-end knowledge base tests against a real PostgreSQL + pgvector.

Skipped when no database is reachable, so the default unit suite stays
deterministic.  Run explicitly with::

    pytest tests/integration/test_knowledge_pipeline.py -v

The embedding provider is faked with topic vectors: the network is not part of
what this file is testing, and a deterministic vector keeps the assertions about
scoping and fusion meaningful.
"""

from __future__ import annotations

import os

import pytest

from artemis.knowledge.models import Workflow, WorkflowStep
from artemis.knowledge.service import KnowledgeService
from artemis.knowledge.text import normalize

TEST_DSN = os.getenv(
    "ARTEMIS_KB_TEST_DSN", "postgresql://nguyenhoa@localhost:5432/artemis_kb_test"
)


class TopicEmbedder:
    """Deterministic 4-dim vectors, one axis per topic in the sample catalogue."""

    model = "topic-test"
    DIM = 4

    def __init__(self) -> None:
        self.document_calls = 0

    def _vector(self, text: str) -> list[float]:
        folded = normalize(text)
        vector = [0.0] * self.DIM
        if "dang xuat" in folded or "thoat" in folded or "logout" in folded:
            vector[0] = 1.0
        if "thiet bi" in folded:
            vector[1] = 1.0
        if "thong bao" in folded:
            vector[2] = 1.0
        if not any(vector):
            vector[3] = 1.0
        return vector

    def embed_documents(self, texts):
        self.document_calls += 1
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)


def _logout(**kwargs) -> Workflow:
    payload = {
        "id": "auth.logout",
        "app_id": "hunonic",
        "name": "Đăng xuất tài khoản",
        "description": "Đăng xuất khỏi tài khoản đang đăng nhập",
        "aliases": ["logout", "đăng xuất", "thoát tài khoản"],
        "tags": ["auth"],
        "preconditions": ["Người dùng đã đăng nhập"],
        "steps": [
            WorkflowStep(
                action="tap", target="Tab cuối cùng trên bottom navigation",
                expected_state="Màn hình cá nhân",
            ),
            WorkflowStep(action="scroll", direction="down", until="Nút Đăng xuất xuất hiện"),
            WorkflowStep(action="tap", target="Đăng xuất"),
            WorkflowStep(action="confirm_if_present", target="Xác nhận"),
        ],
        "success_criteria": ["Màn hình đăng nhập xuất hiện"],
        "status": "verified",
    }
    payload.update(kwargs)
    return Workflow(**payload)


@pytest.fixture(scope="module")
def store():
    psycopg = pytest.importorskip("psycopg", reason="psycopg chưa cài")
    admin_dsn, dbname = TEST_DSN.rsplit("/", 1)
    try:
        with psycopg.connect(f"{admin_dsn}/postgres", autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
                if cur.fetchone() is None:
                    cur.execute(f'CREATE DATABASE "{dbname}"')
    except Exception as exc:  # noqa: BLE001 - no database means "cannot test here"
        pytest.skip(f"PostgreSQL không sẵn sàng: {exc}")

    from artemis.knowledge.stores.postgres import PostgresKnowledgeStore

    store = PostgresKnowledgeStore(TEST_DSN)
    store.migrate()
    if not store.ping():
        pytest.skip("Extension pgvector chưa được bật trong database test")
    return store


@pytest.fixture
def clean_store(store):
    """Empty catalogue before each test: every test states its own world."""
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE kb_workflow CASCADE")
        cur.execute("TRUNCATE kb_app CASCADE")
        cur.execute("TRUNCATE kb_retrieval_log")
    return store


@pytest.fixture
def service(clean_store):
    return KnowledgeService(store=clean_store, embedder=TopicEmbedder(), top_k=3)


# ── storage ──────────────────────────────────────────────────────────────
def test_save_and_read_back_a_workflow(clean_store):
    saved = clean_store.save_workflow(_logout())
    assert saved.revision_no == 1

    loaded = clean_store.get_workflow("auth.logout")
    assert loaded is not None
    assert loaded.name == "Đăng xuất tài khoản"
    assert loaded.aliases == ["logout", "đăng xuất", "thoát tài khoản"]
    assert [step.action for step in loaded.steps] == [
        "tap", "scroll", "tap", "confirm_if_present"
    ]
    assert loaded.steps[1].direction == "down"
    assert loaded.preconditions == ["Người dùng đã đăng nhập"]
    assert loaded.success_criteria == ["Màn hình đăng nhập xuất hiện"]
    assert loaded.status == "verified"


def test_saving_twice_bumps_the_revision_and_keeps_history(clean_store):
    clean_store.save_workflow(_logout())
    clean_store.save_workflow(_logout(name="Đăng xuất (đổi tên)"), change_note="sửa tên")
    loaded = clean_store.get_workflow("auth.logout")
    assert loaded.revision_no == 2
    assert loaded.name == "Đăng xuất (đổi tên)"

    revisions = clean_store.revisions("auth.logout")
    assert [row["revision_no"] for row in revisions] == [2, 1]
    assert revisions[0]["change_note"] == "sửa tên"
    assert revisions[1]["snapshot"]["name"] == "Đăng xuất tài khoản"


def test_replacing_a_workflow_does_not_leave_orphan_children(clean_store):
    clean_store.save_workflow(_logout())
    clean_store.save_workflow(_logout(aliases=["logout"], steps=[WorkflowStep(action="tap", target="X")]))
    loaded = clean_store.get_workflow("auth.logout")
    assert loaded.aliases == ["logout"]
    assert len(loaded.steps) == 1


def test_delete_removes_the_workflow_and_its_children(clean_store):
    clean_store.save_workflow(_logout())
    assert clean_store.delete_workflow("auth.logout") is True
    assert clean_store.get_workflow("auth.logout") is None
    with clean_store._connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM kb_workflow_step")
        assert cur.fetchone()[0] == 0


def test_apps_and_versions_are_listed(clean_store):
    clean_store.upsert_app("hunonic", "Hunonic", "com.hunonic.app")
    clean_store.upsert_version("hunonic", "3.2.0")
    clean_store.save_workflow(_logout())
    assert clean_store.list_apps()[0]["workflow_count"] == 1
    assert clean_store.list_versions("hunonic")[0]["version_label"] == "3.2.0"


# ── scoping: the failure that matters most ───────────────────────────────
def test_a_workflow_is_never_served_to_another_app(clean_store):
    """Following another app's instructions would drive a UI that is not on screen."""
    clean_store.upsert_app("other", "App khác", "com.other.app")
    clean_store.save_workflow(_logout())
    clean_store.save_workflow(_logout(id="auth.logout.other", app_id="other", name="Đăng xuất (app khác)"))

    result = KnowledgeService(store=clean_store, embedder=TopicEmbedder()).retrieve(
        "Test đăng xuất", "hunonic"
    )
    assert [hit.workflow.app_id for hit in result.hits] == ["hunonic"]
    assert all(hit.workflow.id != "auth.logout.other" for hit in result.hits)


def test_version_scoped_workflow_is_not_served_to_another_version(clean_store):
    clean_store.upsert_version("hunonic", "2.0.0")
    clean_store.upsert_version("hunonic", "3.0.0")
    versions = {row["version_label"]: row["id"] for row in clean_store.list_versions("hunonic")}
    clean_store.save_workflow(_logout(version_id=versions["2.0.0"], id="auth.logout.v2"))
    clean_store.save_workflow(_logout(version_id=versions["3.0.0"], id="auth.logout.v3"))

    service = KnowledgeService(store=clean_store, embedder=TopicEmbedder())
    got_v3 = service.retrieve("Test đăng xuất", "hunonic", version_id=versions["3.0.0"])
    assert [hit.workflow.id for hit in got_v3.hits] == ["auth.logout.v3"]
    got_v2 = service.retrieve("Test đăng xuất", "hunonic", version_id=versions["2.0.0"])
    assert [hit.workflow.id for hit in got_v2.hits] == ["auth.logout.v2"]


def test_version_agnostic_workflow_is_served_to_every_version(clean_store):
    clean_store.upsert_version("hunonic", "3.0.0")
    version_id = clean_store.list_versions("hunonic")[0]["id"]
    clean_store.save_workflow(_logout())
    result = KnowledgeService(store=clean_store, embedder=TopicEmbedder()).retrieve(
        "Test đăng xuất", "hunonic", version_id=version_id
    )
    assert result.found is True


def test_deprecated_workflow_is_not_retrieved(clean_store):
    clean_store.save_workflow(_logout(status="deprecated"))
    result = KnowledgeService(store=clean_store, embedder=TopicEmbedder()).retrieve(
        "Test đăng xuất", "hunonic"
    )
    assert result.found is False
    assert result.hits == []


def test_draft_workflow_is_retrieved_so_a_tester_can_try_it(clean_store):
    clean_store.save_workflow(_logout(status="draft"))
    assert KnowledgeService(store=clean_store, embedder=TopicEmbedder()).retrieve(
        "Test đăng xuất", "hunonic"
    ).found is True


# ── the pipeline ─────────────────────────────────────────────────────────
def test_acceptance_test_dang_xuat(service, clean_store):
    """The acceptance case: "Test đăng xuất" retrieves the logout workflow."""
    clean_store.save_workflow(_logout())
    result, context = service.context_for("Test đăng xuất", "hunonic")

    assert result.found is True
    assert result.hits[0].workflow.id == "auth.logout"
    assert result.hits[0].trusted is True
    assert "auth.logout" in context
    assert "1. [chạm vào] Tab cuối cùng trên bottom navigation" in context
    assert "Màn hình đăng nhập xuất hiện" in context


def test_a_paraphrase_still_finds_the_workflow(service, clean_store):
    clean_store.save_workflow(_logout())
    result = service.retrieve("thoát khỏi tài khoản đang dùng", "hunonic")
    assert result.found is True
    assert result.hits[0].workflow.id == "auth.logout"


def test_an_unknown_function_reports_no_guidance(service, clean_store):
    clean_store.save_workflow(_logout())
    result, context = service.context_for("chụp ảnh màn hình", "hunonic")
    assert result.found is False
    # The block must say outright that nothing was confirmed, and any surviving
    # hit must be labelled rather than presented as instructions.
    assert "Không có workflow nào được xác nhận" in context
    assert all(hit.trusted is False for hit in result.hits)
    assert "chưa đủ tin cậy" in result.reason


def test_an_empty_catalogue_reports_no_guidance(service):
    result, context = service.context_for("Test đăng xuất", "hunonic")
    assert result.found is False and result.hits == []
    assert "KHÔNG có workflow nào" in context


def test_retrieval_is_recorded_for_the_ui(service, clean_store):
    clean_store.save_workflow(_logout())
    service.retrieve("Test đăng xuất", "hunonic", run_id="run-42")
    logs = clean_store.list_retrieval_logs(run_id="run-42")
    assert len(logs) == 1
    assert logs[0]["chosen_workflow_id"] == "auth.logout"
    assert logs[0]["retrieved"][0]["workflow_id"] == "auth.logout"
    assert logs[0]["timings_ms"]
    assert "auth.logout" in logs[0]["context_sent"]


# ── embedding lifecycle ──────────────────────────────────────────────────
def test_embeddings_are_created_once_and_then_reused(service, clean_store):
    clean_store.save_workflow(_logout())
    first = service.ensure_embeddings()
    assert first["embedded"] == 1
    second = service.ensure_embeddings()
    assert second == {"embedded": 0, "reused": 1, "stale": 0}


def test_editing_a_workflow_re_embeds_only_that_workflow(service, clean_store):
    clean_store.save_workflow(_logout())
    clean_store.save_workflow(_logout(id="device.add", name="Thêm thiết bị mới"))
    service.ensure_embeddings()

    clean_store.save_workflow(_logout(name="Đăng xuất tài khoản (sửa)"))
    result = service.ensure_embeddings()
    assert result["embedded"] == 1 and result["reused"] == 1


def test_embedding_metadata_records_the_model_and_dimension(service, clean_store):
    clean_store.save_workflow(_logout())
    service.ensure_embeddings()
    rows = clean_store.load_embeddings(TopicEmbedder.model)
    assert rows["auth.logout"].dim == TopicEmbedder.DIM
    assert rows["auth.logout"].content_hash


def test_reindex_rebuilds_every_vector(service, clean_store):
    clean_store.save_workflow(_logout())
    service.ensure_embeddings()
    result = service.reindex()
    assert result["dropped"] == 1 and result["embedded"] == 1


def test_an_embedding_outage_leaves_lexical_retrieval_working(service, clean_store):
    clean_store.save_workflow(_logout())
    service._embedder = None  # simulate "no key / provider down"
    result = service.retrieve("Test đăng xuất", "hunonic")
    assert result.found is True
    assert "vector" not in result.hits[0].channels
    assert service.ensure_embeddings()["error"] == "không có embedder"


def test_an_alias_only_match_is_trusted_without_any_vector(clean_store):
    """Aliases are the tester's own vocabulary; they must work on their own."""
    clean_store.save_workflow(_logout())
    service = KnowledgeService(store=clean_store, embedder=None)
    result = service.retrieve("logout", "hunonic")
    assert result.found is True
    assert "exact" in result.hits[0].channels
    assert "vector" not in result.hits[0].channels


# ── scoping in the vector channel specifically ───────────────────────────
def test_vector_channel_respects_the_app_scope(service, clean_store):
    clean_store.upsert_app("other", "App khác", "com.other.app")
    clean_store.save_workflow(_logout(id="auth.logout.other", app_id="other", name="Đăng xuất (khác)"))
    service.ensure_embeddings()

    rows = clean_store.search_vector(
        TopicEmbedder()._vector("đăng xuất"), TopicEmbedder.model, "hunonic", None
    )
    assert rows == []


def test_keyword_channel_respects_the_app_scope(clean_store):
    clean_store.upsert_app("other", "App khác", "com.other.app")
    clean_store.save_workflow(_logout(id="auth.logout.other", app_id="other", name="Đăng xuất (khác)"))
    assert clean_store.search_keyword("đăng xuất", "hunonic", None) == []
    assert clean_store.search_keyword("đăng xuất", "other", None)


# ── the whole flow: package -> retrieval -> context -> Planner prompt ────
def _planner_prompts() -> dict:
    import json
    from pathlib import Path

    path = Path("artemis/agents/planner/planner.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _planner_prompts_for(ctx) -> tuple[str, str]:
    """Build the Planner's (system, human) exactly as ``PlannerNode`` does.

    Mirrors ``artemis/agents/planner/planner.py``: read the block off the
    context, mount the knowledge block only when there is one, then render the
    human message.  Kept in step with that code by
    ``tests/unit/agents/test_planner_knowledge_prompt.py``.
    """
    from artemis.agents.planner.planner import (
        build_planner_system_blocks,
        render_planner_human,
    )
    from artemis.knowledge.integration import context_text

    prompts = _planner_prompts()
    knowledge = context_text(ctx)
    blocks = build_planner_system_blocks(
        prompts, "initial_plan", include_checks=False, include_knowledge=bool(knowledge)
    )
    system = "\n\n".join(prompts["blocks"][block] for block in blocks)
    human = render_planner_human(
        prompts["modes"]["initial_plan"]["human"], "Test đăng xuất", knowledge
    )
    return system, human


def test_the_acceptance_flow_puts_the_workflow_in_front_of_the_planner(service, clean_store):
    """The acceptance test's core claim, minus the device.

    "Test đăng xuất" -> the right workflow is retrieved -> its steps reach the
    Planner's prompt -> the Planner is told how to use them.
    """
    from artemis.context import ArtemisContext, DeviceContext
    from artemis.knowledge.integration import attach, retrieve_for_run

    clean_store.upsert_app("hunonic", "Hunonic", "com.hunonic.app")
    clean_store.save_workflow(_logout())

    knowledge = retrieve_for_run(
        "Test đăng xuất", app_package="com.hunonic.app", run_id="run-acceptance", service=service
    )
    assert knowledge.trusted_ids == ["auth.logout"]
    assert knowledge.app_id == "hunonic"

    ctx = ArtemisContext(device=DeviceContext(device_id="integration", device_width=1080, device_height=2400))
    attach(ctx, knowledge)
    system, human = _planner_prompts_for(ctx)

    # The Planner's human message carries the steps and the success criteria.
    assert human.startswith("Goal: Test đăng xuất")
    assert "auth.logout" in human
    assert "1. [chạm vào] Tab cuối cùng trên bottom navigation" in human
    assert "2. [cuộn] (down) cho tới khi thấy: Nút Đăng xuất xuất hiện" in human
    assert "Màn hình đăng nhập xuất hiện" in human
    # ...and the system prompt teaches how to treat it.
    assert "Reference Guidance From the App's Knowledge Base" in system


def test_a_run_without_knowledge_gets_the_original_planner_prompt(service, clean_store):
    """Requirement: with no workflow, ARTEMIS behaves exactly as before."""
    from artemis.context import ArtemisContext, DeviceContext
    from artemis.knowledge.integration import attach, retrieve_for_run

    clean_store.upsert_app("hunonic", "Hunonic", "com.hunonic.app")
    clean_store.save_workflow(_logout())

    knowledge = retrieve_for_run(
        "chụp ảnh màn hình", app_package="com.hunonic.app", service=service
    )
    assert knowledge.injected is False  # a guess alone is not injected

    ctx = ArtemisContext(device=DeviceContext(device_id="integration"))
    attach(ctx, knowledge)
    system, human = _planner_prompts_for(ctx)

    assert human == "Goal: Test đăng xuất"
    assert "Reference Guidance From the App's Knowledge Base" not in system
    assert "Tab cuối cùng trên bottom navigation" not in human


def test_an_unregistered_package_gets_no_knowledge(service, clean_store):
    from artemis.knowledge.integration import retrieve_for_run

    clean_store.upsert_app("hunonic", "Hunonic", "com.hunonic.app")
    clean_store.save_workflow(_logout())

    knowledge = retrieve_for_run("Test đăng xuất", app_package="com.khac.app", service=service)
    assert knowledge.injected is False
    assert "com.khac.app" in knowledge.reason


def test_the_run_outcome_is_recorded_against_the_retrieval(service, clean_store):
    """Requirement: log the retrieved workflow *and* how it turned out."""
    from artemis.knowledge.integration import record_outcome, retrieve_for_run, usage_report

    clean_store.upsert_app("hunonic", "Hunonic", "com.hunonic.app")
    clean_store.save_workflow(_logout())
    retrieve_for_run("Test đăng xuất", "com.hunonic.app", run_id="run-out", service=service)

    updated = record_outcome(
        "run-out",
        {"task_status": "completed", "tests": {"passed": 2, "failed": 0, "inconclusive": 0}},
        service=service,
    )
    assert updated == 1

    report = usage_report("run-out", service=service)
    assert report["degraded"] is False
    log = report["logs"][0]
    assert log["chosen_workflow_id"] == "auth.logout"
    assert log["outcome"]["task_status"] == "completed"
    assert log["outcome"]["tests"]["passed"] == 2


def test_the_app_package_maps_to_the_knowledge_base_app(clean_store):
    clean_store.upsert_app("hunonic", "Hunonic", "com.hunonic.app")
    assert clean_store.find_app_by_package("com.hunonic.app") == "hunonic"
    assert clean_store.find_app_by_package("hunonic") == "hunonic"
    assert clean_store.find_app_by_package("com.khong.co") is None


# ── the console's Knowledge tab, driven exactly as the page drives it ────
@pytest.fixture
def ui(clean_store, monkeypatch):
    """HTTP client over the real router, with the real PostgreSQL store.

    The payloads below are copied from ``apps/test_console/static/index.html``
    on purpose: this is the contract between the hand-written UI and the API, and
    it is the part a typo would silently break.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from artemis.knowledge import api as api_module

    service = KnowledgeService(store=clean_store, embedder=TopicEmbedder(), top_k=3)
    monkeypatch.setattr(api_module, "get_service", lambda: service)

    app = FastAPI()
    app.include_router(api_module.router)
    return TestClient(app)


def _ui_workflow_payload() -> dict:
    """Exactly the body ``saveKbWorkflow()`` builds."""
    return {
        "app_id": "hunonic",
        "name": "Đăng xuất tài khoản",
        "description": "Đăng xuất khỏi tài khoản đang đăng nhập",
        "aliases": ["logout", "đăng xuất", "thoát tài khoản"],
        "tags": ["auth"],
        "preconditions": ["Người dùng đã đăng nhập"],
        "steps": [
            {"action": "tap", "target": "Tab cuối cùng trên bottom navigation",
             "expected_state": "Màn hình cá nhân"},
            {"action": "scroll", "direction": "down", "until": "Nút Đăng xuất xuất hiện"},
            {"action": "tap", "target": "Đăng xuất"},
            {"action": "confirm_if_present", "target": "Xác nhận"},
        ],
        "success_criteria": ["Màn hình đăng nhập xuất hiện"],
        "status": "verified",
        "version_id": None,
        "change_note": "",
    }


def test_the_ui_flow_creates_edits_and_removes_a_workflow(ui, clean_store):
    clean_store.upsert_app("hunonic", "Hunonic", "com.hunonic.app")

    # 1. Trạng thái + danh sách app (loadKb)
    status = ui.get("/api/kb/status").json()
    assert status["available"] is True and status["workflow_count"] == 0
    assert ui.get("/api/kb/apps").json()[0]["id"] == "hunonic"
    assert ui.get("/api/kb/apps/hunonic/versions").json() == []

    # 2. Lưu workflow mới (saveKbWorkflow)
    saved = ui.put("/api/kb/workflows/auth.logout", json=_ui_workflow_payload()).json()
    assert saved["revision_no"] == 1
    assert saved["embedded"] is True
    assert [step["action"] for step in saved["steps"]] == [
        "tap", "scroll", "tap", "confirm_if_present"
    ]

    # 3. Danh sách cho app đang chọn (loadKbWorkflows)
    rows = ui.get("/api/kb/workflows?app_id=hunonic").json()
    assert [row["id"] for row in rows] == ["auth.logout"]
    assert rows[0]["aliases"] == ["logout", "đăng xuất", "thoát tài khoản"]

    # 4. Sửa (editKbWorkflow -> saveKbWorkflow)
    edited = dict(_ui_workflow_payload(), name="Đăng xuất (sửa)", change_note="đổi tên")
    again = ui.put("/api/kb/workflows/auth.logout", json=edited).json()
    assert again["revision_no"] == 2
    assert ui.get("/api/kb/workflows/auth.logout/revisions").json()[0]["change_note"] == "đổi tên"

    # 5. Kiểm tra truy xuất (testKbRetrieve)
    probe = ui.post(
        "/api/kb/retrieve", json={"query": "Test đăng xuất", "app_id": "hunonic"}
    ).json()
    assert probe["found"] is True
    assert probe["hits"][0]["workflow_id"] == "auth.logout"
    assert probe["hits"][0]["trusted"] is True
    assert probe["hits"][0]["channels"]
    assert "Tab cuối cùng trên bottom navigation" in probe["context"]

    # 6. Kiến thức đã dùng (loadKbLogs)
    logs = ui.get("/api/kb/logs?limit=20").json()
    assert logs and logs[0]["chosen_workflow_id"] == "auth.logout"

    # 7. Xoá (deleteKbWorkflow)
    assert ui.delete("/api/kb/workflows/auth.logout").json()["deleted"] is True
    assert ui.get("/api/kb/workflows?app_id=hunonic").json() == []


def test_the_ui_gets_a_readable_message_for_a_bad_step(ui, clean_store):
    """A 422 must explain itself; the page shows whatever this returns."""
    clean_store.upsert_app("hunonic", "Hunonic", "com.hunonic.app")
    payload = dict(_ui_workflow_payload(), steps=[{"action": "teleport"}])
    response = ui.put("/api/kb/workflows/bad.wf", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"] == ["steps", 0, "action"] for item in detail)
    assert "action" in detail[0]["msg"]


def test_the_ui_can_save_a_workflow_while_the_knowledge_base_is_down(ui, monkeypatch):
    """The tab must report the outage instead of appearing to work."""
    from artemis.knowledge import api as api_module

    monkeypatch.setattr(api_module, "get_service", lambda: KnowledgeService())
    assert ui.get("/api/kb/status").json()["available"] is False
    response = ui.get("/api/kb/workflows")
    assert response.status_code == 503
    assert response.json()["detail"]["degraded"] is True

