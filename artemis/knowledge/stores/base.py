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

"""Storage contract for the knowledge base.

The retrieval logic is deliberately *outside* the store: the store only answers
"which workflow ids match this channel, in what order", and the fusion of those
rankings happens once in :mod:`artemis.knowledge.retrieval`.  A second backend
therefore has to implement ranking, never relevance policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from artemis.knowledge.models import Workflow


class KnowledgeUnavailable(RuntimeError):
    """The knowledge base could not be consulted.

    Callers must treat this as "no guidance available", never as a task failure:
    ARTEMIS has to keep running with an empty knowledge base.
    """


@dataclass
class EmbeddingRow:
    """A stored vector plus the hash of the text it was produced from."""

    workflow_id: str
    model: str
    dim: int
    content_hash: str
    vector: list[float]


@dataclass
class RetrievalLogEntry:
    """One retrieval, as recorded for the "what did the AI know" view."""

    query: str
    app_id: str | None = None
    version_id: str | None = None
    run_id: str | None = None
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    chosen_workflow_id: str | None = None
    context_sent: str = ""
    timings_ms: dict[str, float] = field(default_factory=dict)
    degraded: bool = False


@runtime_checkable
class KnowledgeStore(Protocol):
    """What every backend must provide."""

    def migrate(self) -> None:
        """Create or upgrade the schema. Idempotent."""

    # ── Apps and versions ────────────────────────────────────────────────
    def upsert_app(self, app_id: str, name: str, package_name: str | None = None) -> dict: ...

    def list_apps(self) -> list[dict]: ...

    def find_app_by_package(self, package_name: str) -> str | None:
        """Knowledge-base app id for an Android package, or None.

        Runs carry a package name (``com.hunonic.app``) while workflows are
        scoped by app id (``hunonic``); this is the join between them.
        """

    def upsert_version(
        self, app_id: str, version_label: str, version_id: str | None = None, notes: str = ""
    ) -> dict: ...

    def list_versions(self, app_id: str) -> list[dict]: ...

    # ── Workflows ────────────────────────────────────────────────────────
    def save_workflow(self, workflow: Workflow, change_note: str = "") -> Workflow:
        """Insert or update, bumping ``revision_no`` and appending a revision row."""

    def get_workflow(self, workflow_id: str) -> Workflow | None: ...

    def list_workflows(
        self, app_id: str | None = None, status: str | None = None
    ) -> list[Workflow]: ...

    def delete_workflow(self, workflow_id: str) -> bool: ...

    def revisions(self, workflow_id: str) -> list[dict]: ...

    # ── Retrieval channels (each returns workflow ids best-first) ─────────
    def search_exact(
        self, query: str, app_id: str, version_id: str | None, limit: int
    ) -> list[str]: ...

    def search_keyword(
        self, query: str, app_id: str, version_id: str | None, limit: int
    ) -> list[tuple[str, float]]:
        """``(workflow_id, coverage)`` best-first.  Coverage is the share of the
        query's content tokens present in the document, and is what the caller
        uses to decide whether a keyword hit is trustworthy."""

    def search_vector(
        self, vector: list[float], model: str, app_id: str, version_id: str | None, limit: int
    ) -> list[tuple[str, float]]:
        """``(workflow_id, similarity)`` best-first; similarity is 1 - cosine distance."""

    # ── Embeddings ───────────────────────────────────────────────────────
    def load_embeddings(
        self, model: str, app_id: str | None = None, version_id: str | None = None
    ) -> dict[str, EmbeddingRow]: ...

    def save_embedding(
        self, workflow_id: str, model: str, content_hash: str, vector: list[float]
    ) -> None: ...

    # ── Observability ────────────────────────────────────────────────────
    def log_retrieval(self, entry: RetrievalLogEntry) -> None: ...

    def list_retrieval_logs(self, run_id: str | None = None, limit: int = 50) -> list[dict]: ...

    def record_outcome(self, run_id: str, outcome: dict) -> int:
        """Attach a run outcome to every retrieval logged for that run.

        Returns the number of rows updated, so a caller can tell "recorded" from
        "there was nothing to record".
        """
