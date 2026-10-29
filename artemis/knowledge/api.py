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

"""HTTP surface for the knowledge base.

Mounted by the Test Console (``apps/test_console/server.py``) so the workflow
editor lives in the UI testers already use rather than in a separate admin app.

Every endpoint is a thin wrapper over :class:`KnowledgeService`: validation lives
in the pydantic models, trust decisions live in retrieval, and this module only
translates between JSON and those.

Two rules the handlers enforce because they are easy to get wrong at the edge:

* A knowledge-base outage answers ``503`` with ``degraded: true``, never a 500 --
  the UI must be able to say "the KB is down" rather than "the console broke".
* Workflow content is never echoed back into a prompt from here; retrieval does
  that, with redaction.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError

from artemis.knowledge.models import Workflow
from artemis.knowledge.service import get_service
from artemis.knowledge.stores.base import KnowledgeUnavailable

router = APIRouter(prefix="/api/kb", tags=["knowledge"])


class AppRequest(BaseModel):
    id: str
    name: str = ""
    package_name: str | None = None


class VersionRequest(BaseModel):
    app_id: str
    version_label: str
    version_id: str | None = None
    notes: str = ""


class WorkflowRequest(BaseModel):
    """A workflow as submitted by the editor.

    ``id`` is optional because the path already carries it; requiring it would
    make the client send the same value twice and fail on omission before the
    handler could fill it in.
    """

    id: str | None = None
    app_id: str
    version_id: str | None = None
    name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    status: str = "draft"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    change_note: str = ""


class RetrieveRequest(BaseModel):
    query: str = ""
    #: ``app_id`` hoặc ``app_package`` — máy tester chỉ biết package của app đang test, nên đường
    #: từ xa phải tự tra ra app_id (giống hệt ``retrieve_for_run`` lúc bắt đầu run).
    app_id: str = ""
    app_package: str | None = None
    version_id: str | None = None
    top_k: int | None = None
    run_id: str | None = None
    # ── Bộ lọc metadata (tùy chọn) ────────────────────────────────────────
    # Cùng bộ lọc với tool ``kb_lookup`` của agent: cho phép hỏi KB theo cấu trúc
    # ("modal nào của màn hình `home`?") thay vì chỉ theo câu văn. Bỏ trống ⇒ hành vi cũ.
    ids: list[str] | None = None
    namespace: str | list[str] | None = None
    type: str | list[str] | None = None
    tags: str | list[str] | None = None
    screen: str | list[str] | None = None
    status: str | list[str] | None = None


def _store():
    service = get_service()
    if not service.available:
        raise HTTPException(
            status_code=503,
            detail={
                "degraded": True,
                "reason": service.degraded_reason or "Knowledge base chưa sẵn sàng",
            },
        )
    return service


def _guarded(call, *args, **kwargs):
    """Run a store call, turning an outage into 503 rather than a 500."""
    try:
        return call(*args, **kwargs)
    except KnowledgeUnavailable as exc:
        raise HTTPException(status_code=503, detail={"degraded": True, "reason": str(exc)}) from exc


@router.get("/status")
def kb_status() -> dict:
    """Health for the UI: whether retrieval is usable and why not."""
    service = get_service()
    status = service.status()
    status["workflow_count"] = (
        len(_guarded(service._store.list_workflows)) if service.available else 0
    )
    status["database"] = _database_label()
    return status


def _database_label() -> str:
    """Nơi dữ liệu thật sự nằm, đã bỏ mật khẩu.

    Cần hiển thị vì DSN của máy dev trỏ ``127.0.0.1:5433`` — cổng của SSH tunnel
    sang VPS. Nhìn vào rất dễ tưởng là database tại chỗ, trong khi thực tế mọi thứ
    đọc/ghi đều ở trên VPS.
    """
    try:
        from artemis.config.settings import settings

        dsn = str(getattr(settings, "ARTEMIS_KB_DSN", "") or "")
    except Exception:  # noqa: BLE001 - thiếu settings thì chỉ là không có nhãn
        return ""
    if not dsn:
        return "(chưa cấu hình)"
    # postgresql://user:matkhau@host:port/db  ->  user@host:port/db
    without_scheme = dsn.split("://", 1)[-1]
    if "@" in without_scheme:
        credentials, location = without_scheme.rsplit("@", 1)
        user = credentials.split(":", 1)[0]
        return f"{user}@{location}"
    return without_scheme


@router.get("/apps")
def list_apps() -> list[dict]:
    return _guarded(_store()._store.list_apps)


@router.post("/apps")
def upsert_app(req: AppRequest) -> dict:
    return _guarded(_store()._store.upsert_app, req.id, req.name or req.id, req.package_name)


@router.get("/apps/{app_id}/versions")
def list_versions(app_id: str) -> list[dict]:
    return _guarded(_store()._store.list_versions, app_id)


@router.post("/versions")
def upsert_version(req: VersionRequest) -> dict:
    return _guarded(
        _store()._store.upsert_version, req.app_id, req.version_label, req.version_id, req.notes
    )


@router.get("/workflows")
def list_workflows(
    app_id: str | None = Query(default=None), status: str | None = Query(default=None)
) -> list[dict]:
    workflows = _guarded(_store()._store.list_workflows, app_id, status)
    return [workflow.model_dump() for workflow in workflows]


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str) -> dict:
    workflow = _guarded(_store()._store.get_workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"Không có workflow {workflow_id!r}")
    return workflow.model_dump()


@router.put("/workflows/{workflow_id}")
def save_workflow(workflow_id: str, req: WorkflowRequest) -> dict:
    """Create or replace a workflow, then index it.

    Validation errors are the tester's to fix, so they come back as 422 with
    pydantic's message rather than as a 500.
    """
    service = _store()
    payload = req.model_dump(exclude={"change_note"})
    payload["id"] = workflow_id
    try:
        workflow = Workflow(**payload)
    except ValidationError as exc:
        # ``exc.errors()`` carries a ``ctx`` holding the raised exception and an
        # ``input`` echo; neither is JSON-serialisable and the input can be large.
        # Serialising it unmodified turns a validation error into a 500.
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": list(error.get("loc", ())),
                    "msg": error.get("msg", ""),
                    "type": error.get("type", ""),
                }
                for error in exc.errors(include_url=False)
            ],
        ) from exc

    saved = _guarded(service._store.save_workflow, workflow, req.change_note)
    embedded = service.index_workflow(saved)
    return {**saved.model_dump(), "embedded": embedded}


@router.delete("/workflows/{workflow_id}")
def delete_workflow(workflow_id: str) -> dict:
    deleted = _guarded(_store()._store.delete_workflow, workflow_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Không có workflow {workflow_id!r}")
    return {"deleted": True, "id": workflow_id}


@router.get("/workflows/{workflow_id}/revisions")
def list_revisions(workflow_id: str) -> list[dict]:
    return _guarded(_store()._store.revisions, workflow_id)


@router.post("/retrieve")
def retrieve(req: RetrieveRequest) -> dict:
    """Try a query the way a run would, so a tester can check retrieval first."""
    from artemis.knowledge.context import build_context
    from artemis.knowledge.filters import filter_from_payload
    from artemis.knowledge.integration import resolve_app_id

    service = _store()
    app_id = (req.app_id or "").strip()
    if not app_id and req.app_package:
        app_id = resolve_app_id(req.app_package, service) or ""
    if not app_id:
        raise HTTPException(
            status_code=400,
            detail="Thiếu app_id (hoặc app_package để tra ra app_id) — không truy hồi mù.",
        )
    filters = filter_from_payload(
        req.model_dump(exclude={"query", "app_id", "app_package", "version_id", "top_k", "run_id"}),
        origin="api /retrieve",
    )
    result = service.retrieve(
        req.query,
        app_id,
        version_id=req.version_id,
        top_k=req.top_k,
        run_id=req.run_id,
        filters=filters,
    )
    return {
        "found": result.found,
        "reason": result.reason,
        "degraded": result.degraded,
        "timings_ms": result.timings_ms,
        "filters": result.filters,
        "app_id": app_id,
        # The exact block a run would inject -- built from this result, not by
        # retrieving a second time (which would double the embedding cost).
        "context": build_context(result, service.context_max_chars),
        "hits": [_hit_payload(hit) for hit in result.hits],
    }


def _hit_payload(hit: Any) -> dict:
    """Một mục KB kèm phạm vi/điều kiện/bước để nơi gọi **ở xa** hiển thị được như đường local.

    Chỉ trả ``workflow_id``/``score`` thì console từ xa không nói được "modal này thuộc màn hình
    nào" — mà đó chính là câu agent cần trả lời khi gặp UI ngoài dự kiến.
    """
    workflow = hit.workflow
    metadata = dict(getattr(workflow, "metadata", None) or {})
    edge = dict(metadata.get("nav_edge") or {})
    return {
        "workflow_id": workflow.id,
        "name": workflow.name,
        "status": workflow.status,
        "score": hit.score,
        "channels": hit.channels,
        "trusted": hit.trusted,
        "type": metadata.get("type"),
        "screen": metadata.get("screen"),
        "guards": [str(item.get("text") or "") for item in (edge.get("guards") or []) if item],
        "verified": edge.get("verified"),
        "steps": [
            {
                "action": step.action,
                "target": step.target,
                "expected_state": step.expected_state,
            }
            for step in workflow.steps
        ],
    }


@router.get("/logs")
def list_logs(run_id: str | None = Query(default=None), limit: int = Query(default=50, le=500)):
    return _guarded(_store()._store.list_retrieval_logs, run_id, limit)


@router.post("/reindex")
def reindex(app_id: str | None = Query(default=None)) -> dict:
    """Rebuild every vector for the configured model (after a model change)."""
    try:
        return _store().reindex(app_id=app_id)
    except KnowledgeUnavailable as exc:
        raise HTTPException(status_code=503, detail={"degraded": True, "reason": str(exc)}) from exc
