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

"""API đo một span: ``perf_span`` / ``record_event`` / ``@perf_traced``.

Hai yêu cầu kỹ thuật định hình toàn bộ module này:

1. **Đo bằng ``time.perf_counter()``** (đồng hồ đơn điệu, không nhảy theo giờ hệ
   thống) nhưng vẫn ghi timestamp thực bằng ``time.time()`` để đối chiếu với log khác.
2. **Không làm chậm agent đáng kể.**  Vì vậy mỗi span chỉ tạo một object, một lần ghi
   JSONL nhỏ (append + flush), không tính toán nặng, không gọi mạng.  Khi lớp đo tắt,
   đường đi chỉ còn một phép kiểm ``recorder is None``.

Mọi hàm ở đây **không bao giờ raise** — lỗi đo không được phép làm gãy task.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import Token
from typing import Any, AsyncIterator, Callable, Iterator

from artemis.perf import context as perf_context
from artemis.perf.models import (
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    PerfEvent,
    PerfStage,
)
from artemis.perf.recorder import PerfRecorder

logger = logging.getLogger(__name__)


def new_correlation_id() -> str:
    """Id tương quan cho một hành động (LLM quyết định → ADB → quan sát lại)."""
    return uuid.uuid4().hex[:12]


def _resolve_recorder(recorder: PerfRecorder | None) -> PerfRecorder | None:
    return recorder if recorder is not None else perf_context.current_recorder()


class PerfSpan:
    """Một span đang mở; ghi lại khi thoát khỏi ``perf_span``."""

    def __init__(
        self,
        *,
        recorder: PerfRecorder,
        event: PerfEvent,
        perf_start: float,
        wall_start: float,
    ) -> None:
        self._recorder = recorder
        self._perf_start = perf_start
        self._wall_start = wall_start
        self.event = event
        self._closed = False

    # ── Bổ sung số đo khi đã biết ───────────────────────────────────────
    def set(self, **fields: Any) -> PerfSpan:
        """Cập nhật trường của sự kiện (tokens, model, retry_count, …)."""
        for key, value in fields.items():
            if value is None:
                continue
            if hasattr(self.event, key):
                setattr(self.event, key, value)
        return self

    def note(self, **detail: Any) -> PerfSpan:
        """Thêm chi tiết phụ (đã được lọc bí mật ở tầng ghi)."""
        self.event.detail.update(detail)
        return self

    def fail(self, error: BaseException | str, *, status: str = STATUS_ERROR) -> PerfSpan:
        """Đánh dấu span kết thúc bằng lỗi."""
        if isinstance(error, BaseException):
            self.event.error_type = type(error).__name__
            self.event.error_code = self.event.error_code or _error_code(error)
            message = str(error)
            if message:
                self.event.detail.setdefault("error_message", message[:200])
        else:
            self.event.error_type = str(error)[:120]
        self.event.status = status
        return self

    def timeout(self, message: str = "") -> PerfSpan:
        return self.fail(message or "timeout", status=STATUS_TIMEOUT)

    # ── Kết thúc ────────────────────────────────────────────────────────
    def finish(self, status: str | None = None) -> PerfEvent | None:
        """Đóng span và ghi sự kiện; gọi hai lần là vô hại."""
        if self._closed:
            return self.event
        self._closed = True
        now_perf = time.perf_counter()
        self.event.duration_ms = round((now_perf - self._perf_start) * 1000.0, 3)
        self.event.ended_at = time.time()
        self.event.started_at = self._wall_start
        if status:
            self.event.status = status
        elif self.event.status == "running":
            self.event.status = STATUS_SUCCESS
        try:
            self._recorder.record(self.event)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Không ghi được perf span: %s", exc)
        return self.event


def _error_code(error: BaseException) -> str | None:
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if code is None:
        return None
    return str(code)


def _open_span(
    stage: str | PerfStage,
    event_name: str,
    *,
    recorder: PerfRecorder | None,
    action_type: str | None,
    model: str | None,
    provider: str | None,
    endpoint: str | None,
    phase: str,
    role: str | None,
    iteration: int | None,
    step_id: str | None,
    correlation_id: str | None,
    detail: dict[str, Any] | None,
) -> tuple[PerfSpan | None, Token | None, Token | None, Token | None]:
    """Tạo span + đẩy ngăn xếp cha.  Trả ``(span, token_stack, token_role, token_corr)``."""
    active = _resolve_recorder(recorder)
    if active is None:
        return None, None, None, None

    event = PerfEvent(
        session_id=active.session_id,
        step_id=step_id or perf_context.current_step_id(),
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=perf_context.current_parent_span(),
        correlation_id=correlation_id or perf_context.current_correlation_id(),
        event_name=event_name,
        stage=str(stage),
        phase=phase,
        agent_role=role or perf_context.current_agent_role(),
        action_type=action_type,
        model=model,
        provider=provider,
        endpoint=endpoint,
        iteration=iteration if iteration is not None else perf_context.current_iteration(),
        status="running",
        detail=dict(detail or {}),
    )
    perf_start = time.perf_counter()
    wall_start = time.time()
    event.started_at = wall_start

    stack_token = perf_context.SPAN_STACK.set((*perf_context.SPAN_STACK.get(), event.span_id))
    role_token = perf_context.CURRENT_AGENT_ROLE.set(event.agent_role) if role else None
    corr_token = (
        perf_context.CURRENT_CORRELATION_ID.set(event.correlation_id) if correlation_id else None
    )
    span = PerfSpan(recorder=active, event=event, perf_start=perf_start, wall_start=wall_start)
    if event.parent_span_id is None:
        # Span gốc: ghi ngay một dòng ``running`` để lúc session đang chạy vẫn có khoảng
        # thời gian bao trùm (nếu không, mọi thứ bị tính là idle).  Span con không cần vì
        # đã nằm trong khoảng của span gốc.
        try:
            active.mark_running(event.model_copy(deep=True))
        except Exception:  # noqa: BLE001 - đo hiệu năng không được cản công việc
            pass
    return span, stack_token, role_token, corr_token


def _close_span(
    span: PerfSpan | None,
    tokens: tuple[Token | None, Token | None, Token | None],
    *,
    status: str | None = None,
) -> None:
    if span is not None:
        span.finish(status)
    stack_token, role_token, corr_token = tokens
    if corr_token is not None:
        perf_context.CURRENT_CORRELATION_ID.reset(corr_token)
    if role_token is not None:
        perf_context.CURRENT_AGENT_ROLE.reset(role_token)
    if stack_token is not None:
        perf_context.SPAN_STACK.reset(stack_token)


@asynccontextmanager
async def perf_span(
    stage: str | PerfStage,
    event_name: str,
    *,
    action_type: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    endpoint: str | None = None,
    phase: str = "run",
    role: str | None = None,
    iteration: int | None = None,
    step_id: str | None = None,
    correlation_id: str | None = None,
    detail: dict[str, Any] | None = None,
    recorder: PerfRecorder | None = None,
) -> AsyncIterator[PerfSpan | None]:
    """Đo một công đoạn bất đồng bộ.

    Dùng::

        async with perf_span(PerfStage.SCREENSHOT, "screenshot") as span:
            data = await driver.get_screen_data()
            if span:
                span.note(bytes=len(data.screenshot_bytes))

    Khi lớp đo tắt, ``span`` là ``None`` và overhead chỉ còn một phép tra contextvar.
    """
    span, *tokens = _open_span(
        stage,
        event_name,
        recorder=recorder,
        action_type=action_type,
        model=model,
        provider=provider,
        endpoint=endpoint,
        phase=phase,
        role=role,
        iteration=iteration,
        step_id=step_id,
        correlation_id=correlation_id,
        detail=detail,
    )
    token_tuple = (tokens[0], tokens[1], tokens[2]) if span is not None else (None, None, None)
    try:
        yield span
    except BaseException as exc:  # noqa: BLE001 - ghi lại rồi cho lỗi đi tiếp
        if span is not None:
            span.fail(exc)
        _close_span(span, token_tuple)
        raise
    else:
        _close_span(span, token_tuple)


@contextmanager
def perf_span_sync(
    stage: str | PerfStage,
    event_name: str,
    *,
    action_type: str | None = None,
    phase: str = "run",
    role: str | None = None,
    step_id: str | None = None,
    detail: dict[str, Any] | None = None,
    recorder: PerfRecorder | None = None,
) -> Iterator[PerfSpan | None]:
    """Bản đồng bộ của :func:`perf_span` (dùng trong luồng chặn)."""
    span, *tokens = _open_span(
        stage,
        event_name,
        recorder=recorder,
        action_type=action_type,
        model=None,
        provider=None,
        endpoint=None,
        phase=phase,
        role=role,
        iteration=None,
        step_id=step_id,
        correlation_id=None,
        detail=detail,
    )
    token_tuple = (tokens[0], tokens[1], tokens[2]) if span is not None else (None, None, None)
    try:
        yield span
    except BaseException as exc:  # noqa: BLE001
        if span is not None:
            span.fail(exc)
        _close_span(span, token_tuple)
        raise
    else:
        _close_span(span, token_tuple)


def perf_traced(stage: str | PerfStage, event_name: str | None = None, **span_kwargs: Any):
    """Decorator đo một hàm (async hoặc sync) mà không phải sửa thân hàm."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        name = event_name or func.__name__

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                async with perf_span(stage, name, **span_kwargs):
                    return await func(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with perf_span_sync(stage, name, **span_kwargs):
                return func(*args, **kwargs)

        return sync_wrapper

    return decorator


def record_event(
    stage: str | PerfStage,
    event_name: str,
    *,
    duration_ms: float | None = None,
    status: str = STATUS_SUCCESS,
    recorder: PerfRecorder | None = None,
    **fields: Any,
) -> PerfEvent | None:
    """Ghi một sự kiện rời (không có span bao quanh), ví dụ một lần retry đã biết delay."""
    active = _resolve_recorder(recorder)
    if active is None:
        return None
    known = {
        "action_type",
        "model",
        "provider",
        "endpoint",
        "agent_role",
        "phase",
        "step_id",
        "request_id",
        "parent_span_id",
        "correlation_id",
        "iteration",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "retry_count",
        "retry_wait_ms",
        "ttft_ms",
        "error_type",
        "error_code",
        "http_status",
        "ui_fingerprint",
        "ui_changed",
        "detail",
    }
    payload = {key: value for key, value in fields.items() if key in known and value is not None}
    detail = payload.pop("detail", None)
    if "parent_span_id" not in payload:
        # Sự kiện rời nằm trong một span đang mở thì là span con của nó — nhờ vậy thời
        # gian retry không bị tính hai lần (một lần trong span cha, một lần riêng nó).
        parent = perf_context.current_parent_span()
        if parent:
            payload["parent_span_id"] = parent
    event = PerfEvent(
        session_id=active.session_id,
        span_id=uuid.uuid4().hex[:16],
        event_name=event_name,
        stage=str(stage),
        duration_ms=duration_ms,
        status=status,
        detail=dict(detail or {}),
        **payload,
    )
    # Sự kiện rời kết thúc ngay bây giờ: lùi ``started_at`` đúng bằng duration đã khai để
    # khoảng thời gian của nó khớp với số đo.  Nếu không, phần trăm runtime sẽ cộng vượt
    # 100% vì thời gian được tính mà wall-clock không hề chứa nó.
    now = time.time()
    if duration_ms is not None:
        event.started_at = now - (float(duration_ms) / 1000.0)
        event.ended_at = now
    else:
        event.started_at = now
        event.ended_at = None
    return active.record(event)
