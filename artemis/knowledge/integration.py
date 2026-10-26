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

"""Wire knowledge retrieval into a run.

**Why the context rides on ``ArtemisContext`` and not on the graph ``State``.**
``State`` is ``extra="forbid"`` and guarded by a per-node visibility manifest, so
a new field is a contract change with a test-time enforcer behind it.  The
Planner already receives the context object, and ``ArtemisContext`` is
``extra="allow"``, so attaching the block there costs nothing and changes no
contract.  A run with no knowledge simply never sets the attribute, and every
reader treats "missing" as "no knowledge" -- which is exactly the pre-RAG
behaviour the feature must fall back to.

**Nothing here raises.**  A knowledge base that can fail a test run is worse than
no knowledge base; every entry point degrades to "no knowledge".
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from artemis.knowledge.service import KnowledgeService, get_service

logger = logging.getLogger(__name__)

#: Attribute names on ``ArtemisContext``.  Read them through
#: :func:`context_text` / :func:`context_meta` so a rename stays in one place.
KNOWLEDGE_CONTEXT_ATTR = "knowledge_context"
KNOWLEDGE_META_ATTR = "knowledge_meta"

#: A console elsewhere can answer retrieval over HTTP.  Set these on a tester
#: machine that has ARTEMIS and a phone but no PostgreSQL and no embedding key.
REMOTE_API_ENV = "ARTEMIS_KB_API"
REMOTE_KEY_ENV = "ARTEMIS_KB_API_KEY"
REMOTE_TIMEOUT_ENV = "ARTEMIS_KB_API_TIMEOUT"

#: Tên client gửi kèm. Cloudflare đứng trước máy chủ chặn User-Agent mặc định của
#: Python (`Python-urllib/3.x`) bằng lỗi 1010 — đo được: cùng một request, UA mặc
#: định trả 403 còn UA này trả 200. Tự giới thiệu tên mình cũng là phép lịch sự.
REMOTE_USER_AGENT = "ARTEMIS/1.0"

#: Đặt biến này khi prompt đưa vào **đã có sẵn khối kiến thức** — console ghép rồi
#: và tester đã xem/sửa. Không có nó thì ARTEMIS lấy thêm một lần nữa và khối kiến
#: thức vào prompt hai lần.
PRESUPPLIED_ENV = "ARTEMIS_KB_PRESUPPLIED"


def knowledge_presupplied() -> bool:
    """True khi prompt đã chứa khối kiến thức do console ghép sẵn."""
    return (os.getenv(PRESUPPLIED_ENV) or "").strip().lower() in {"1", "true", "yes"}


def remote_api_url() -> str:
    """Base URL of the console that serves knowledge, or ``""`` for the local database."""
    return (os.getenv(REMOTE_API_ENV) or "").strip().rstrip("/")


@dataclass
class KnowledgeContext:
    """What retrieval produced for one run, and what was handed to the Planner."""

    text: str = ""
    found: bool = False
    degraded: bool = False
    reason: str = ""
    app_id: str | None = None
    workflow_ids: list[str] = field(default_factory=list)
    trusted_ids: list[str] = field(default_factory=list)
    #: ``"database"`` (đọc tại chỗ) hoặc ``"console"`` (hỏi console ở xa). Chỉ để
    #: dòng log nói đúng khối kiến thức này đến từ đâu khi có sự cố.
    source: str = "database"
    elapsed_ms: float = 0.0

    @property
    def injected(self) -> bool:
        """True when the Planner will actually be given a knowledge block.

        Requires a *confirmed* hit, not merely a non-empty text.  When the only
        match is an unconfirmed guess, the run must behave exactly as a run with
        no knowledge base at all -- so the guess never reaches the Planner.  It
        stays visible in the console preview, where a human can judge it, which
        is the only place a guess is worth anything.
        """
        return self.found and bool(self.text.strip())

    def meta(self) -> dict:
        """The structured record kept on the context and written to the log."""
        return {
            "found": self.found,
            "degraded": self.degraded,
            "injected": self.injected,
            "app_id": self.app_id,
            "reason": self.reason,
            "workflow_ids": list(self.workflow_ids),
            "trusted_ids": list(self.trusted_ids),
            "source": self.source,
            "elapsed_ms": self.elapsed_ms,
        }


def resolve_app_id(app_package: str | None, service: KnowledgeService) -> str | None:
    """Knowledge-base app id for the package under test, or None.

    None is a normal answer: the app simply is not registered, and the run
    proceeds without knowledge rather than guessing at a similar app.
    """
    if not app_package:
        return None
    try:
        return service._store.find_app_by_package(app_package)
    except Exception as exc:  # noqa: BLE001 - lookup must never break a run
        logger.info("Không tra được app cho package %s: %s", app_package, exc)
        return None


def retrieve_for_run(
    goal: str,
    app_package: str | None = None,
    run_id: str | None = None,
    service: KnowledgeService | None = None,
) -> KnowledgeContext:
    """Retrieve workflow guidance for a run.  Never raises."""
    started = time.perf_counter()
    if knowledge_presupplied():
        # Console đã ghép khối kiến thức vào prompt và tester đã xem/sửa nó. Lấy
        # thêm lần nữa là chèn hai lần.
        return KnowledgeContext(
            reason="Prompt đã có khối kiến thức do console ghép sẵn",
            source="console",
        )
    remote = remote_api_url()
    if remote:
        return _retrieve_remote(remote, goal, app_package, run_id, started)
    try:
        service = service or get_service()
    except Exception as exc:  # noqa: BLE001
        logger.info("Knowledge base không khởi tạo được: %s", exc)
        return KnowledgeContext(degraded=True, reason=f"Không khởi tạo được knowledge base: {exc}")

    if not service.available:
        return KnowledgeContext(
            degraded=True,
            reason=service.degraded_reason or "Knowledge base chưa sẵn sàng",
        )

    app_id = resolve_app_id(app_package, service)
    if app_id is None:
        return KnowledgeContext(
            reason=(
                f"App của package {app_package!r} chưa có trong knowledge base"
                if app_package
                else "Không xác định được app đang test"
            ),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    try:
        result, text = service.context_for(goal, app_id, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - retrieval is advisory
        logger.warning("Truy xuất knowledge base lỗi: %s", exc)
        return KnowledgeContext(
            app_id=app_id,
            degraded=True,
            reason=f"Truy xuất lỗi: {exc}",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    knowledge = KnowledgeContext(
        text=text,
        found=result.found,
        degraded=result.degraded,
        reason=result.reason,
        app_id=app_id,
        workflow_ids=[hit.workflow.id for hit in result.hits],
        trusted_ids=[hit.workflow.id for hit in result.hits if hit.trusted],
        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    logger.info(
        "RAG: %d workflow truy xuất (%d xác nhận) cho app %s trong %.0fms",
        len(knowledge.workflow_ids),
        len(knowledge.trusted_ids),
        app_id,
        knowledge.elapsed_ms,
    )
    return knowledge


def _retrieve_remote(
    base: str,
    goal: str,
    app_package: str | None,
    run_id: str | None,
    started: float,
) -> KnowledgeContext:
    """Ask a console elsewhere for the knowledge block.  Never raises.

    This is the whole point of the split deployment: a tester machine installs
    ARTEMIS and plugs in a phone, and nothing else.  It holds no PostgreSQL and
    no embedding key; it asks the console -- which has both -- for the exact
    block that console would have injected itself.

    A console that is down or unreachable means *no knowledge*, which is the
    same degradation as a missing database: the run proceeds with the prompt it
    would have had before RAG existed.
    """
    elapsed = lambda: round((time.perf_counter() - started) * 1000, 2)  # noqa: E731

    headers = {"Content-Type": "application/json", "User-Agent": REMOTE_USER_AGENT}
    key = (os.getenv(REMOTE_KEY_ENV) or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps({"goal": goal, "app_package": app_package, "run_id": run_id})
    request = urllib.request.Request(
        f"{base}/api/knowledge/context",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        timeout = float(os.getenv(REMOTE_TIMEOUT_ENV, "20"))
    except ValueError:
        timeout = 20.0

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        logger.warning("Console từ chối truy xuất kiến thức (%s): %s", exc.code, detail)
        return KnowledgeContext(
            degraded=True, reason=f"Console trả mã {exc.code}: {detail}", elapsed_ms=elapsed()
        )
    except Exception as exc:  # noqa: BLE001 - unreachable console is not a failed run
        logger.warning("Không gọi được console %s: %s", base, exc)
        return KnowledgeContext(
            degraded=True, reason=f"Không gọi được console {base}: {exc}", elapsed_ms=elapsed()
        )

    if not isinstance(data, dict):
        return KnowledgeContext(
            degraded=True, reason="Console trả dữ liệu không phải JSON object", elapsed_ms=elapsed()
        )

    kb = data.get("kb") if isinstance(data.get("kb"), dict) else {}
    return KnowledgeContext(
        text=str(data.get("prompt") or ""),
        found=bool(data.get("found")),
        degraded=bool(kb.get("degraded")),
        reason=str(kb.get("reason") or f"Console {base}"),
        app_id=kb.get("app_id"),
        workflow_ids=list(kb.get("workflows") or []),
        trusted_ids=list(kb.get("trusted") or []),
        source="console",
        elapsed_ms=elapsed(),
    )


def attach(ctx: object, knowledge: KnowledgeContext) -> None:
    """Put the block and its metadata on the run context.

    Only *trusted* guidance ever reaches ``knowledge_context``: the text comes
    from :func:`artemis.knowledge.context.build_context`, which renders an
    unconfirmed match as a name-only pointer without steps.
    """
    try:
        setattr(ctx, KNOWLEDGE_CONTEXT_ATTR, knowledge.text if knowledge.injected else "")
        setattr(ctx, KNOWLEDGE_META_ATTR, knowledge.meta())
    except Exception as exc:  # noqa: BLE001 - a context that refuses the attribute is fine
        logger.info("Không gắn được knowledge context: %s", exc)


def context_text(ctx: object) -> str:
    """The knowledge block for this run, or ``""``.

    Missing, ``None`` and whitespace-only all mean "no knowledge", so a run
    without RAG renders exactly the prompt it rendered before this feature
    existed.
    """
    return str(getattr(ctx, KNOWLEDGE_CONTEXT_ATTR, "") or "").strip()


def context_meta(ctx: object) -> dict:
    meta = getattr(ctx, KNOWLEDGE_META_ATTR, None)
    return dict(meta) if isinstance(meta, dict) else {}


def record_outcome(
    run_id: str | None, outcome: dict, service: KnowledgeService | None = None
) -> int:
    """Write the run outcome back onto its retrieval log.  Never raises.

    This is what turns "a workflow was retrieved" into "the workflow we injected
    was used and the run passed" -- the question the UI actually needs answered.
    """
    if not run_id:
        return 0
    try:
        service = service or get_service()
        if not service.available:
            return 0
        return service._store.record_outcome(run_id, outcome)
    except Exception as exc:  # noqa: BLE001
        logger.info("Không ghi được outcome cho run %s: %s", run_id, exc)
        return 0


def usage_report(run_id: str, service: KnowledgeService | None = None) -> dict:
    """Retrieval + outcome for one run, for the UI.  Never raises."""
    try:
        service = service or get_service()
        if not service.available:
            return {"run_id": run_id, "degraded": True, "logs": []}
        return {
            "run_id": run_id,
            "degraded": False,
            "logs": service._store.list_retrieval_logs(run_id),
        }
    except Exception as exc:  # noqa: BLE001
        return {"run_id": run_id, "degraded": True, "error": str(exc), "logs": []}
