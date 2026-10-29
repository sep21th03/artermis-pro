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
import time
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

#: Đọc KB local trước khi hỏi console từ xa (mặc định bật).
#:
#: Hai đường đọc **cùng một cơ sở dữ liệu** (console trỏ vào đúng máy chủ mà tunnel local đang
#: chuyển tiếp), nên đọc local trước không mất thông tin nào mà bỏ được một vòng HTTP. Đặt ``0``
#: nếu console được triển khai trên một cơ sở dữ liệu khác — khi đó giữ thứ tự cũ (remote trước,
#: local là đường dự phòng).
LOCAL_FIRST_ENV = "ARTEMIS_KB_LOCAL_FIRST"

#: Thời gian ngắt mạch remote khi endpoint **chưa được triển khai** (404/403/405/501) — giây.
REMOTE_COOLDOWN_ENV = "ARTEMIS_KB_REMOTE_COOLDOWN"
DEFAULT_REMOTE_COOLDOWN = 900.0

#: Thời gian ngắt mạch remote cho lỗi **tạm thời** (5xx, timeout, mạng) — giây.
REMOTE_TRANSIENT_COOLDOWN_ENV = "ARTEMIS_KB_REMOTE_TRANSIENT_COOLDOWN"
DEFAULT_REMOTE_TRANSIENT_COOLDOWN = 60.0

#: Số lỗi tạm thời **liên tiếp** mới ngắt mạch (404/403 ngắt ngay lần đầu).
REMOTE_TRANSIENT_THRESHOLD = 2

#: Mã HTTP nghĩa là "console này không có router ``/api/kb/*``" ⇒ ngắt mạch dài, thử lại thưa.
REMOTE_NOT_DEPLOYED_CODES = frozenset({403, 404, 405, 501})

#: Hạn dùng của việc nhớ ``package → app_id`` (giây). Đây là dữ liệu **không đổi trong một run**,
#: nhưng vẫn có hạn để một app mới đăng ký không bị bỏ qua mãi.
APP_ID_TTL_ENV = "ARTEMIS_KB_APP_ID_TTL"
DEFAULT_APP_ID_TTL = 300.0


def _env_flag(name: str, *, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _env_seconds(name: str, *, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def lookup_max_chars() -> int:
    try:
        return max(400, int(os.getenv(LOOKUP_MAX_CHARS_ENV, str(DEFAULT_LOOKUP_MAX_CHARS))))
    except ValueError:
        return DEFAULT_LOOKUP_MAX_CHARS


def local_first() -> bool:
    """Có đọc KB local trước khi hỏi console từ xa hay không."""
    return _env_flag(LOCAL_FIRST_ENV, default=True)


#: Nhớ ``package → app_id`` trong tiến trình: mỗi lần tra bằng package tốn ~100–180 ms truy vấn
#: (đo qua tunnel 29/10/2026) mà câu trả lời không đổi trong một run.
_APP_ID_MEMO: dict[str, tuple[str, float]] = {}


def _remembered_app_id(app_package: str, now: float | None = None) -> str | None:
    """``app_id`` đã biết cho package này, nếu còn hạn nhớ."""
    if not app_package:
        return None
    hit = _APP_ID_MEMO.get(app_package)
    if hit is None:
        return None
    value, stamp = hit
    now = time.monotonic() if now is None else now
    if now - stamp >= _env_seconds(APP_ID_TTL_ENV, default=DEFAULT_APP_ID_TTL):
        _APP_ID_MEMO.pop(app_package, None)
        return None
    return value


def _remember_app_id(app_package: str, app_id: str, now: float | None = None) -> None:
    if app_package and app_id:
        _APP_ID_MEMO[app_package] = (app_id, time.monotonic() if now is None else now)


def reset_app_id_memo() -> None:
    """Xoá phần nhớ ``package → app_id`` (test cần trạng thái sạch)."""
    _APP_ID_MEMO.clear()


class RemoteHealth:
    """Cầu dao (circuit breaker) cho endpoint KB từ xa.

    **Vì sao cần.** Đo thật 29/10/2026 trên máy này: ``ARTEMIS_KB_API`` trỏ vào console trên VPS
    nhưng bản console ở đó **không có** router ``/api/kb/*`` ⇒ mọi lần tra đều trả 404, và mỗi lần
    như vậy tốn một vòng HTTP (~1 giây) *trước khi* rơi về KB local. Với KB theo công đoạn, mỗi công
    đoạn có ít nhất một lần tra ⇒ độ trễ đó lặp lại suốt run một cách vô ích.

    Cầu dao giữ đúng hai tính chất cần thiết:

    * **không mất khả năng dùng remote**: hết ``cooldown`` thì đúng **một** lần gọi được phép đi
      (half-open); nếu lần đó thành công, cầu dao đóng lại ngay và remote được dùng bình thường;
    * **không ngắt mạch vì một lần trục trặc**: lỗi tạm thời (5xx/timeout/mạng) cần
      :data:`REMOTE_TRANSIENT_THRESHOLD` lần **liên tiếp** mới ngắt, và ngắt ngắn hơn nhiều so với
      trường hợp endpoint chưa tồn tại.

    Trạng thái sống trong tiến trình (một tiến trình = một sức khoẻ endpoint), không ghi ra đĩa.
    """

    def __init__(
        self,
        *,
        unavailable_cooldown: float = DEFAULT_REMOTE_COOLDOWN,
        transient_cooldown: float = DEFAULT_REMOTE_TRANSIENT_COOLDOWN,
        transient_threshold: int = REMOTE_TRANSIENT_THRESHOLD,
    ) -> None:
        self.unavailable_cooldown = max(0.0, float(unavailable_cooldown))
        self.transient_cooldown = max(0.0, float(transient_cooldown))
        self.transient_threshold = max(1, int(transient_threshold))
        self.open_until: float | None = None
        self.last_reason = ""
        self.last_kind = ""
        self.failures = 0
        self.transient_failures = 0
        self.trips = 0
        self.probes = 0

    def allows(self, now: float | None = None) -> bool:
        """Có được phép gọi remote lúc này không (mạch mở ⇒ chờ hết cooldown rồi thử lại một lần)."""
        if self.open_until is None:
            return True
        if self.remaining(now) <= 0:
            self.probes += 1
            return True
        return False

    def remaining(self, now: float | None = None) -> float:
        """Số giây còn lại của mạch đang mở (0 khi mạch đóng)."""
        if self.open_until is None:
            return 0.0
        return max(0.0, self.open_until - (time.monotonic() if now is None else now))

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        return {
            "open": self.open_until is not None and self.remaining(now) > 0,
            "open_for": round(self.remaining(now), 1),
            "last_reason": self.last_reason,
            "last_kind": self.last_kind,
            "failures": self.failures,
            "transient_failures": self.transient_failures,
            "trips": self.trips,
            "probes": self.probes,
        }

    def record_success(self) -> None:
        if self.open_until is not None:
            logger.info("kb_lookup: KB từ xa hoạt động trở lại; dùng lại bình thường")
        self.open_until = None
        self.last_reason = ""
        self.last_kind = ""
        self.transient_failures = 0

    def record_failure(self, reason: str, *, kind: str, now: float | None = None) -> None:
        """Ghi một lần thất bại và (nếu đủ điều kiện) mở mạch."""
        now = time.monotonic() if now is None else now
        self.failures += 1
        self.last_reason = reason
        self.last_kind = kind
        if kind == "transient":
            self.transient_failures += 1
            if self.transient_failures < self.transient_threshold:
                return
            cooldown = self.transient_cooldown
        else:
            cooldown = self.unavailable_cooldown
        self.open_until = now + cooldown
        self.trips += 1
        logger.warning(
            f"kb_lookup: tạm ngừng gọi KB từ xa {cooldown:.0f}s ({reason}); "
            "vẫn tra KB local nếu máy này có DSN"
        )

    def reset(self) -> None:
        """Xoá toàn bộ trạng thái (dùng cho test và cho lệnh chẩn đoán)."""
        self.open_until = None
        self.last_reason = ""
        self.last_kind = ""
        self.failures = 0
        self.transient_failures = 0
        self.trips = 0
        self.probes = 0


_REMOTE_HEALTH = RemoteHealth()


def remote_health() -> RemoteHealth:
    """Cầu dao dùng chung của tiến trình (``kb_lookup`` không giữ trạng thái nào khác)."""
    return _REMOTE_HEALTH


def reset_remote_health() -> None:
    """Đóng lại cầu dao và xoá số đếm — test cần trạng thái sạch cho từng ca."""
    _REMOTE_HEALTH.reset()


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
) -> tuple[str | None, str, str]:
    """Hỏi console từ xa. Trả ``(khối chữ, lý do không dùng được, loại lỗi)``.

    **Vì sao trả "không dùng được" thay vì một câu lỗi.** Đo 29/10/2026 trên máy này: ``.env`` đặt
    ``ARTEMIS_KB_API=https://sep21th03.com/artemis``, nhưng bản console đang chạy ở đó **không có**
    router ``/api/kb/*`` (mọi đường trả 404). Nếu tool coi đó là câu trả lời thì mỗi lần agent hỏi KB
    đều nhận "console trả mã 404" và KB trở thành vô dụng trong run thật — dù máy tester vẫn có
    tunnel + ``ARTEMIS_KB_DSN`` dùng được. Nên: endpoint hỏng ⇒ trả lý do để nơi gọi thử KB local;
    chỉ khi remote *trả lời* (kể cả ``found=false``) thì mới coi là câu trả lời.

    Loại lỗi (phần tử thứ ba) để :class:`RemoteHealth` chọn mức ngắt mạch: ``"unavailable"`` =
    console không có router (404/403/405/501 — thử lại thưa), ``"transient"`` = 5xx/timeout/mạng.
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
        # 404/403/405/501: bản console này không có router ⇒ không phải câu trả lời của KB.
        # 5xx: bản console lỗi tạm thời — cùng là *hạ tầng*, nhưng đáng thử lại sớm hơn.
        detail = exc.read().decode("utf-8", "replace")[:120]
        kind = "unavailable" if exc.code in REMOTE_NOT_DEPLOYED_CODES else "transient"
        return None, f"console {base} trả mã {exc.code}: {detail}", kind
    except Exception as exc:  # noqa: BLE001 - KB hỏng không phải run hỏng
        return None, f"không gọi được console {base}: {exc}", "transient"
    return _render_result(_hits_from_api(payload), max_chars=max_chars), "", ""


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


def _local_lookup(
    query: str,
    *,
    app_id: str,
    app_package: str,
    filters: KnowledgeFilter,
    max_results: int,
    run_id: str | None,
    max_chars: int,
) -> tuple[str | None, str]:
    """Tra KB local (DSN + tunnel). Trả ``(khối chữ, lý do không dùng được)``.

    Cùng hợp đồng với :func:`_remote_lookup`: chỉ ``(None, lý do)`` khi **không dùng được**, còn
    ``found=false`` là một câu trả lời hợp lệ.
    """
    try:
        from artemis.knowledge.integration import resolve_app_id
        from artemis.knowledge.service import get_service

        service = get_service()
        if not service.available:
            return None, f"KB local chưa sẵn sàng: {service.degraded_reason or 'không rõ lý do'}"
        resolved = app_id or _remembered_app_id(app_package) or ""
        if not resolved:
            resolved = resolve_app_id(app_package, service) or ""
            _remember_app_id(app_package, resolved)
        if not resolved:
            return None, (
                "KB local: chưa xác định được app đang test "
                "(thiếu app_id và không tra được package)"
            )
        result = service.retrieve(
            query,
            resolved,
            top_k=max_results,
            run_id=run_id,
            filters=filters,
        )
    except Exception as exc:  # noqa: BLE001 - KB hỏng không phải run hỏng
        logger.warning(f"kb_lookup local lỗi: {exc}")
        return None, f"KB local tra cứu lỗi: {exc}"

    return (
        _render_result(
            {
                "found": result.found,
                "reason": result.reason,
                "filters": result.filters,
                "hits": [_hit_payload(hit.workflow, trusted=hit.trusted) for hit in result.hits],
            },
            max_chars=max_chars,
        ),
        "",
    )


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

    Thứ tự đường đọc (đổi 29/10/2026, xem :data:`LOCAL_FIRST_ENV`):

    1. **KB local** (DSN + tunnel) — nhanh nhất và cùng cơ sở dữ liệu với console;
    2. **console từ xa** chỉ khi local không dùng được, và chỉ khi **cầu dao** cho phép
       (:class:`RemoteHealth`): một lần 404 ⇒ ngừng gọi 15 phút thay vì trả giá một vòng HTTP ở
       *mọi* lần tra; hết cooldown thì thử lại một lần, thành công là dùng lại bình thường;
    3. không nguồn nào trả lời ⇒ một câu trả lời lỗi gọn (nêu cả hai lý do) cho agent đọc.

    ``found=false`` vẫn là **câu trả lời hợp lệ**, không phải lỗi — agent cần biết là KB không có gì
    thay vì tự nghĩ ra cách làm.
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

    budget_max = max(1, int(max_results or 5))
    prefer_local = local_first()
    local_text, local_reason = (
        _local_lookup(
            query,
            app_id=app_id,
            app_package=app_package,
            filters=filters,
            max_results=budget_max,
            run_id=run_id,
            max_chars=budget,
        )
        if prefer_local
        else (None, "")
    )
    if local_text is not None:
        return local_text

    remote = remote_api_url()
    health = remote_health()
    remote_reason = ""
    if remote:
        if health.allows():
            text, remote_reason, kind = _remote_lookup(
                remote,
                query=query,
                app_id=app_id,
                app_package=app_package,
                filters=filters,
                max_results=budget_max,
                run_id=run_id,
                max_chars=budget,
            )
            if text is not None:
                health.record_success()
                return text
            # Endpoint từ xa không dùng được. Đây là điều đã xảy ra thật: console trên VPS chưa có
            # router ``/api/kb/*`` ⇒ 404 cho mọi lần hỏi. Ghi vào cầu dao để những lần sau trong
            # cùng khoảng cooldown không phải trả giá một vòng HTTP nữa.
            health.record_failure(remote_reason, kind=kind)
        else:
            remote_reason = f"KB từ xa đang bị ngắt mạch còn {health.remaining():.0f}s" + (
                f" ({health.last_reason})" if health.last_reason else ""
            )

    if not prefer_local:
        local_text, local_reason = _local_lookup(
            query,
            app_id=app_id,
            app_package=app_package,
            filters=filters,
            max_results=budget_max,
            run_id=run_id,
            max_chars=budget,
        )
        if local_text is not None:
            return local_text

    reasons = " · ".join(reason for reason in (local_reason, remote_reason) if reason)
    return ToolFailure(f"[KB] không lấy được hướng dẫn: {reasons or 'không có nguồn KB nào'}")


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
