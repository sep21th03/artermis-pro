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

"""Tool ``kb_lookup``: agent tự hỏi knowledge base **giữa lúc chạy**.

**Vì sao cần.** Đo trên máy thật 29/10/2026 (task "đăng nhập 0999 → chuyển nhà Mesh Tạo 2 → tạo
ngữ cảnh chạy khi ấn"): log ghi ``RAG: không chèn kiến thức — Prompt đã có khối kiến thức do
console ghép sẵn``, tức là suốt run **không có lần truy hồi nào**. Khi UI khác dự kiến (modal chuyển
nhà bật lên), agent không có đường nào để hỏi "modal này là gì" — nó chỉ có khối kiến thức đã ghép
ở đầu phiên, nằm cuối một prompt 4.000+ ký tự. Kết quả: agent tự bấm Đăng xuất thay vì chuyển tài
khoản, và bị safety net chặn 3 lần rồi hết giờ.

Tool này là đường hỏi đó: truy hồi **theo yêu cầu**, có bộ lọc cấu trúc (id, namespace, loại UI,
màn hình, tag) nên hỏi được "modal nào thuộc màn hình home" — câu mà tìm kiếm ngữ nghĩa không trả
lời được vì không có văn bản nào để so.

Không có KB thứ hai: mọi thứ đi qua :class:`artemis.knowledge.service.KnowledgeService` (hoặc
console từ xa qua ``POST /api/kb/retrieve``, đúng cách bản triển khai tách rời vẫn làm). KB hỏng ⇒
trả lời "không có hướng dẫn", không bao giờ ném lỗi làm hỏng run.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel, Field

from artemis.core.tool_failure import ToolFailure
from artemis.knowledge.filters import KnowledgeFilter, parse_workflow_ids
from artemis.knowledge.integration import remote_api_url
from artemis.knowledge.text import strip_secrets
from artemis.data_engine.trace import trace_langchain_tool
from artemis.tools.base import ArtemisTool
from artemis.tools.tool_wrapper import ToolWrapper
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

#: Trần ký tự trả về cho agent. Bằng chứng từ prompt thật: khối kiến thức 4.000+ ký tự ở đầu
#: phiên bị "trôi" khỏi chú ý; một câu trả lời tra cứu phải ngắn hơn nhiều mới đọc được.
LOOKUP_MAX_CHARS_ENV = "ARTEMIS_KB_LOOKUP_MAX_CHARS"
DEFAULT_LOOKUP_MAX_CHARS = 2000

#: Trần thời gian gọi console từ xa (giây).
LOOKUP_TIMEOUT_ENV = "ARTEMIS_KB_API_TIMEOUT"
DEFAULT_LOOKUP_TIMEOUT = 20.0


def lookup_max_chars() -> int:
    try:
        return max(400, int(os.getenv(LOOKUP_MAX_CHARS_ENV, str(DEFAULT_LOOKUP_MAX_CHARS))))
    except ValueError:
        return DEFAULT_LOOKUP_MAX_CHARS


def _render_result(payload: dict, *, max_chars: int) -> str:
    """Chuyển kết quả tra cứu thành khối chữ cho agent đọc.

    Trả về cả trường hợp **không tìm thấy**: đó là câu trả lời hợp lệ và quan trọng nhất — nếu
    im lặng, agent sẽ tự nghĩ ra cách làm (đúng lỗi đã xảy ra với bước chuyển tài khoản).
    """
    found = bool(payload.get("found"))
    reason = str(payload.get("reason") or "").strip()
    hits = payload.get("hits") or []
    filters = payload.get("filters") or {}
    header = "[KB] "
    if filters:
        shown = ", ".join(f"{key}={value}" for key, value in filters.items() if key != "origin")
        header += f"lọc [{shown}] · "
    if not found:
        return ToolFailure(
            f"{header}không có hướng dẫn nào khớp."
            + (f" Lý do: {reason}" if reason else "")
            + " Không được tự nghĩ ra cách làm khác; hãy quan sát màn hình rồi báo lại."
        )
    lines = [f"{header}{len(hits)} mục khớp:"]
    for hit in hits:
        lines.append(f"- {hit.get('workflow_id')} — {hit.get('name')} [{hit.get('status')}]")
        if hit.get("type"):
            scope = ", ".join(
                part
                for part in (
                    f"loại {hit.get('type')}",
                    f"màn hình {hit.get('screen')}" if hit.get("screen") else "",
                )
                if part
            )
            lines.append(f"  {scope}")
        if hit.get("guards"):
            conditions = "; ".join(str(item) for item in hit["guards"][:3])
            lines.append(f"  điều kiện theo source: {conditions}")
        for step in hit.get("steps") or []:
            target = step.get("target") or ""
            expected = step.get("expected_state") or ""
            lines.append(
                f"  bước: {step.get('action')} «{target}»" + (f" ⇒ {expected}" if expected else "")
            )
        if hit.get("verified") is False:
            lines.append("  (bản nháp chưa xác minh trên thiết bị — phải kiểm bằng UI thật)")
    text = strip_secrets("\n".join(lines))
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…(đã cắt bớt; hỏi hẹp hơn nếu cần chi tiết)"
    return text


def _remote_lookup(
    base: str,
    *,
    query: str,
    app_id: str,
    app_package: str,
    filters: KnowledgeFilter,
    max_results: int,
    run_id: str | None,
    max_chars: int,
) -> tuple[str | None, str]:
    """Hỏi console từ xa. Trả ``(khối chữ, lý do không dùng được)``.

    **Vì sao trả "không dùng được" thay vì một câu lỗi.** Đo 29/10/2026 trên máy này: ``.env`` đặt
    ``ARTEMIS_KB_API=https://sep21th03.com/artemis``, nhưng bản console đang chạy ở đó **không có**
    router ``/api/kb/*`` (mọi đường trả 404). Nếu tool coi đó là câu trả lời thì mỗi lần agent hỏi KB
    đều nhận "console trả mã 404" và KB trở thành vô dụng trong run thật — dù máy tester vẫn có
    tunnel + ``ARTEMIS_KB_DSN`` dùng được. Nên: endpoint hỏng ⇒ trả lý do để nơi gọi thử KB local;
    chỉ khi remote *trả lời* (kể cả ``found=false``) thì mới coi là câu trả lời.
    """
    headers = {"Content-Type": "application/json", "User-Agent": "ARTEMIS/1.0"}
    key = (os.getenv("ARTEMIS_KB_API_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "query": query,
        "app_id": app_id,
        "app_package": app_package or None,
        "top_k": max_results,
        "run_id": run_id,
        **filters.as_request_dict(),
    }
    request = urllib.request.Request(
        f"{base}/api/kb/retrieve",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        timeout = float(os.getenv(LOOKUP_TIMEOUT_ENV, str(DEFAULT_LOOKUP_TIMEOUT)))
    except ValueError:
        timeout = DEFAULT_LOOKUP_TIMEOUT
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 404/403/5xx: endpoint không tồn tại, bị chặn, hoặc bản console lỗi — đều là *hạ tầng*,
        # không phải câu trả lời của KB.
        detail = exc.read().decode("utf-8", "replace")[:120]
        return None, f"console {base} trả mã {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001 - KB hỏng không phải run hỏng
        return None, f"không gọi được console {base}: {exc}"
    return _render_result(_hits_from_api(payload), max_chars=max_chars), ""


def _hit_payload(workflow: Any, *, trusted: bool = True) -> dict:
    """Một mục KB ở dạng agent đọc được: định danh, phạm vi, điều kiện, bước."""
    metadata = dict(getattr(workflow, "metadata", None) or {})
    edge = dict(metadata.get("nav_edge") or {})
    guards = [str(item.get("text") or "") for item in (edge.get("guards") or []) if item]
    return {
        "workflow_id": workflow.id,
        "name": workflow.name,
        "status": workflow.status,
        "trusted": trusted,
        "type": metadata.get("type"),
        "screen": metadata.get("screen"),
        "guards": guards,
        "verified": edge.get("verified"),
        "steps": [
            {
                "action": getattr(step, "action", ""),
                "target": getattr(step, "target", ""),
                "expected_state": getattr(step, "expected_state", ""),
            }
            for step in (getattr(workflow, "steps", None) or [])
        ],
    }


def _hits_from_api(payload: dict) -> dict:
    """Chuẩn hoá câu trả lời của ``POST /api/kb/retrieve`` về cùng dạng với đường local."""
    return {
        "found": payload.get("found"),
        "reason": payload.get("reason"),
        "filters": payload.get("filters") or {},
        "hits": payload.get("hits") or [],
    }


def lookup_knowledge(
    query: str = "",
    *,
    workflow_id: str = "",
    namespace: str = "",
    ui_type: str = "",
    screen: str = "",
    tags: str = "",
    app_id: str = "",
    app_package: str = "",
    run_id: str | None = None,
    max_results: int = 5,
    max_chars: int | None = None,
) -> str:
    """Tra cứu KB theo yêu cầu. **Không bao giờ ném lỗi** — trả chữ cho agent.

    Đường đi giống hệt lúc bắt đầu run: có ``ARTEMIS_KB_API`` thì hỏi console (bản cài trên máy
    tester không có cơ sở dữ liệu), không thì dùng KB local.
    """
    budget = max_chars or lookup_max_chars()
    filters = KnowledgeFilter(
        ids=parse_workflow_ids(workflow_id),
        namespaces=namespace,
        types=ui_type,
        screens=screen,
        tags=tags,
        origin="operator kb_lookup",
    )
    query = (query or "").strip()
    if not query and filters.is_empty:
        return ToolFailure(
            "[KB] cần ít nhất một trong: query, workflow_id, namespace, type, screen, tags."
        )

    remote = remote_api_url()
    if remote:
        text, unavailable_reason = _remote_lookup(
            remote,
            query=query,
            app_id=app_id,
            app_package=app_package,
            filters=filters,
            max_results=max(1, int(max_results or 5)),
            run_id=run_id,
            max_chars=budget,
        )
        if text is not None:
            return text
        # Endpoint từ xa không dùng được ⇒ thử KB local (nếu máy này có DSN + tunnel). Đây là điều
        # đã xảy ra thật: console trên VPS chưa có router ``/api/kb/*``, nên nếu không fallback thì
        # tool trả 404 cho mọi lần hỏi KB trong run thật.
        logger.warning(f"kb_lookup: {unavailable_reason}; thử KB local")

    try:
        from artemis.knowledge.integration import resolve_app_id
        from artemis.knowledge.service import get_service

        service = get_service()
        if not service.available:
            return ToolFailure(f"[KB] chưa sẵn sàng: {service.degraded_reason or 'không rõ lý do'}")
        resolved = app_id or resolve_app_id(app_package, service) or ""
        if not resolved:
            return ToolFailure(
                "[KB] chưa xác định được app đang test (thiếu app_id và không tra được package)."
            )
        result = service.retrieve(
            query,
            resolved,
            top_k=max(1, int(max_results or 5)),
            run_id=run_id,
            filters=filters,
        )
    except Exception as exc:  # noqa: BLE001 - KB hỏng không phải run hỏng
        logger.warning(f"kb_lookup lỗi: {exc}")
        return ToolFailure(f"[KB] tra cứu lỗi: {exc}")

    return _render_result(
        {
            "found": result.found,
            "reason": result.reason,
            "filters": result.filters,
            "hits": [_hit_payload(hit.workflow, trusted=hit.trusted) for hit in result.hits],
        },
        max_chars=budget,
    )


class KbLookupArgs(BaseModel):
    """Tham số tra cứu KB. Mọi trường đều tùy chọn trừ khi phải có ít nhất một."""

    query: str = Field(
        default="",
        description=(
            "What to look up, in the tester's language (e.g. 'chuyển nhà', 'mở ngữ cảnh chạy"
            " khi ấn'). Leave empty when the lookup is purely structural (workflow_id /"
            " namespace / type / screen)."
        ),
    )
    workflow_id: str = Field(
        default="",
        description="Exact workflow id(s) to fetch, comma-separated (e.g. 'auth.switch_account').",
    )
    namespace: str = Field(
        default="",
        description=(
            "Id namespace to search inside, comma-separated. Known namespaces:"
            " screen, navigation, modal, bottomsheet, dialog, popup, action, workflow, auth."
        ),
    )
    type: str = Field(
        default="",
        description=(
            "Kind of UI the entry describes: screen, navigation, modal, bottomsheet,"
            " dialog, popup, workflow."
        ),
    )
    screen: str = Field(
        default="",
        description=(
            "Screen the entry belongs to (the KB stores this in metadata.screen, e.g. 'home')."
            " Use it when an unexpected modal/bottomsheet appeared on a known screen."
        ),
    )
    tags: str = Field(
        default="",
        description="Tag filter, comma-separated (e.g. 'nav-graph').",
    )
    max_results: int = Field(default=5, ge=1, le=20, description="Maximum entries to return.")


KB_LOOKUP_DESCRIPTION = (
    "[KB] Look up the tested app's knowledge base on demand: verified workflows, screens,"
    " navigation edges (with the conditions found in the app source) and modal/bottomsheet"
    " entries. Use it when the screen is not what the current subgoal expects — an unexpected"
    " modal, bottomsheet, dialog or a screen you have no instructions for — or when you need the"
    " exact steps of one workflow. Prefer filters (workflow_id / namespace / type / screen) over"
    " free text: they are exact, cost no embedding call, and answer questions like 'which modal"
    " belongs to screen home'. An empty answer is a real answer: it means the KB has no"
    " guidance, so observe the screen instead of inventing a path. Entries marked as drafts are"
    " source-derived and must still be verified against the real UI before you rely on them."
)


class KbLookupTool(ArtemisTool):
    """Tra cứu knowledge base theo yêu cầu, giữa lúc chạy."""

    def __init__(self) -> None:
        super().__init__(
            name="kb_lookup",
            description=KB_LOOKUP_DESCRIPTION,
            args_schema=KbLookupArgs,
            category="memory",
        )

    # pylint: disable=too-many-arguments
    async def execute(
        self,
        driver: Any = None,  # pylint: disable=unused-argument
        ctx: Any = None,
        query: str = "",
        workflow_id: str = "",
        namespace: str = "",
        type: str = "",  # noqa: A002 - tên trường theo schema công khai
        screen: str = "",
        tags: str = "",
        max_results: int = 5,
        **kwargs: Any,
    ) -> str:
        app_package = ""
        setup = getattr(ctx, "execution_setup", None)
        if setup is not None and hasattr(setup, "get_locked_app_package"):
            try:
                app_package = setup.get_locked_app_package() or ""
            except Exception:  # noqa: BLE001 - thiếu package chỉ làm truy vấn hẹp hơn
                app_package = ""
        return lookup_knowledge(
            query,
            workflow_id=workflow_id,
            namespace=namespace,
            ui_type=type,
            screen=screen,
            tags=tags,
            app_id=str(getattr(ctx, "kb_app_id", "") or ""),
            app_package=app_package,
            run_id=str(getattr(ctx, "run_id", "") or "") or None,
            max_results=int(max_results or 5),
        )


#: Một instance dùng chung: tool này không giữ trạng thái giữa các run, và
#: ``to_langchain_tool``/``to_tool_declaration`` đều là hàm thuần của schema.
KB_LOOKUP_TOOL = KbLookupTool()


def kb_lookup_available(ctx: Any = None) -> bool:
    """Tool chỉ được gắn khi thật sự có KB để hỏi.

    Prompt quảng cáo một tool không tồn tại thì model gọi vào hư không rồi mất lượt — cùng luật
    đang áp cho ``search_history`` và ``video_analyzer``.
    """
    if remote_api_url():
        return True
    try:
        from artemis.knowledge.service import get_service

        return bool(get_service().available)
    except Exception:  # noqa: BLE001 - không dựng được KB thì coi như không có
        return False


def get_kb_lookup_tool(ctx: Any) -> Any:
    """``kb_lookup`` dưới dạng LangChain tool, có trace (giống các tool lịch sử)."""
    return trace_langchain_tool(KB_LOOKUP_TOOL.to_langchain_tool(ctx), ctx)


def kb_lookup_declaration() -> Any:
    """Khai báo cho Flash (Google GenAI FunctionDeclaration)."""
    return KB_LOOKUP_TOOL.to_tool_declaration()


kb_lookup_wrapper = ToolWrapper(
    tool_fn_getter=get_kb_lookup_tool,
    on_success_fn=lambda *a, **k: "Đã tra knowledge base",
    on_failure_fn=lambda err: f"kb_lookup lỗi: {err}",
    is_available_fn=kb_lookup_available,
)

KB_LOOKUP_WRAPPERS: tuple[ToolWrapper, ...] = (kb_lookup_wrapper,)

KB_LOOKUP_TOOL_NAME = "kb_lookup"

__all__ = [
    "KB_LOOKUP_DESCRIPTION",
    "KB_LOOKUP_TOOL",
    "KB_LOOKUP_WRAPPERS",
    "kb_lookup_available",
    "kb_lookup_declaration",
    "kb_lookup_wrapper",
    "KB_LOOKUP_TOOL_NAME",
    "DEFAULT_LOOKUP_MAX_CHARS",
    "KbLookupArgs",
    "KbLookupTool",
    "get_kb_lookup_tool",
    "lookup_knowledge",
    "lookup_max_chars",
]
