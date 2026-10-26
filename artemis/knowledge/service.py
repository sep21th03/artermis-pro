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

"""The single entry point to the knowledge base.

Everything the agent or the web UI does with workflow knowledge goes through
here.  The contract that matters: **this never raises into a run.**  A missing
DSN, a stopped database, a broken API key or an embedding outage all collapse to
``RetrievalResult(found=False, degraded=True)`` and ARTEMIS proceeds without
knowledge, exactly as it did before this subsystem existed.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from artemis.knowledge.context import build_context
from artemis.knowledge.embeddings import Embedder, EmbeddingError, GeminiEmbedder
from artemis.knowledge.models import RetrievalResult, Workflow, WorkflowHit
from artemis.knowledge.retrieval import (
    DEFAULT_RRF_K,
    decide_found,
    reciprocal_rank_fusion,
)
from artemis.knowledge.stores.base import KnowledgeStore, KnowledgeUnavailable, RetrievalLogEntry
from artemis.knowledge.text import strip_secrets

logger = logging.getLogger(__name__)

#: Share of a query's content tokens a document must contain for a keyword hit to
#: count as evidence.  Below 1.0 because testers pad requests with words the
#: workflow does not store; above 0.5 because a hit on half the words is how
#: "chụp ảnh màn hình" ends up matched against the logout workflow (they share
#: "màn hình").  Measured on the sample catalogue: real requests land at 1.0,
#: that false positive lands at 0.5.
DEFAULT_KEYWORD_MIN_COVERAGE = 0.6


def content_hash(text: str) -> str:
    """Stable hash of the text a vector was produced from.

    This is what keeps a re-save of an untouched workflow from spending an
    embedding call: the stored hash matches and the vector is reused.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class KnowledgeService:
    """Workflow retrieval and indexing, degrading instead of failing."""

    def __init__(
        self,
        store: KnowledgeStore | None = None,
        embedder: Embedder | None = None,
        *,
        top_k: int = 3,
        rrf_k: int = DEFAULT_RRF_K,
        context_max_chars: int = 4000,
        keyword_min_coverage: float = DEFAULT_KEYWORD_MIN_COVERAGE,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self.top_k = max(1, top_k)
        self.rrf_k = rrf_k
        self.context_max_chars = context_max_chars
        self.keyword_min_coverage = keyword_min_coverage
        self._degraded_reason = ""

    # ── construction ─────────────────────────────────────────────────────
    @classmethod
    def from_settings(cls, settings: Any | None = None) -> KnowledgeService:
        """Build from configuration; never raises, returns a disabled service."""
        if settings is None:
            from artemis.config.settings import settings as global_settings

            settings = global_settings
        if not getattr(settings, "ARTEMIS_KB_ENABLED", True):
            service = cls()
            service._degraded_reason = "ARTEMIS_KB_ENABLED=0"
            return service
        dsn = getattr(settings, "ARTEMIS_KB_DSN", None)
        if not dsn:
            service = cls()
            service._degraded_reason = "Chưa cấu hình ARTEMIS_KB_DSN"
            return service
        try:
            from artemis.knowledge.stores.postgres import PostgresKnowledgeStore

            store: KnowledgeStore | None = PostgresKnowledgeStore(dsn)
            store.migrate()
        except (KnowledgeUnavailable, Exception) as exc:  # noqa: BLE001
            logger.warning("Knowledge base không dùng được: %s", exc)
            service = cls()
            service._degraded_reason = f"Knowledge base không dùng được: {exc}"
            return service

        embedder: Embedder | None
        try:
            embedder = GeminiEmbedder(
                model=getattr(settings, "ARTEMIS_KB_EMBEDDING_MODEL", "gemini-embedding-2"),
                output_dim=getattr(settings, "ARTEMIS_KB_EMBEDDING_DIM", None),
                batch_size=getattr(settings, "ARTEMIS_KB_EMBEDDING_BATCH", 32),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Không tạo được embedder: %s", exc)
            embedder = None
        return cls(
            store=store,
            embedder=embedder,
            top_k=getattr(settings, "ARTEMIS_KB_TOP_K", 3),
            rrf_k=getattr(settings, "ARTEMIS_KB_RRF_K", DEFAULT_RRF_K),
            context_max_chars=getattr(settings, "ARTEMIS_KB_CONTEXT_MAX_CHARS", 4000),
            keyword_min_coverage=getattr(
                settings, "ARTEMIS_KB_KEYWORD_MIN_COVERAGE", DEFAULT_KEYWORD_MIN_COVERAGE
            ),
        )

    @property
    def available(self) -> bool:
        return self._store is not None

    @property
    def degraded_reason(self) -> str:
        return self._degraded_reason

    def status(self) -> dict:
        """Health for the web UI: is retrieval actually usable right now."""
        return {
            "available": self.available,
            "degraded_reason": self._degraded_reason,
            "embedding_model": getattr(self._embedder, "model", None),
            "vector_channel": self._embedder is not None,
            "top_k": self.top_k,
            "rrf_k": self.rrf_k,
            "keyword_min_coverage": self.keyword_min_coverage,
        }

    # ── indexing ─────────────────────────────────────────────────────────
    def _embedding_text(self, workflow: Workflow) -> str:
        return workflow.embedding_text()

    def ensure_embeddings(self, app_id: str | None = None, version_id: str | None = None) -> dict:
        """Embed only the workflows whose text changed since the last indexing.

        Called before retrieval, so a catalogue edited by hand (or by a script)
        becomes searchable without anyone remembering to run a re-index command.
        """
        if self._store is None:
            return {"embedded": 0, "reused": 0, "error": self._degraded_reason or "no store"}
        if self._embedder is None:
            return {"embedded": 0, "reused": 0, "error": "không có embedder"}

        model = getattr(self._embedder, "model", "")
        workflows = self._store.list_workflows(app_id=app_id)
        stored = self._store.load_embeddings(model, app_id=app_id, version_id=version_id)

        stale: list[tuple[Workflow, str]] = []
        reused = 0
        for workflow in workflows:
            digest = content_hash(self._embedding_text(workflow))
            row = stored.get(workflow.id)
            if row is not None and row.content_hash == digest:
                reused += 1
            else:
                stale.append((workflow, digest))
        if not stale:
            return {"embedded": 0, "reused": reused, "stale": 0}

        try:
            vectors = self._embedder.embed_documents(
                [self._embedding_text(workflow) for workflow, _ in stale]
            )
        except EmbeddingError as exc:
            logger.warning("Không tạo được embedding: %s", exc)
            return {"embedded": 0, "reused": reused, "stale": len(stale), "error": str(exc)}

        for (workflow, digest), vector in zip(stale, vectors, strict=True):
            self._store.save_embedding(workflow.id, model, digest, vector)
        return {"embedded": len(stale), "reused": reused, "stale": len(stale)}

    def index_workflow(self, workflow: Workflow) -> bool:
        """Embed one workflow if its text changed.  True when a call was made."""
        if self._store is None or self._embedder is None:
            return False
        model = getattr(self._embedder, "model", "")
        digest = content_hash(self._embedding_text(workflow))
        existing = self._store.load_embeddings(model).get(workflow.id)
        if existing is not None and existing.content_hash == digest:
            return False
        try:
            vectors = self._embedder.embed_documents([self._embedding_text(workflow)])
        except EmbeddingError as exc:
            logger.warning("Không embed được %s: %s", workflow.id, exc)
            return False
        if not vectors:
            return False
        self._store.save_embedding(workflow.id, model, digest, vectors[0])
        return True

    def reindex(self, app_id: str | None = None) -> dict:
        """Drop and rebuild every vector for the configured model."""
        if self._store is None:
            raise KnowledgeUnavailable(self._degraded_reason or "Knowledge base chưa sẵn sàng")
        model = getattr(self._embedder, "model", None)
        if model is None:
            raise KnowledgeUnavailable("Không có embedder nên không reindex được")
        dropped = self._store.delete_embeddings(model)
        result = self.ensure_embeddings(app_id=app_id)
        return {"dropped": dropped, **result}

    # ── retrieval ────────────────────────────────────────────────────────
    def retrieve(
        self,
        query: str,
        app_id: str,
        version_id: str | None = None,
        top_k: int | None = None,
        run_id: str | None = None,
    ) -> RetrievalResult:
        """Hybrid retrieval for one request.  Never raises."""
        started = time.perf_counter()
        if not self.available:
            return RetrievalResult(
                found=False,
                query=query,
                app_id=app_id,
                version_id=version_id,
                reason=self._degraded_reason or "Knowledge base chưa sẵn sàng",
                degraded=True,
            )

        limit = max(1, top_k or self.top_k)
        timings: dict[str, float] = {}
        channels: dict[str, list[str]] = {}
        similarities: dict[str, dict[str, float]] = {}
        trusted_ids: set[str] = set()

        try:
            mark = time.perf_counter()
            exact_rows = self._store.search_exact(query, app_id, version_id)
            channels["exact"] = list(exact_rows)
            # An id or alias match is the strongest evidence there is: the tester
            # named the workflow.
            trusted_ids.update(exact_rows)
            timings["exact_ms"] = round((time.perf_counter() - mark) * 1000, 2)

            mark = time.perf_counter()
            keyword_rows = self._store.search_keyword(query, app_id, version_id)
            channels["keyword"] = [workflow_id for workflow_id, _ in keyword_rows]
            # The keyword channel ranks with an OR query, so a single common word
            # matches; only a hit covering enough of the query counts as evidence.
            trusted_ids.update(
                workflow_id
                for workflow_id, covered in keyword_rows
                if covered >= self.keyword_min_coverage
            )
            timings["keyword_ms"] = round((time.perf_counter() - mark) * 1000, 2)
        except KnowledgeUnavailable as exc:
            logger.warning("Truy vấn knowledge base lỗi: %s", exc)
            return RetrievalResult(
                found=False,
                query=query,
                app_id=app_id,
                version_id=version_id,
                reason=f"Knowledge base lỗi: {exc}",
                degraded=True,
                timings_ms=timings,
            )

        # The vector channel is best-effort: losing it costs recall, and it must
        # never be able to fail the lookup.
        if self._embedder is not None:
            mark = time.perf_counter()
            vector: list[float] = []
            try:
                vector = self._embedder.embed_query(query)
            except EmbeddingError as exc:
                logger.info("Bỏ kênh vector cho truy vấn này: %s", exc)
            timings["embed_ms"] = round((time.perf_counter() - mark) * 1000, 2)
            if vector:
                mark = time.perf_counter()
                try:
                    rows = self._store.search_vector(
                        vector, getattr(self._embedder, "model", ""), app_id, version_id
                    )
                    channels["vector"] = [workflow_id for workflow_id, _ in rows]
                    similarities = {workflow_id: {"cosine": sim} for workflow_id, sim in rows}
                except KnowledgeUnavailable as exc:
                    logger.info("Bỏ kênh vector: %s", exc)
                timings["vector_ms"] = round((time.perf_counter() - mark) * 1000, 2)

        fused = reciprocal_rank_fusion(channels, k=self.rrf_k, similarities=similarities)

        # Trusted hits fill the budget; at most one guess rides along, so a
        # speculative match can never crowd out a real one.
        hits: list[WorkflowHit] = []
        trusted = 0
        guessed = 0
        for item in fused:
            is_trusted = item.workflow_id in trusted_ids
            if is_trusted:
                if trusted >= limit:
                    continue
            elif guessed >= 1:
                continue
            workflow = self._store.get_workflow(item.workflow_id)
            if workflow is None:
                continue
            hits.append(
                WorkflowHit(
                    workflow=workflow,
                    score=item.score,
                    channels=item.ranks,
                    trusted=is_trusted,
                )
            )
            if is_trusted:
                trusted += 1
            else:
                guessed += 1

        found, reason = decide_found(hits)

        timings["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
        result = RetrievalResult(
            found=found,
            query=query,
            app_id=app_id,
            version_id=version_id,
            hits=hits,
            reason=reason,
            timings_ms=timings,
        )
        self._log(result, run_id=run_id)
        return result

    def context_for(
        self,
        query: str,
        app_id: str,
        version_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[RetrievalResult, str]:
        """Retrieve and render in one step -- what the Planner actually calls."""
        result = self.retrieve(query, app_id, version_id=version_id, run_id=run_id)
        return result, build_context(result, self.context_max_chars)

    def _log(self, result: RetrievalResult, run_id: str | None) -> None:
        if self._store is None:
            return
        try:
            self._store.log_retrieval(
                RetrievalLogEntry(
                    query=result.query,
                    app_id=result.app_id,
                    version_id=result.version_id,
                    run_id=run_id,
                    retrieved=[
                        {
                            "workflow_id": hit.workflow.id,
                            "name": hit.workflow.name,
                            "score": hit.score,
                            "channels": hit.channels,
                            "semantic_only": hit.semantic_only,
                        }
                        for hit in result.hits
                    ],
                    # Only a *confirmed* hit counts as chosen: recording a guess
                    # here would make the "what did the AI know" view claim the
                    # logout workflow was used for a screenshot request.
                    chosen_workflow_id=next(
                        (hit.workflow.id for hit in result.hits if hit.trusted), None
                    ),
                    # Redacted before it is written: this table must never hold a
                    # credential a tester pasted into a workflow note.
                    context_sent=strip_secrets(build_context(result, self.context_max_chars)),
                    timings_ms=result.timings_ms,
                    degraded=result.degraded,
                )
            )
        except KnowledgeUnavailable as exc:
            logger.info("Không ghi được retrieval log: %s", exc)


_SERVICE: KnowledgeService | None = None


def get_service() -> KnowledgeService:
    """Process-wide service, built from settings on first use."""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = KnowledgeService.from_settings()
    return _SERVICE


def reset_service() -> None:
    """Drop the cached service (tests, or after changing configuration)."""
    global _SERVICE
    _SERVICE = None
