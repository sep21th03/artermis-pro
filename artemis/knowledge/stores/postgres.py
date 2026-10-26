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

"""PostgreSQL + pgvector backend for the knowledge base.

``psycopg`` and ``pgvector`` are optional extras: importing this module without
them raises :class:`KnowledgeUnavailable`, which the service turns into
"no guidance available" rather than a task failure.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from artemis.knowledge.models import Workflow
from artemis.knowledge.stores.base import (
    EmbeddingRow,
    KnowledgeUnavailable,
    RetrievalLogEntry,
)
from artemis.knowledge.text import content_tokens, normalize

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"

#: Rows pulled per channel before fusion.  Wide enough that a good match is never
#: cut for being 30th in one channel, small enough to keep the query cheap.
DEFAULT_CHANNEL_LIMIT = 50


def _vector_literal(vector: list[float]) -> str:
    """Render a vector as a pgvector literal.

    Passing the value as a bound parameter keeps this injection-safe; the cast to
    ``::vector`` is what lets the untyped column accept it without a driver
    adapter, so the store depends on neither numpy nor adapter registration.
    """
    return "[" + ",".join(f"{float(value):.8g}" for value in vector) + "]"


class PostgresKnowledgeStore:
    """Knowledge base on PostgreSQL.

    Connections are opened per operation.  The Test Console runs one task at a
    time and the graph retrieves once per run, so a pool would add lifecycle to
    maintain without buying anything; a deployment that outgrows this can swap in
    a pooled ``connect`` callable.
    """

    def __init__(self, dsn: str, schema_path: Path | None = None) -> None:
        self.dsn = dsn
        self.schema_path = schema_path or SCHEMA_PATH
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise KnowledgeUnavailable(
                "Thiếu psycopg. Cài bằng: uv pip install 'psycopg[binary]' pgvector"
            ) from exc

    # ── connection plumbing ──────────────────────────────────────────────
    @contextmanager
    def _connect(self) -> Iterator[Any]:
        import psycopg

        try:
            conn = psycopg.connect(self.dsn, autocommit=True)
        except Exception as exc:  # noqa: BLE001 - any driver error means "unavailable"
            raise KnowledgeUnavailable(f"Không kết nối được PostgreSQL: {exc}") from exc
        try:
            yield conn
        except KnowledgeUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise KnowledgeUnavailable(f"Truy vấn knowledge base thất bại: {exc}") from exc
        finally:
            conn.close()

    def migrate(self) -> None:
        sql = self.schema_path.read_text(encoding="utf-8")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
        logger.info("Knowledge base schema ready (%s)", self.schema_path.name)

    def ping(self) -> bool:
        """True when the database answers and the vector extension is installed."""
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                return cur.fetchone() is not None
        except KnowledgeUnavailable:
            return False

    # ── apps and versions ────────────────────────────────────────────────
    def _ensure_app(self, cur: Any, app_id: str, name: str | None = None) -> None:
        """Workflows reference an app; create it on demand so a save never fails
        just because nobody registered the app first."""
        cur.execute(
            """
            INSERT INTO kb_app (id, name) VALUES (%s, %s)
            ON CONFLICT (id) DO UPDATE SET updated_at = now()
            """,
            (app_id, name or app_id),
        )

    def upsert_app(self, app_id: str, name: str, package_name: str | None = None) -> dict:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kb_app (id, name, package_name) VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                    SET name = EXCLUDED.name,
                        package_name = COALESCE(EXCLUDED.package_name, kb_app.package_name),
                        updated_at = now()
                RETURNING id, name, package_name
                """,
                (app_id, name, package_name),
            )
            row = cur.fetchone()
        return {"id": row[0], "name": row[1], "package_name": row[2]}

    def list_apps(self) -> list[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id, a.name, a.package_name,
                       (SELECT count(*) FROM kb_workflow w WHERE w.app_id = a.id)
                FROM kb_app a ORDER BY a.id
                """
            )
            rows = cur.fetchall()
        return [
            {"id": r[0], "name": r[1], "package_name": r[2], "workflow_count": r[3]} for r in rows
        ]

    def upsert_version(
        self, app_id: str, version_label: str, version_id: str | None = None, notes: str = ""
    ) -> dict:
        vid = version_id or f"{app_id}@{version_label}"
        with self._connect() as conn, conn.cursor() as cur:
            self._ensure_app(cur, app_id)
            cur.execute(
                """
                INSERT INTO kb_app_version (id, app_id, version_label, notes)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (app_id, version_label) DO UPDATE SET notes = EXCLUDED.notes
                RETURNING id, app_id, version_label, notes
                """,
                (vid, app_id, version_label, notes),
            )
            row = cur.fetchone()
        return {"id": row[0], "app_id": row[1], "version_label": row[2], "notes": row[3]}

    def list_versions(self, app_id: str) -> list[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, app_id, version_label, notes FROM kb_app_version
                WHERE app_id = %s ORDER BY version_label
                """,
                (app_id,),
            )
            rows = cur.fetchall()
        return [{"id": r[0], "app_id": r[1], "version_label": r[2], "notes": r[3]} for r in rows]

    # ── workflows ────────────────────────────────────────────────────────
    def find_app_by_package(self, package_name: str) -> str | None:
        """Knowledge-base app id for an Android package.

        Tries the registered package name first, then the package as an id, then
        the last package segment (``com.hunonic.app`` -> ``hunonic``).  Every step
        is an exact lookup: a fuzzy match here would serve another app's
        instructions, which is the one failure this subsystem must never have.
        """
        candidates = [package_name]
        if package_name and "." in package_name:
            candidates.append(package_name.rsplit(".", 1)[-1])
        with self._connect() as conn, conn.cursor() as cur:
            for candidate in candidates:
                cur.execute(
                    "SELECT id FROM kb_app WHERE package_name = %s OR id = %s LIMIT 1",
                    (package_name, candidate),
                )
                row = cur.fetchone()
                if row:
                    return row[0]
        return None

    def save_workflow(self, workflow: Workflow, change_note: str = "") -> Workflow:
        """Insert or replace a workflow, bump the revision and keep a snapshot.

        Children (aliases, steps, preconditions, criteria) are replaced wholesale:
        a workflow is edited as a document in the UI, so diffing them row by row
        would invent a merge policy nobody asked for.  The revision snapshot is
        what makes the replace reversible.
        """
        with self._connect() as conn, conn.cursor() as cur:
            self._ensure_app(cur, workflow.app_id)
            cur.execute(
                """
                INSERT INTO kb_workflow
                    (id, app_id, version_id, name, description, status, tags,
                     metadata, search_text, revision_no)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, 1)
                ON CONFLICT (id) DO UPDATE SET
                    app_id      = EXCLUDED.app_id,
                    version_id  = EXCLUDED.version_id,
                    name        = EXCLUDED.name,
                    description = EXCLUDED.description,
                    status      = EXCLUDED.status,
                    tags        = EXCLUDED.tags,
                    metadata    = EXCLUDED.metadata,
                    search_text = EXCLUDED.search_text,
                    revision_no = kb_workflow.revision_no + 1,
                    updated_at  = now()
                RETURNING revision_no, created_at, updated_at
                """,
                (
                    workflow.id,
                    workflow.app_id,
                    workflow.version_id,
                    workflow.name,
                    workflow.description,
                    workflow.status,
                    workflow.tags,
                    json.dumps(workflow.metadata),
                    normalize(workflow.search_text()),
                ),
            )
            revision_no, created_at, updated_at = cur.fetchone()

            cur.execute("DELETE FROM kb_workflow_alias WHERE workflow_id = %s", (workflow.id,))
            for ordinal, alias in enumerate(workflow.aliases):
                cur.execute(
                    """
                    INSERT INTO kb_workflow_alias (workflow_id, alias, normalized, ordinal)
                    VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                    """,
                    (workflow.id, alias, normalize(alias), ordinal),
                )

            cur.execute("DELETE FROM kb_workflow_step WHERE workflow_id = %s", (workflow.id,))
            for ordinal, step in enumerate(workflow.steps):
                cur.execute(
                    """
                    INSERT INTO kb_workflow_step
                        (workflow_id, ordinal, action, target, expected_state,
                         direction, until, value, timeout_s, optional)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        workflow.id,
                        ordinal,
                        step.action,
                        step.target,
                        step.expected_state,
                        step.direction,
                        step.until,
                        step.value,
                        step.timeout_s,
                        step.optional,
                    ),
                )

            for table, values in (
                ("kb_workflow_precondition", workflow.preconditions),
                ("kb_workflow_success_criterion", workflow.success_criteria),
            ):
                cur.execute(f"DELETE FROM {table} WHERE workflow_id = %s", (workflow.id,))
                for ordinal, text in enumerate(values):
                    cur.execute(
                        f"INSERT INTO {table} (workflow_id, ordinal, text) VALUES (%s, %s, %s)",
                        (workflow.id, ordinal, text),
                    )

            saved = workflow.model_copy(
                update={
                    "revision_no": revision_no,
                    "created_at": str(created_at),
                    "updated_at": str(updated_at),
                }
            )
            cur.execute(
                """
                INSERT INTO kb_workflow_revision
                    (workflow_id, revision_no, snapshot, change_note)
                VALUES (%s, %s, %s::jsonb, %s)
                """,
                (workflow.id, revision_no, json.dumps(saved.model_dump()), change_note),
            )
        return saved

    _COLUMNS = (
        "id, app_id, version_id, name, description, status, tags, metadata, "
        "revision_no, created_at, updated_at"
    )

    def _children(self, cur: Any, ids: list[str]) -> tuple[dict, dict, dict, dict]:
        """Load every child row for ``ids`` in four queries (no N+1)."""
        aliases: dict[str, list[str]] = {}
        steps: dict[str, list[dict]] = {}
        preconds: dict[str, list[str]] = {}
        criteria: dict[str, list[str]] = {}
        if not ids:
            return aliases, steps, preconds, criteria

        cur.execute(
            "SELECT workflow_id, alias FROM kb_workflow_alias "
            "WHERE workflow_id = ANY(%s) ORDER BY workflow_id, ordinal",
            (ids,),
        )
        for workflow_id, alias in cur.fetchall():
            aliases.setdefault(workflow_id, []).append(alias)

        cur.execute(
            """
            SELECT workflow_id, action, target, expected_state, direction,
                   until, value, timeout_s, optional
            FROM kb_workflow_step WHERE workflow_id = ANY(%s)
            ORDER BY workflow_id, ordinal
            """,
            (ids,),
        )
        for row in cur.fetchall():
            steps.setdefault(row[0], []).append(
                {
                    "action": row[1],
                    "target": row[2],
                    "expected_state": row[3],
                    "direction": row[4],
                    "until": row[5],
                    "value": row[6],
                    "timeout_s": row[7],
                    "optional": row[8],
                }
            )

        for table, target in (
            ("kb_workflow_precondition", preconds),
            ("kb_workflow_success_criterion", criteria),
        ):
            cur.execute(
                f"SELECT workflow_id, text FROM {table} "
                "WHERE workflow_id = ANY(%s) ORDER BY workflow_id, ordinal",
                (ids,),
            )
            for workflow_id, text in cur.fetchall():
                target.setdefault(workflow_id, []).append(text)
        return aliases, steps, preconds, criteria

    def _build(
        self, row: Any, aliases: dict, steps: dict, preconds: dict, criteria: dict
    ) -> Workflow:
        return Workflow(
            id=row[0],
            app_id=row[1],
            version_id=row[2],
            name=row[3],
            description=row[4],
            status=row[5],
            tags=list(row[6] or []),
            metadata=row[7] or {},
            revision_no=row[8],
            created_at=str(row[9]),
            updated_at=str(row[10]),
            aliases=aliases.get(row[0], []),
            steps=steps.get(row[0], []),
            preconditions=preconds.get(row[0], []),
            success_criteria=criteria.get(row[0], []),
        )

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLUMNS} FROM kb_workflow WHERE id = %s", (workflow_id,))
            row = cur.fetchone()
            if row is None:
                return None
            children = self._children(cur, [workflow_id])
        return self._build(row, *children)

    def list_workflows(
        self, app_id: str | None = None, status: str | None = None
    ) -> list[Workflow]:
        where, params = [], []
        if app_id:
            where.append("app_id = %s")
            params.append(app_id)
        if status:
            where.append("status = %s")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._COLUMNS} FROM kb_workflow {clause} ORDER BY app_id, id", params
            )
            rows = cur.fetchall()
            children = self._children(cur, [row[0] for row in rows])
        return [self._build(row, *children) for row in rows]

    def delete_workflow(self, workflow_id: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM kb_workflow WHERE id = %s", (workflow_id,))
            return cur.rowcount > 0

    def revisions(self, workflow_id: str) -> list[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT revision_no, change_note, created_at, snapshot
                FROM kb_workflow_revision WHERE workflow_id = %s
                ORDER BY revision_no DESC
                """,
                (workflow_id,),
            )
            rows = cur.fetchall()
        return [
            {"revision_no": r[0], "change_note": r[1], "created_at": str(r[2]), "snapshot": r[3]}
            for r in rows
        ]

    # ── retrieval channels ───────────────────────────────────────────────
    #: Every channel is scoped identically.  Serving another app's workflow is
    #: the worst possible failure of this subsystem: the agent would follow
    #: instructions for a UI that is not on screen.
    _SCOPE = (
        "w.app_id = %(app)s "
        "AND (w.version_id IS NULL OR w.version_id = %(ver)s) "
        "AND w.status <> 'deprecated'"
    )

    _RANK_CASE = """
        CASE
            WHEN lower(w.id) = %(q)s THEN 1
            WHEN a.normalized = %(q)s THEN 2
            WHEN %(q)s LIKE '%%' || lower(w.id) || '%%' THEN 3
            WHEN a.normalized IS NOT NULL
                 AND %(q)s LIKE '%%' || a.normalized || '%%' THEN 4
            ELSE 99
        END
    """

    def search_exact(
        self, query: str, app_id: str, version_id: str | None, limit: int = DEFAULT_CHANNEL_LIMIT
    ) -> list[str]:
        """Workflow id and alias matches, strongest first.

        Ranks: 1 = the query *is* the id, 2 = the query *is* an alias, 3 = the id
        appears inside the query, 4 = an alias appears inside the query.  A tester
        typing "test auth.logout" still lands on the workflow.
        """
        normalised = normalize(query)
        if not normalised:
            return []
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT w.id, MIN({self._RANK_CASE}) AS rank
                FROM kb_workflow w
                LEFT JOIN kb_workflow_alias a ON a.workflow_id = w.id
                WHERE {self._SCOPE}
                GROUP BY w.id
                HAVING MIN({self._RANK_CASE}) < 99
                ORDER BY rank, w.id
                LIMIT %(limit)s
                """,
                {"q": normalised, "app": app_id, "ver": version_id, "limit": limit},
            )
            return [row[0] for row in cur.fetchall()]

    def search_keyword(
        self, query: str, app_id: str, version_id: str | None, limit: int = DEFAULT_CHANNEL_LIMIT
    ) -> list[tuple[str, float]]:
        """Full-text search over the normalised projection.

        Returns ``(workflow_id, coverage)`` where coverage is the share of the
        query's *content* tokens present in the document.  Ranking uses an OR
        query (built from the tokens, because ``websearch_to_tsquery`` ANDs them
        and would match nothing for "test đăng xuất"), while coverage is what
        decides whether the hit is trustworthy -- an OR query alone matches on a
        single common word.
        """
        tokens = content_tokens(query)
        if not tokens:
            return []
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT w.id,
                       ts_rank(to_tsvector('simple', w.search_text),
                               to_tsquery('simple', %(tsq)s)) AS rank,
                       (SELECT count(*) FROM unnest(%(tokens)s::text[]) AS t
                         WHERE w.search_text ~ ('\\m' || t || '\\M')) AS matched
                FROM kb_workflow w
                WHERE {self._SCOPE}
                  AND to_tsvector('simple', w.search_text)
                      @@ to_tsquery('simple', %(tsq)s)
                ORDER BY rank DESC, w.id
                LIMIT %(limit)s
                """,
                {
                    "tsq": " | ".join(tokens),
                    "tokens": tokens,
                    "app": app_id,
                    "ver": version_id,
                    "limit": limit,
                },
            )
            rows = cur.fetchall()
        return [(row[0], float(row[2]) / len(tokens)) for row in rows]

    def search_vector(
        self,
        vector: list[float],
        model: str,
        app_id: str,
        version_id: str | None,
        limit: int = DEFAULT_CHANNEL_LIMIT,
    ) -> list[tuple[str, float]]:
        """Cosine nearest neighbours for ``model``, best first."""
        if not vector:
            return []
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT w.id, 1 - (e.embedding <=> %(vec)s::vector) AS similarity
                FROM kb_workflow_embedding e
                JOIN kb_workflow w ON w.id = e.workflow_id
                WHERE e.model = %(model)s AND {self._SCOPE}
                ORDER BY e.embedding <=> %(vec)s::vector
                LIMIT %(limit)s
                """,
                {
                    "vec": _vector_literal(vector),
                    "model": model,
                    "app": app_id,
                    "ver": version_id,
                    "limit": limit,
                },
            )
            return [(row[0], float(row[1])) for row in cur.fetchall()]

    # ── embeddings ───────────────────────────────────────────────────────
    def load_embeddings(
        self, model: str, app_id: str | None = None, version_id: str | None = None
    ) -> dict[str, EmbeddingRow]:
        where = ["e.model = %s"]
        params: list[Any] = [model]
        if app_id:
            where.append("w.app_id = %s")
            params.append(app_id)
        if version_id:
            where.append("(w.version_id IS NULL OR w.version_id = %s)")
            params.append(version_id)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT e.workflow_id, e.model, e.dim, e.content_hash, e.embedding::text
                FROM kb_workflow_embedding e
                JOIN kb_workflow w ON w.id = e.workflow_id
                WHERE {" AND ".join(where)}
                """,
                params,
            )
            rows = cur.fetchall()
        out: dict[str, EmbeddingRow] = {}
        for workflow_id, model_name, dim, content_hash, literal in rows:
            out[workflow_id] = EmbeddingRow(
                workflow_id=workflow_id,
                model=model_name,
                dim=dim,
                content_hash=content_hash,
                vector=[float(x) for x in str(literal).strip("[]").split(",") if x],
            )
        return out

    def save_embedding(
        self, workflow_id: str, model: str, content_hash: str, vector: list[float]
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kb_workflow_embedding
                    (workflow_id, model, dim, content_hash, embedding)
                VALUES (%s, %s, %s, %s, %s::vector)
                ON CONFLICT (workflow_id, model) DO UPDATE SET
                    dim = EXCLUDED.dim,
                    content_hash = EXCLUDED.content_hash,
                    embedding = EXCLUDED.embedding,
                    updated_at = now()
                """,
                (workflow_id, model, len(vector), content_hash, _vector_literal(vector)),
            )

    def delete_embeddings(self, model: str) -> int:
        """Drop every vector for a model, so a re-index can rebuild them."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM kb_workflow_embedding WHERE model = %s", (model,))
            return cur.rowcount

    # ── observability ────────────────────────────────────────────────────
    def log_retrieval(self, entry: RetrievalLogEntry) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kb_retrieval_log
                    (run_id, query, app_id, version_id, retrieved, chosen_workflow_id,
                     context_sent, timings_ms, degraded)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s)
                """,
                (
                    entry.run_id,
                    entry.query,
                    entry.app_id,
                    entry.version_id,
                    json.dumps(entry.retrieved),
                    entry.chosen_workflow_id,
                    entry.context_sent,
                    json.dumps(entry.timings_ms),
                    entry.degraded,
                ),
            )

    _LOG_COLUMNS = (
        "id, run_id, query, app_id, version_id, retrieved, chosen_workflow_id, "
        "context_sent, timings_ms, degraded, outcome, created_at"
    )

    def list_retrieval_logs(self, run_id: str | None = None, limit: int = 50) -> list[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            if run_id:
                cur.execute(
                    f"SELECT {self._LOG_COLUMNS} FROM kb_retrieval_log "
                    "WHERE run_id = %s ORDER BY id DESC LIMIT %s",
                    (run_id, limit),
                )
            else:
                cur.execute(
                    f"SELECT {self._LOG_COLUMNS} FROM kb_retrieval_log ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
        keys = [name.strip() for name in self._LOG_COLUMNS.split(",")]
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def record_outcome(self, run_id: str, outcome: dict) -> int:
        """Attach a run outcome to every retrieval logged for that run."""
        if not run_id:
            # Without a run id the update would match every historical row.
            return 0
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE kb_retrieval_log
                SET outcome = %s::jsonb, outcome_recorded_at = now()
                WHERE run_id = %s
                """,
                (json.dumps(outcome), run_id),
            )
            return cur.rowcount
