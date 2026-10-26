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

"""HTTP contract of the knowledge base.

The console UI drives these endpoints, so the two things that must hold are:
a validation mistake comes back as 422 with a message a tester can act on, and a
knowledge-base outage comes back as 503 with ``degraded`` set -- never as a 500,
because the UI has to be able to say "the KB is down" instead of "the console
broke".
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from artemis.knowledge import api
from artemis.knowledge.models import Workflow
from artemis.knowledge.service import KnowledgeService
from artemis.knowledge.stores.base import KnowledgeUnavailable


def _workflow(workflow_id: str = "auth.logout", **kwargs) -> Workflow:
    payload = {
        "id": workflow_id,
        "app_id": "hunonic",
        "name": "Đăng xuất tài khoản",
        "aliases": ["logout"],
        "steps": [{"action": "tap", "target": "Tab cuối cùng"}],
        "status": "verified",
    }
    payload.update(kwargs)
    return Workflow(**payload)


class FakeStore:
    def __init__(self, workflows=(), *, fail=False, coverage=1.0, exact=None):
        self.workflows = {workflow.id: workflow for workflow in workflows}
        self.fail = fail
        self.coverage = coverage
        self.exact = list(exact) if exact is not None else [w.id for w in workflows]
        self.embeddings = {}
        self.logs = []

    def _guard(self):
        if self.fail:
            raise KnowledgeUnavailable("database is down")

    def list_workflows(self, app_id=None, status=None):
        self._guard()
        return list(self.workflows.values())

    def get_workflow(self, workflow_id):
        self._guard()
        return self.workflows.get(workflow_id)

    def save_workflow(self, workflow, change_note=""):
        self._guard()
        self.workflows[workflow.id] = workflow
        return workflow

    def delete_workflow(self, workflow_id):
        self._guard()
        return self.workflows.pop(workflow_id, None) is not None

    def revisions(self, workflow_id):
        self._guard()
        return [{"revision_no": 1, "change_note": "", "created_at": "", "snapshot": {}}]

    def list_apps(self):
        self._guard()
        return [{"id": "hunonic", "name": "Hunonic", "package_name": None, "workflow_count": 1}]

    def list_versions(self, app_id):
        self._guard()
        return [{"id": "hunonic@3.2.0", "app_id": app_id, "version_label": "3.2.0", "notes": ""}]

    def upsert_app(self, app_id, name, package_name=None):
        self._guard()
        return {"id": app_id, "name": name, "package_name": package_name}

    def upsert_version(self, app_id, version_label, version_id=None, notes=""):
        self._guard()
        return {"id": version_id or f"{app_id}@{version_label}", "app_id": app_id,
                "version_label": version_label, "notes": notes}

    def list_retrieval_logs(self, run_id=None, limit=50):
        self._guard()
        return [{"id": 1, "run_id": run_id, "query": "q"}]

    def search_exact(self, query, app_id, version_id, limit=50):
        self._guard()
        # Empty by default: a fake that always matched would mask exactly the
        # decision under test (a lexical match is evidence, a guess is not).
        return list(self.exact)

    def search_keyword(self, query, app_id, version_id, limit=50):
        self._guard()
        return [(workflow.id, self.coverage) for workflow in self.workflows.values()]

    def search_vector(self, vector, model, app_id, version_id, limit=50):
        self._guard()
        return []

    def load_embeddings(self, model, app_id=None, version_id=None):
        self._guard()
        return {}

    def save_embedding(self, workflow_id, model, content_hash, vector):
        self.embeddings[workflow_id] = vector

    def delete_embeddings(self, model):
        self._guard()
        count = len(self.embeddings)
        self.embeddings.clear()
        return count

    def log_retrieval(self, entry):
        self.logs.append(entry)


class FakeEmbedder:
    model = "fake"

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


@pytest.fixture
def client(monkeypatch):
    def _client(store, **service_kwargs):
        service = KnowledgeService(
            store=store, embedder=FakeEmbedder(), **service_kwargs
        )
        monkeypatch.setattr(api, "get_service", lambda: service)
        app = FastAPI()
        app.include_router(api.router)
        return TestClient(app), store

    return _client


# ── health ───────────────────────────────────────────────────────────────
def test_status_reports_availability_and_count(client):
    http, _ = client(FakeStore([_workflow()]))
    body = http.get("/api/kb/status").json()
    assert body["available"] is True
    assert body["workflow_count"] == 1
    assert body["embedding_model"] == "fake"


def test_status_of_a_disabled_service_is_still_200(client, monkeypatch):
    """Health must be answerable precisely when the KB is unusable."""
    import artemis.knowledge.api as api_module

    monkeypatch.setattr(api_module, "get_service", lambda: KnowledgeService())
    body = TestClient(_app()).get("/api/kb/status").json()
    assert body["available"] is False
    assert body["workflow_count"] == 0


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(api.router)
    return app


# ── CRUD ─────────────────────────────────────────────────────────────────
def test_list_and_get_workflows(client):
    http, _ = client(FakeStore([_workflow()]))
    assert http.get("/api/kb/workflows?app_id=hunonic").json()[0]["id"] == "auth.logout"
    assert http.get("/api/kb/workflows/auth.logout").json()["name"] == "Đăng xuất tài khoản"


def test_get_a_missing_workflow_is_404(client):
    http, _ = client(FakeStore([]))
    assert http.get("/api/kb/workflows/nope").status_code == 404


def test_saving_a_workflow_indexes_it(client):
    http, store = client(FakeStore([]))
    response = http.put(
        "/api/kb/workflows/auth.logout",
        json={
            "app_id": "hunonic",
            "name": "Đăng xuất tài khoản",
            "aliases": ["logout"],
            "steps": [{"action": "tap", "target": "Tab cuối cùng"}],
            "status": "verified",
        },
    )
    assert response.status_code == 200
    assert response.json()["embedded"] is True
    assert "auth.logout" in store.embeddings


def test_an_invalid_step_action_is_422_with_a_reason(client):
    http, _ = client(FakeStore([]))
    response = http.put(
        "/api/kb/workflows/auth.logout",
        json={"app_id": "hunonic", "name": "X", "steps": [{"action": "teleport"}]},
    )
    assert response.status_code == 422
    assert "action" in str(response.json()["detail"])


def test_a_malformed_workflow_id_is_422(client):
    http, _ = client(FakeStore([]))
    response = http.put(
        "/api/kb/workflows/Auth%20Logout", json={"app_id": "hunonic", "name": "X"}
    )
    assert response.status_code == 422


def test_delete_reports_404_when_absent(client):
    http, _ = client(FakeStore([]))
    assert http.delete("/api/kb/workflows/nope").status_code == 404
    http, _ = client(FakeStore([_workflow()]))
    assert http.delete("/api/kb/workflows/auth.logout").json()["deleted"] is True


def test_apps_and_versions_are_listed(client):
    http, _ = client(FakeStore([]))
    assert http.get("/api/kb/apps").json()[0]["id"] == "hunonic"
    assert http.get("/api/kb/apps/hunonic/versions").json()[0]["version_label"] == "3.2.0"


def test_revisions_are_exposed(client):
    http, _ = client(FakeStore([_workflow()]))
    assert http.get("/api/kb/workflows/auth.logout/revisions").json()[0]["revision_no"] == 1


# ── retrieval preview ────────────────────────────────────────────────────
def test_retrieve_returns_hits_and_the_context_that_would_be_injected(client):
    http, _ = client(FakeStore([_workflow()]))
    body = http.post(
        "/api/kb/retrieve", json={"query": "Test đăng xuất", "app_id": "hunonic"}
    ).json()
    assert body["found"] is True
    assert body["hits"][0]["trusted"] is True
    assert body["hits"][0]["channels"] == {"exact": 1, "keyword": 1}
    assert "auth.logout" in body["context"]


def test_retrieve_of_an_unknown_function_says_so(client):
    """A partial keyword match must not be reported as a confirmed workflow."""
    http, _ = client(FakeStore([_workflow()], coverage=0.5, exact=[]))
    body = http.post(
        "/api/kb/retrieve", json={"query": "chụp ảnh màn hình", "app_id": "hunonic"}
    ).json()
    assert body["found"] is False
    assert body["hits"][0]["trusted"] is False
    assert "Không có workflow nào được xác nhận" in body["context"]


def test_logs_are_listed(client):
    http, _ = client(FakeStore([]))
    assert http.get("/api/kb/logs?run_id=run-1").json()[0]["run_id"] == "run-1"


def test_reindex_reports_what_it_did(client):
    http, store = client(FakeStore([_workflow()]))
    store.embeddings["auth.logout"] = [1.0, 0.0]
    body = http.post("/api/kb/reindex").json()
    assert body["dropped"] == 1 and body["embedded"] == 1


# ── degradation ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/kb/apps"),
        ("get", "/api/kb/workflows"),
        ("get", "/api/kb/workflows/auth.logout"),
        ("get", "/api/kb/logs"),
    ],
)
def test_a_store_outage_is_503_not_500(client, method, path):
    http, _ = client(FakeStore([_workflow()], fail=True))
    response = getattr(http, method)(path)
    assert response.status_code == 503
    assert response.json()["detail"]["degraded"] is True


def test_a_disabled_knowledge_base_answers_503_with_the_reason(client, monkeypatch):
    import artemis.knowledge.api as api_module

    disabled = KnowledgeService()
    monkeypatch.setattr(api_module, "get_service", lambda: disabled)
    response = TestClient(_app()).get("/api/kb/workflows")
    assert response.status_code == 503
    assert response.json()["detail"]["degraded"] is True
