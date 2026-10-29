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

"""The knowledge service: trust decisions, caching, and degradation.

Every failure path here must end in "no guidance", never in an exception.  A
knowledge base that can take down a test run is worse than no knowledge base, so
the degradation cases are the ones that matter most.
"""

import pytest

from artemis.knowledge.embeddings import EmbeddingError
from artemis.knowledge.models import Workflow, WorkflowStep
from artemis.knowledge.service import KnowledgeService, content_hash
from artemis.knowledge.stores.base import EmbeddingRow, KnowledgeUnavailable


def _workflow(workflow_id: str = "auth.logout", **kwargs) -> Workflow:
    payload = {
        "id": workflow_id,
        "app_id": "hunonic",
        "name": "Đăng xuất tài khoản",
        "aliases": ["logout"],
        "steps": [WorkflowStep(action="tap", target="Tab cuối cùng")],
        "status": "verified",
    }
    payload.update(kwargs)
    return Workflow(**payload)


class FakeStore:
    """Minimal in-memory store: enough to exercise the service's decisions."""

    def __init__(
        self,
        workflows=(),
        *,
        exact=(),
        keyword=(),
        vector=(),
        fail=False,
        saved_embeddings=None,
    ):
        self.workflows = {workflow.id: workflow for workflow in workflows}
        self.exact = list(exact)
        self.keyword = list(keyword)
        self.vector = list(vector)
        self.fail = fail
        self.logs = []
        self.embeddings = dict(saved_embeddings or {})

    def _guard(self):
        if self.fail:
            raise KnowledgeUnavailable("database is down")

    def search_exact(self, query, app_id, version_id, limit=50, filters=None):
        self._guard()
        return list(self.exact)

    def search_keyword(self, query, app_id, version_id, limit=50, filters=None):
        self._guard()
        return list(self.keyword)

    def search_vector(self, vector, model, app_id, version_id, limit=50, filters=None):
        self._guard()
        return list(self.vector)

    def get_workflow(self, workflow_id):
        self._guard()
        return self.workflows.get(workflow_id)

    def list_workflows(self, app_id=None, status=None):
        self._guard()
        return list(self.workflows.values())

    def load_embeddings(self, model, app_id=None, version_id=None):
        self._guard()
        return dict(self.embeddings)

    def save_embedding(self, workflow_id, model, content_hash_, vector):
        self.embeddings[workflow_id] = EmbeddingRow(
            workflow_id=workflow_id,
            model=model,
            dim=len(vector),
            content_hash=content_hash_,
            vector=vector,
        )

    def delete_embeddings(self, model):
        dropped = len(self.embeddings)
        self.embeddings.clear()
        return dropped

    def log_retrieval(self, entry):
        self.logs.append(entry)


class FakeEmbedder:
    model = "fake-embedding"

    def __init__(self, fail=False):
        self.fail = fail
        self.document_calls = []
        self.query_calls = []

    def embed_documents(self, texts):
        if self.fail:
            raise EmbeddingError("provider down")
        self.document_calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        if self.fail:
            raise EmbeddingError("provider down")
        self.query_calls.append(text)
        return [1.0, 0.0]


def _service(store, embedder=None, **kwargs):
    return KnowledgeService(store=store, embedder=embedder or FakeEmbedder(), **kwargs)


# ── trust decisions ──────────────────────────────────────────────────────
def test_exact_hit_is_trusted():
    store = FakeStore([_workflow()], exact=["auth.logout"])
    result = _service(store).retrieve("Test đăng xuất", "hunonic")
    assert result.found is True
    assert result.hits[0].trusted is True
    assert result.hits[0].channels == {"exact": 1}


def test_keyword_hit_below_the_coverage_bar_is_not_trusted():
    """'chụp ảnh màn hình' shares half its words with the logout workflow."""
    store = FakeStore([_workflow()], keyword=[("auth.logout", 0.5)])
    result = _service(store).retrieve("chụp ảnh màn hình", "hunonic")
    assert result.found is False
    assert result.hits[0].trusted is False
    assert "chưa đủ tin cậy" in result.reason


def test_keyword_hit_at_the_coverage_bar_is_trusted():
    store = FakeStore([_workflow()], keyword=[("auth.logout", 0.6)])
    assert _service(store).retrieve("đăng xuất", "hunonic").found is True


def test_coverage_bar_is_configurable():
    store = FakeStore([_workflow()], keyword=[("auth.logout", 0.5)])
    service = _service(store, keyword_min_coverage=0.4)
    assert service.retrieve("chụp ảnh màn hình", "hunonic").found is True


def test_vector_only_never_declares_a_match():
    store = FakeStore([_workflow()], vector=[("auth.logout", 0.71)])
    result = _service(store).retrieve("chụp ảnh màn hình", "hunonic")
    assert result.found is False
    assert result.hits[0].trusted is False


def test_at_most_one_guess_rides_alongside_trusted_hits():
    store = FakeStore(
        [_workflow(), _workflow("device.add"), _workflow("other.wf")],
        exact=["auth.logout"],
        vector=[("device.add", 0.7), ("other.wf", 0.6)],
    )
    result = _service(store).retrieve("Test đăng xuất", "hunonic")
    assert [hit.trusted for hit in result.hits] == [True, False]
    assert len(result.hits) == 2


def test_top_k_caps_the_trusted_hits():
    store = FakeStore(
        [_workflow("a"), _workflow("b"), _workflow("c")],
        exact=["a", "b", "c"],
    )
    result = _service(store, top_k=2).retrieve("đăng xuất", "hunonic")
    assert [hit.workflow.id for hit in result.hits] == ["a", "b"]


def test_a_hit_whose_workflow_vanished_is_skipped():
    store = FakeStore([], exact=["ghost"])
    result = _service(store).retrieve("đăng xuất", "hunonic")
    assert result.hits == [] and result.found is False


# ── degradation ──────────────────────────────────────────────────────────
def test_store_failure_degrades_instead_of_raising():
    store = FakeStore([_workflow()], exact=["auth.logout"], fail=True)
    result = _service(store).retrieve("Test đăng xuất", "hunonic")
    assert result.degraded is True
    assert result.found is False and result.hits == []
    assert "Knowledge base lỗi" in result.reason


def test_embedding_failure_keeps_the_lexical_channels():
    store = FakeStore([_workflow()], exact=["auth.logout"])
    result = _service(store, FakeEmbedder(fail=True)).retrieve("Test đăng xuất", "hunonic")
    assert result.found is True
    assert "vector" not in result.hits[0].channels


def test_disabled_service_reports_why():
    service = KnowledgeService()
    assert service.available is False
    result = service.retrieve("Test đăng xuất", "hunonic")
    assert result.degraded is True and result.found is False
    assert service.status()["available"] is False


def test_vector_search_failure_does_not_lose_a_lexical_hit():
    class FlakyVectorStore(FakeStore):
        def search_vector(self, vector, model, app_id, version_id, limit=50, filters=None):
            raise KnowledgeUnavailable("pgvector exploded")

    store = FlakyVectorStore([_workflow()], exact=["auth.logout"])
    result = _service(store).retrieve("Test đăng xuất", "hunonic")
    assert result.found is True
    assert "vector" not in result.hits[0].channels


# ── logging ──────────────────────────────────────────────────────────────
def test_retrieval_is_logged_with_scores_and_timings():
    store = FakeStore([_workflow()], exact=["auth.logout"])
    _service(store).retrieve("Test đăng xuất", "hunonic", run_id="run-1")
    entry = store.logs[0]
    assert entry.run_id == "run-1"
    assert entry.chosen_workflow_id == "auth.logout"
    assert entry.retrieved[0]["channels"] == {"exact": 1}
    assert entry.timings_ms


def test_logged_context_is_redacted():
    workflow = _workflow(aliases=["logout"], description="token=supersecretvalue")
    store = FakeStore([workflow], exact=["auth.logout"])
    _service(store).retrieve("Test đăng xuất", "hunonic")
    assert "supersecretvalue" not in store.logs[0].context_sent
    assert "<redacted>" in store.logs[0].context_sent


def test_a_guess_is_not_logged_as_the_chosen_workflow():
    """Otherwise the "what did the AI know" view credits a workflow it never used."""
    store = FakeStore([_workflow()], keyword=[("auth.logout", 0.5)])
    _service(store).retrieve("chụp ảnh màn hình", "hunonic")
    assert store.logs[0].chosen_workflow_id is None
    assert store.logs[0].retrieved[0]["semantic_only"] is False  # keyword matched
    assert store.logs[0].retrieved[0]["channels"] == {"keyword": 1}
