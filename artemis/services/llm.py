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

"""Universal LLM Service for Artemis.

Provides connection-pooled model instantiation, robust retries,
thought stream recording, and role-based dynamic dispatching.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
import functools
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, TypeVar
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel

from artemis.config import (
    PAUSE_FILE,
    get_default_llm_config,
    settings,
)
from artemis.context import ArtemisContext
from artemis.data_engine.trace import CURRENT_TRACE_ID, DataEngineCallbackHandler
from artemis.llm.google import is_google_chat_model, is_google_provider
from artemis.llm.reliability import (
    CircuitBreaker,
    FailureCategory,
    LLMCallError,
    LLMExhaustedError,
    LLMPermanentError,
    classify_failure,
    quota_cooldown,
    retry_policy_for,
)
from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider
from artemis.perf import (
    PerfStage,
    STATUS_RETRY,
    get_recorder,
    perf_span,
    record_event,
)
from artemis.perf import context as perf_context
from artemis.services.token_meter import record_llm_usage
from artemis.llm.structured import (
    ParseFailure,
    StructuredOutputError,
    content_to_text,
    parse_structured,
)
from third_party.mobile_use.llm_service import FallbackRunner, GetLLM, LLMWaitNotice
from third_party.mobile_use.utils.logger import get_logger

# Logger for internal messages
llm_logger = logging.getLogger(__name__)
# Logger for user-facing messages
user_messages_logger = get_logger(__name__)

T = TypeVar("T")

# Retained as an override seam for unit tests. At runtime the active engine must
# be resolved dynamically because DataEngine.start_session() updates the module
# global after this module has already been imported.
_CURRENT_DATA_ENGINE = None

# Provider retry logs do not carry an Artemis trace id.  Keep the active model
# request in async-local state so observable SDK retries and the terminal error
# are emitted as one lifecycle, even when several model calls run concurrently.
_ACTIVE_LLM_REQUEST: ContextVar[dict[str, Any] | None] = ContextVar(
    "active_llm_request", default=None
)


def _begin_llm_request(provider: str | None) -> Token:
    return _ACTIVE_LLM_REQUEST.set(
        {
            "request_id": str(uuid4()),
            "provider": provider,
            "started_at": time.time(),
            "retries": [],
        }
    )


# Whether the current async context has a configured fallback model waiting
# behind this call (set by with_fallback).  When a fallback exists, exhausting
# retries hands over to it immediately instead of pausing the whole task.
_FALLBACK_AVAILABLE: ContextVar[bool] = ContextVar("llm_fallback_available", default=False)

# Shared circuit breaker keyed by "provider:model".  Transient failures open
# it; while open, concurrent calls wait out the cooldown instead of hammering
# a provider that is already melting down.
_ENDPOINT_BREAKER = CircuitBreaker(threshold=3, cooldown_seconds=30.0)

# Endpoints observed to not support streaming.  Detected once, loudly, then
# remembered so subsequent calls go straight to non-streaming invocation.
_NON_STREAMING_ENDPOINTS: set[str] = set()

# Total retry limit when failures switch categories within one recovery cycle.
_MAX_TOTAL_RETRY_ATTEMPTS = 8

#: TTFT của lượt gọi thành công gần nhất (ms).  Đặt trong ``_complete_attempt`` khi
#: chunk đầu tiên tới, đọc ở ``complete()`` để ghi vào sự kiện hiệu năng.
_PERF_TTFT_MS: ContextVar[float | None] = ContextVar("perf_ttft_ms", default=None)


def _perf_stage_for_role(role: str | None) -> str:
    """Vai trò agent → công đoạn trong bảng Performance.

    Pro chạy node ``validator`` (đồng thời là Checker của kiến trúc), Flash chạy một
    vòng lặp duy nhất nên được xếp vào ``Operator LLM`` — nhưng sự kiện vẫn mang
    ``agent_role`` gốc, nên log phân biệt được Flash với Operator của Pro.
    """
    mapping = {
        "planner": PerfStage.PLANNER_LLM,
        "operator": PerfStage.OPERATOR_LLM,
        "validator": PerfStage.CHECKER_LLM,
        "checker": PerfStage.CHECKER_LLM,
        "summarizer": PerfStage.SUMMARIZER_LLM,
        "flash": PerfStage.OPERATOR_LLM,
        # FlashRunner là span gốc của vòng lặp Flash: model ở đây chính là agent quyết
        # định hành động, nên xếp vào Operator LLM (sự kiện vẫn giữ agent_role gốc).
        "flashrunner": PerfStage.OPERATOR_LLM,
    }
    return str(mapping.get((role or "").strip().lower(), PerfStage.LLM_OTHER))


def _extract_token_usage(response: Any) -> tuple[int | None, int | None, int | None]:
    """Lấy ``(input, output, total)`` token từ response của provider; thiếu thì ``None``.

    Không suy đoán: provider không trả usage thì để ``None`` (hiển thị N/A) thay vì ước
    lượng — một con số ước lượng sẽ bị đọc như số đo thật.
    """
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        response_metadata = getattr(response, "response_metadata", None)
        if isinstance(response_metadata, dict):
            usage = response_metadata.get("token_usage") or response_metadata.get("usage_metadata")
    if not isinstance(usage, dict):
        return None, None, None

    def pick(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int):
                return value
        return None

    prompt = pick("input_tokens", "prompt_tokens")
    completion = pick("output_tokens", "completion_tokens")
    total = pick("total_tokens")
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    return prompt, completion, total


def _context_breakdown(messages: Any) -> dict[str, Any]:
    """Đếm ký tự theo *nguồn* và số ảnh của một lượt gọi — trả lời "token đi đâu".

    Đo trên trace thật (session ``76fddabf``): **74,8% ký tự text nằm ở system prompt**
    (chỉ dẫn tĩnh + kế hoạch), 24,8% ở quan sát hiện tại, còn lịch sử hội thoại chỉ
    **0,4%** — tức "history phình to" không phải nguyên nhân của prompt 17k token.  Không
    có phép đo này thì rất dễ tối ưu sai chỗ.

    Ký tự ở đây là số đo thật; token là ước lượng (chars/4) và được ghi rõ là ước lượng,
    tách khỏi token do provider báo cáo.
    """
    buckets: dict[str, int] = {}
    images = 0
    total_chars = 0
    try:
        for message in messages or []:
            role = str(getattr(message, "type", None) or getattr(message, "role", "") or "?")
            content = getattr(message, "content", message)
            chars = 0
            if isinstance(content, str):
                chars = len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, str):
                        chars += len(block)
                    elif isinstance(block, dict):
                        if block.get("type") in ("image_url", "image") or "image_url" in block:
                            images += 1
                        elif isinstance(block.get("text"), str):
                            chars += len(block["text"])
            buckets[role] = buckets.get(role, 0) + chars
            total_chars += chars
    except Exception:  # noqa: BLE001 - đo không được thì thôi
        return {}
    return {
        "chars_by_role": buckets,
        "chars_total": total_chars,
        "images": images,
        "estimate": "token ≈ chars/4 (ước lượng, không phải số provider báo)",
    }


def _get_current_data_engine():
    if _CURRENT_DATA_ENGINE is not None:
        return _CURRENT_DATA_ENGINE
    engine_module = sys.modules.get("artemis.data_engine.engine")
    return getattr(engine_module, "_CURRENT_DATA_ENGINE", None) if engine_module else None


def _record_llm_retry(
    error: str,
    delay: float,
    *,
    attempt: int | None = None,
    max_retries: int | None = None,
    provider: str | None = None,
    source: str = "artemis",
) -> None:
    """Persist and publish one recoverable LLM retry for UI transparency."""
    request = _ACTIVE_LLM_REQUEST.get()
    timestamp = time.time()
    request_id = request.get("request_id") if request else None
    provider = provider or (request.get("provider") if request else None)

    # Performance log trước tiên: nó không phụ thuộc DataEngine (một tiến trình có thể
    # chạy lớp đo mà không có engine), và đây là số đo trả lời câu hỏi "20 giây đó có
    # phải do backoff không".
    record_event(
        _perf_stage_for_role(perf_context.current_agent_role()),
        "llm_retry",
        duration_ms=round(float(delay) * 1000.0, 3),
        status=STATUS_RETRY,
        provider=provider,
        retry_count=attempt,
        # Thời gian backoff **đo được** của lần thử lại này (không suy đoán từ wall time).
        retry_wait_ms=round(float(delay) * 1000.0, 3),
        error_type=str(error)[:120],
        request_id=request_id,
        detail={"source": source, "max_retries": max_retries},
    )

    engine = _get_current_data_engine()
    if not engine or not getattr(engine, "current_session_id", None):
        return

    error_text = str(error)
    if len(error_text) > 1000:
        error_text = error_text[:1000] + "... [Truncated by LLM Retry Telemetry]"

    payload = {
        "error": error_text,
        "delay": float(delay),
        "attempt": attempt,
        "max_retries": max_retries,
        "provider": provider,
        "source": source,
        "recoverable": True,
        "request_id": request_id,
        "scheduled_at": timestamp,
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    step_id = getattr(engine, "current_step_id", None)
    parent_trace_id = CURRENT_TRACE_ID.get()
    try:
        trace_id = engine.record_trace(
            type="llm_call",
            name="llm_retry",
            payload=payload,
            step_id=step_id,
            parent_trace_id=parent_trace_id,
            status="retrying",
        )
    except Exception as trace_error:
        llm_logger.warning("Failed to persist LLM retry trace: %s", trace_error)
        trace_id = None

    if request is not None:
        request["retries"].append(
            {
                **payload,
                "trace_id": str(trace_id) if trace_id else None,
                "timestamp": timestamp,
            }
        )

    try:
        engine._publish(
            "llm_retrying",
            {
                **payload,
                "step_id": str(step_id) if step_id else None,
                "trace_id": str(trace_id) if trace_id else None,
                "timestamp": timestamp,
            },
        )
    except Exception as publish_error:
        # Retry visibility is best-effort and must never break the LLM retry itself.
        llm_logger.warning("Failed to publish LLM retry event: %s", publish_error)


def _record_llm_event(name: str, payload: dict[str, Any], *, status: str = "retrying") -> None:
    """Persist and publish one degradation/lifecycle event on the LLM IO path.

    Every degradation (fallback, stream downgrade, stream reset, giving up)
    must be observable: WARNING-level logging is handled by callers, this
    records the structured trace + live event, best-effort.
    """
    request = _ACTIVE_LLM_REQUEST.get()
    engine = _get_current_data_engine()
    if not engine or not getattr(engine, "current_session_id", None):
        return

    full_payload = dict(payload)
    if request is not None:
        full_payload.setdefault("request_id", request.get("request_id"))
        if request.get("provider"):
            full_payload.setdefault("provider", request.get("provider"))
    full_payload = {key: value for key, value in full_payload.items() if value is not None}

    step_id = getattr(engine, "current_step_id", None)
    trace_id = None
    try:
        trace_id = engine.record_trace(
            type="llm_call",
            name=name,
            payload=full_payload,
            step_id=step_id,
            parent_trace_id=CURRENT_TRACE_ID.get(),
            status=status,
        )
    except Exception as trace_error:
        llm_logger.warning("Failed to persist LLM event trace %s: %s", name, trace_error)
    try:
        engine._publish(
            name,
            {
                **full_payload,
                "step_id": str(step_id) if step_id else None,
                "trace_id": str(trace_id) if trace_id else None,
                "timestamp": time.time(),
            },
        )
    except Exception as publish_error:
        llm_logger.warning("Failed to publish LLM event %s: %s", name, publish_error)


class _ProviderRetryTelemetryHandler(logging.Handler):
    """Turn provider SDK retry log records into structured Artemis telemetry."""

    _DELAY_PATTERN = re.compile(r"\bin\s+(?P<delay>\d+(?:\.\d+)?)\s+seconds?\b", re.IGNORECASE)
    _ERROR_PATTERN = re.compile(r"\bas it raised\s+(?P<error>.+)$", re.IGNORECASE)
    _RETRYABLE_MARKERS = (
        "503",
        "429",
        "unavailable",
        "resource_exhausted",
        "high demand",
        "quota",
        "throttled",
        "overloaded",
    )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            request = _ACTIVE_LLM_REQUEST.get()
            if not request or request.get("provider") != ModelProvider.GOOGLE.value:
                return

            message = record.getMessage()
            lowered = message.lower()
            if "retrying" not in lowered or not any(
                marker in lowered for marker in self._RETRYABLE_MARKERS
            ):
                return

            delay_match = self._DELAY_PATTERN.search(message)
            error_match = self._ERROR_PATTERN.search(message)
            delay = float(delay_match.group("delay")) if delay_match else 0.0
            error = error_match.group("error").strip() if error_match else message
            # Số lần thử lại **đếm tại đúng lớp provider retry** (giữ trên ngữ cảnh của lượt gọi),
            # không suy từ wall time: mỗi bản ghi retry của SDK là một lần thử lại thật.
            attempt = int(request.get("retry_count") or 0) + 1
            try:
                request["retry_count"] = attempt
            except (TypeError, ValueError):
                attempt = None
            _record_llm_retry(
                error,
                delay,
                attempt=attempt,
                provider=request["provider"],
                source="provider_sdk",
            )
        except Exception:
            # Telemetry must never interfere with the provider's own retry.
            self.handleError(record)


def _install_provider_retry_telemetry() -> None:
    provider_logger = logging.getLogger("google_genai._api_client")
    if any(
        isinstance(handler, _ProviderRetryTelemetryHandler) for handler in provider_logger.handlers
    ):
        return
    provider_logger.addHandler(_ProviderRetryTelemetryHandler(level=logging.INFO))


_install_provider_retry_telemetry()


def _handle_llm_pause_and_resume(last_error: Exception) -> Path:
    """Handles pausing task execution upon persistent failure until resumed."""
    err_msg = str(last_error)
    if len(err_msg) > 1000:
        err_msg = err_msg[:1000] + "... [Truncated by LLM Wrapper]"

    llm_logger.warning(f"LLM Error: {err_msg}. Pausing execution to wait for resume signal...")
    pause_file = PAUSE_FILE
    try:
        pause_file.write_text(f"LLM Error: {err_msg}", encoding="utf-8")
    except (OSError, UnicodeError):
        pass

    current_engine = _get_current_data_engine()
    if current_engine:
        step_id = getattr(current_engine, "current_step_id", None)
        timestamp = time.time()
        request = _ACTIVE_LLM_REQUEST.get()
        failure_payload: dict[str, Any] = {"error": err_msg, "pause": True}
        if request:
            failure_payload.update(
                {
                    "request_id": request["request_id"],
                    "provider": request.get("provider"),
                    "waited_seconds": max(0.0, timestamp - request["started_at"]),
                    # Only SDK-observed retries are included. Other providers
                    # may retry internally, but their attempt data is opaque.
                    "retries": list(request["retries"]),
                }
            )
            failure_payload = {
                key: value for key, value in failure_payload.items() if value is not None
            }
        try:
            # Persist exhausted LLM retries as a failed call so the same error
            # card is available both in the live stream and after a refresh.
            current_engine.record_trace(
                type="llm_call",
                name="llm_pause",
                payload=failure_payload,
                step_id=step_id,
                status="failed",
            )
        except Exception as trace_error:
            llm_logger.warning("Failed to persist paused LLM error trace: %s", trace_error)

        current_engine._publish(
            "task_paused",
            {
                **failure_payload,
                "step_id": str(step_id) if step_id else None,
                "timestamp": timestamp,
            },
        )

    return pause_file


async def _wait_for_resume(pause_file: Path) -> bool:
    """Wait for the pause file to be cleared. Returns False on deadline."""
    deadline = float(getattr(settings, "LLM_PAUSE_TIMEOUT_SECONDS", 0.0) or 0.0)
    waited = 0.0
    while pause_file.exists():
        if deadline > 0 and waited >= deadline:
            return False
        await asyncio.sleep(1)
        waited += 1
    return True


async def _wait_for_breaker(key: str) -> None:
    """Wait out an open circuit instead of hammering a melting provider."""
    waited = 0.0
    warned = False
    while not _ENDPOINT_BREAKER.allow(key):
        if not warned:
            warned = True
            llm_logger.warning(f"LLM circuit open for {key}; waiting for cooldown...")
        step = min(max(_ENDPOINT_BREAKER.open_remaining(key), 0.5), 2.0)
        await asyncio.sleep(step)
        waited += step
        if waited >= 120.0:
            # Never deadlock behind a stuck half-open trial; proceed anyway.
            break


async def _run_with_recovery[T](
    call_once: Callable[[], Awaitable[T]],
    *,
    provider: str | None = None,
    endpoint_key: str | None = None,
) -> T:
    """Execute one LLM call under the classified retry / pause / breaker policy.

    Failure handling is decided per category (see artemis.llm.reliability):

    - Retryable categories back off per policy; each retry is recorded for UI
      transparency via _record_llm_retry.
    - Non-retryable categories (auth, bad request) raise LLMPermanentError
      immediately: retrying cannot help and pausing would hang the task.
    - When retryable attempts are exhausted: if a fallback model is waiting
      (with_fallback), raise LLMExhaustedError so it takes over immediately;
      otherwise pause the task (bounded by settings.LLM_PAUSE_TIMEOUT_SECONDS)
      and retry from scratch on resume.
    """
    while True:
        request_token = _begin_llm_request(provider)
        last_error: Exception | None = None
        last_failure = None
        attempts: dict[FailureCategory, int] = {}
        pause_file: Path | None = None
        try:
            while True:
                if endpoint_key:
                    await _wait_for_breaker(endpoint_key)
                try:
                    result = await call_once()
                except (KeyboardInterrupt, asyncio.CancelledError):
                    raise
                except LLMCallError:
                    # Already classified terminal by a nested recovery layer.
                    raise
                except Exception as e:
                    failure = classify_failure(e)
                    if failure.category is FailureCategory.CANCELLED:
                        raise
                    if endpoint_key:
                        # Lỗi quota có thời gian hồi do nhà cung cấp nói ⇒ mở mạch ngay bằng
                        # đúng thời gian đó, để lượt sau **xoay sang model khác** thay vì chờ
                        # vô ích (quota ngày của Google: 500 request/ngày/mỗi model).
                        _ENDPOINT_BREAKER.record_failure(
                            endpoint_key, failure, open_for=quota_cooldown(e)
                        )
                    if not failure.retryable:
                        llm_logger.warning(
                            f"LLM call failed permanently ({failure.category.value}): {e}"
                        )
                        _record_llm_event(
                            "llm_gave_up",
                            {
                                "error": str(e)[:1000],
                                "category": failure.category.value,
                                "retryable": False,
                            },
                            status="failed",
                        )
                        raise LLMPermanentError(str(e), failure=failure, cause=e) from e
                    last_error, last_failure = e, failure
                    attempt = attempts.get(failure.category, 0) + 1
                    attempts[failure.category] = attempt
                    policy = retry_policy_for(failure.category)
                    if attempt >= policy.max_attempts:
                        break
                    total_attempts = sum(attempts.values())
                    if total_attempts >= _MAX_TOTAL_RETRY_ATTEMPTS:
                        llm_logger.warning(
                            "LLM retry total across failure categories reached"
                            f" {total_attempts}; treating as exhausted."
                        )
                        break
                    delay = policy.delay_for(attempt)
                    _record_llm_retry(
                        str(e),
                        delay,
                        attempt=attempt,
                        max_retries=policy.max_attempts,
                        provider=provider,
                    )
                    llm_logger.warning(
                        f"LLM call failed ({failure.category.value}), retrying in {delay:.2f}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    if endpoint_key:
                        _ENDPOINT_BREAKER.record_success(endpoint_key)
                    return result

            # Retryable attempts exhausted.
            if _FALLBACK_AVAILABLE.get():
                _record_llm_event(
                    "llm_gave_up",
                    {
                        "error": str(last_error)[:1000],
                        "category": last_failure.category.value,
                        "retryable": True,
                        "handover": "fallback",
                    },
                    status="failed",
                )
                raise LLMExhaustedError(
                    str(last_error), failure=last_failure, cause=last_error
                ) from last_error
            pause_file = _handle_llm_pause_and_resume(last_error)
        finally:
            _ACTIVE_LLM_REQUEST.reset(request_token)

        if not await _wait_for_resume(pause_file):
            llm_logger.error(
                "LLM pause deadline"
                f" ({settings.LLM_PAUSE_TIMEOUT_SECONDS:.0f}s) exceeded; giving up."
            )
            try:
                pause_file.unlink(missing_ok=True)
            except OSError:
                pass
            raise LLMExhaustedError(
                str(last_error), failure=last_failure, cause=last_error
            ) from last_error

        llm_logger.info("Resume signal received, retrying LLM call...")
        current_engine = _get_current_data_engine()
        if current_engine:
            current_engine._publish("task_resumed", {})


def _inject_parent_trace_id(trace_id, *args, **kwargs):
    config = kwargs.get("config") or {}
    if not kwargs.get("config") and len(args) > 1:
        args_list = list(args)
        if isinstance(args_list[1], dict):
            config = args_list[1]
            args_list[1] = config
            args = tuple(args_list)

    if "metadata" not in config:
        config["metadata"] = {}
    config["metadata"]["parent_trace_id"] = str(trace_id)
    kwargs["config"] = config
    return args, kwargs


class _TolerantStructuredRunnable:
    """Structured-output chain that salvages fenced / prose JSON answers.

    ``ChatOpenAI.with_structured_output`` expects a strict JSON (or tool-call)
    answer and raises when the provider replies with prose plus a fenced
    ```` ```json ```` block instead — common when the endpoint is an
    OpenAI-compatible proxy in front of a model that ignores ``response_format``.

    That raise happens *inside* the retry/pause policy, so the call is retried
    (burning tokens) and the task then pauses for ``LLM_PAUSE_TIMEOUT_SECONDS``
    (15 minutes by default). To the tester the run just looks hung — the app is
    never launched, no step is recorded, and the console only shows
    "Đang chạy".

    Running the chain with ``include_raw=True`` keeps the raw message next to
    the parse error, so the answer can be re-parsed through the shared tolerant
    pipeline (:func:`parse_structured`, which already understands fences and
    minor JSON noise) instead of failing the call.
    """

    def __init__(self, runnable: Any, schema: type[BaseModel]) -> None:
        self.runnable = runnable
        self.schema = schema

    def __getattr__(self, item: str) -> Any:
        # ``runnable``/``schema`` are real instance attributes; only unknown
        # names are delegated, and never re-entrantly.
        if item in {"runnable", "schema"}:
            raise AttributeError(item)
        return getattr(self.runnable, item)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        try:
            result = await self.runnable.ainvoke(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised when unrecoverable
            recovered = self._recover_from_error(exc)
            if recovered is None:
                raise
            return recovered
        return self._salvage(result)

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        try:
            result = self.runnable.invoke(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised when unrecoverable
            recovered = self._recover_from_error(exc)
            if recovered is None:
                raise
            return recovered
        return self._salvage(result)

    def _recover_from_error(self, exc: BaseException) -> Any:
        """Salvage the raw answer carried by a strict parser's ValidationError.

        ``method="json_schema"`` — the langchain-openai default since 0.3.0 — has
        no ``include_raw`` fallback and raises anyway, so ``_salvage`` never sees
        the text. The error still carries the model's raw answer in
        ``errors()[*]["input"]``, which is all that is needed to re-parse it
        tolerantly, and that costs no second LLM call.
        """
        for source in (exc, exc.__cause__, exc.__context__):
            for candidate in self._error_inputs(source):
                recovered = parse_structured(candidate, self.schema)
                if not isinstance(recovered, ParseFailure):
                    llm_logger.info(
                        "Recovered %s from a strict-parser error (%d chars).",
                        self.schema.__name__,
                        len(candidate),
                    )
                    return recovered
        return None

    @staticmethod
    def _error_inputs(exc: Any) -> list[str]:
        """Raw string inputs carried by a Pydantic ``ValidationError``."""
        errors = getattr(exc, "errors", None)
        if not callable(errors):
            return []
        try:
            items = errors()
        except Exception:  # noqa: BLE001 - malformed error object
            return []
        inputs: list[str] = []
        for item in items or []:
            value = item.get("input") if isinstance(item, dict) else None
            if isinstance(value, str) and value.strip():
                inputs.append(value)
        return inputs

    def _salvage(self, result: Any) -> Any:
        """Return the parsed object, recovering a non-strict answer if needed."""
        if not isinstance(result, dict):
            # Fake/plain models return the message directly; nothing to salvage.
            return result
        parsed = result.get("parsed")
        if parsed is not None:
            return parsed
        text = content_to_text(getattr(result.get("raw"), "content", result.get("raw")))
        recovered = parse_structured(text, self.schema)
        if not isinstance(recovered, ParseFailure):
            llm_logger.info(
                "Recovered %s from a non-strict structured answer (%d chars).",
                self.schema.__name__,
                len(text),
            )
            return recovered
        llm_logger.warning(
            "Structured answer for %s is unparsable: %s",
            self.schema.__name__,
            recovered.error,
        )
        error = result.get("parsing_error")
        if error is not None:
            raise error
        return None


class RobustChatModelWrapper:
    """Universal LangChain Runnable wrapper ensuring robust execution and telemetry."""

    def __init__(
        self,
        base_model: BaseChatModel,
        ctx: ArtemisContext = None,
        endpoint: ModelEndpoint | None = None,
    ):
        self.base_model = base_model
        self.ctx = ctx
        self.endpoint = endpoint

    def __getattr__(self, item: str):
        return getattr(self.base_model, item)

    def bind_tools(self, *args, **kwargs):
        # Extract tools list from positional args or keyword args
        tools_list = None
        other_args = list(args)
        if other_args:
            tools_list = other_args.pop(0)
        elif "tools" in kwargs:
            tools_list = kwargs.pop("tools")

        processed_tools = list(tools_list) if tools_list is not None else []

        # Determine if the underlying model provider is Google / Gemini
        if self.endpoint is not None:
            google_backed = is_google_provider(self.endpoint.provider)
        else:
            google_backed = is_google_chat_model(self.base_model)

        has_explicit_google_search = any(
            isinstance(t, dict) and "google_search" in t for t in processed_tools
        )
        should_ground = (
            self.endpoint is not None and self.endpoint.enable_grounding
        ) or has_explicit_google_search

        if google_backed:
            if should_ground:
                # Add native Google Search Grounding tool if not already present
                if not has_explicit_google_search:
                    processed_tools.append({"google_search": {}})

                # Ensure include_server_side_tool_invocations is True in tool_config
                # to satisfy Gemini API requirements when mixing built-in tools and function calling
                tool_config = kwargs.get("tool_config")
                if tool_config is None:
                    kwargs["tool_config"] = {"include_server_side_tool_invocations": True}
                elif isinstance(tool_config, dict):
                    tool_config = dict(tool_config)
                    tool_config.setdefault("include_server_side_tool_invocations", True)
                    kwargs["tool_config"] = tool_config
        else:
            # If not Gemini/Google (e.g. OpenAI, Anthropic, Ollama, OpenRouter, XAI):
            # Ignore grounding and filter out any Gemini-specific built-in dict tools to avoid provider errors
            processed_tools = [
                t for t in processed_tools if not (isinstance(t, dict) and "google_search" in t)
            ]

        bound = self.base_model.bind_tools(processed_tools, *other_args, **kwargs)
        return RobustChatModelWrapper(bound, self.ctx, endpoint=self.endpoint)

    def with_config(self, *args, **kwargs):
        return RobustChatModelWrapper(
            self.base_model.with_config(*args, **kwargs),
            self.ctx,
            endpoint=self.endpoint,
        )

    def with_structured_output(self, *args, **kwargs):
        if not hasattr(self.base_model, "with_structured_output"):
            raise AttributeError(
                f"{type(self.base_model)} does not support with_structured_output"
            )
        # Keep the raw message beside the parse result so a non-strict answer
        # (prose + fenced JSON, typical behind an OpenAI-compatible proxy) can be
        # salvaged instead of raising inside the retry/pause policy.
        caller_wants_raw = bool(kwargs.get("include_raw"))
        kwargs.setdefault("include_raw", True)
        structured = self.base_model.with_structured_output(*args, **kwargs)
        schema = args[0] if args else kwargs.get("schema")
        if not caller_wants_raw and isinstance(schema, type) and issubclass(schema, BaseModel):
            structured = _TolerantStructuredRunnable(structured, schema)
        return RobustChatModelWrapper(structured, self.ctx, endpoint=self.endpoint)

    def __or__(self, other):
        return RobustChatModelWrapper(
            self.base_model.__or__(other), self.ctx, endpoint=self.endpoint
        )

    def __ror__(self, other):
        return RobustChatModelWrapper(
            self.base_model.__ror__(other), self.ctx, endpoint=self.endpoint
        )

    def _provider_value(self) -> str | None:
        provider = getattr(self.endpoint, "provider", None)
        if provider is None:
            return None
        return str(getattr(provider, "value", provider))

    def _endpoint_key(self) -> str:
        if self.endpoint is not None:
            return f"{self._provider_value()}:{self.endpoint.model_name}"
        return type(self.base_model).__name__

    def _traced_call(self, args: tuple, kwargs: dict) -> tuple[tuple, dict, Any]:
        trace_id = None
        if self.ctx and self.ctx.data_engine:
            trace_id = CURRENT_TRACE_ID.get()
        if trace_id:
            args, kwargs = _inject_parent_trace_id(trace_id, *args, **kwargs)
        return args, kwargs, trace_id

    def _meter_usage(self, response) -> None:
        """Record measured token usage for this call (M0 metering, best-effort)."""
        engine = self.ctx.data_engine if self.ctx else None
        if engine is None:
            engine = _get_current_data_engine()
        record_llm_usage(engine, response, source=self._endpoint_key())

    @asynccontextmanager
    async def _perf_llm_span(self, transport: str):
        """Span đo một lượt gọi LLM, bao gồm cả retry/backoff bên trong nó.

        Đặt *ngoài* ``_run_with_recovery`` là chủ ý: thời gian tester thấy phải là toàn bộ
        thời gian chờ model, kể cả các lần thử lại.  Số lần retry và thời gian backoff
        được ghi thành span con (``llm_retry``) nên không bị cộng trùng.
        """
        role = perf_context.current_agent_role()
        async with perf_span(
            _perf_stage_for_role(role),
            "llm_call",
            role=role,
            model=getattr(self.endpoint, "model_name", None),
            provider=self._provider_value(),
            endpoint=self._endpoint_key(),
            detail={"transport": transport},
        ) as span:
            yield span

    def _finish_perf_span(self, span, response, messages: Any = None) -> None:
        """Gắn token + TTFT + breakdown ngữ cảnh đã đo được vào span."""
        if span is None:
            return
        prompt, completion, total = _extract_token_usage(response)
        span.set(
            input_tokens=prompt,
            output_tokens=completion,
            total_tokens=total,
            ttft_ms=_PERF_TTFT_MS.get(),
        )
        breakdown = _context_breakdown(messages)
        if breakdown:
            # Token đi đâu: số ký tự theo nguồn (system/quan sát/lịch sử) + số ảnh.
            span.note(context_breakdown=breakdown)

    async def ainvoke(self, *args, **kwargs):
        args, kwargs, _ = self._traced_call(args, kwargs)
        async with self._perf_llm_span("invoke") as span:
            response = await _run_with_recovery(
                functools.partial(self.base_model.ainvoke, *args, **kwargs),
                provider=self._provider_value(),
                endpoint_key=self._endpoint_key(),
            )
            self._finish_perf_span(span, response, args[0] if args else None)
        self._meter_usage(response)
        return response

    async def complete(self, *args, **kwargs):
        """Single completion entry point: returns the full final message.

        Streaming is a transport detail handled internally — provider chunks
        are forwarded to the live UI as deltas and accumulated here, so
        downstream consumers only ever see one complete message (or a typed
        LLMCallError).  A mid-stream failure discards the partial output,
        signals the UI to drop it, and retries the whole call per policy;
        partial or duplicated chunks can never reach message history.
        """
        args, kwargs, _ = self._traced_call(args, kwargs)
        emit_deltas = bool(self.ctx and self.ctx.data_engine)
        async with self._perf_llm_span("stream") as span:
            response = await _run_with_recovery(
                functools.partial(self._complete_attempt, emit_deltas, *args, **kwargs),
                provider=self._provider_value(),
                endpoint_key=self._endpoint_key(),
            )
            self._finish_perf_span(span, response, args[0] if args else None)
        self._meter_usage(response)
        return response

    def _emit_stream_delta(self, stream_exec_id, chunk) -> None:
        content = getattr(chunk, "content", None)
        if not content:
            return
        text_to_stream = ""
        thinking_to_stream = ""
        if isinstance(content, str):
            text_to_stream = content
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text_to_stream += item.get("text", "")
                    elif item.get("type") == "thinking":
                        thinking_to_stream += item.get("thinking", "")
        if text_to_stream:
            self.ctx.data_engine.stream_output(stream_exec_id, text_to_stream, is_thinking=False)
        if thinking_to_stream:
            self.ctx.data_engine.stream_output(stream_exec_id, thinking_to_stream, is_thinking=True)

    async def _complete_attempt(self, emit_deltas: bool, *args, **kwargs):
        endpoint_key = self._endpoint_key()
        if endpoint_key in _NON_STREAMING_ENDPOINTS:
            return await self.base_model.ainvoke(*args, **kwargs)

        stream_exec_id = uuid4()
        full_response = None
        _attempt_start = time.perf_counter()
        try:
            async for chunk in self.base_model.astream(*args, **kwargs):
                if full_response is None:
                    # TTFT: thời gian tới token đầu tiên của lần thử này.
                    _PERF_TTFT_MS.set(round((time.perf_counter() - _attempt_start) * 1000.0, 3))
                    full_response = chunk
                else:
                    full_response = full_response + chunk
                if emit_deltas:
                    self._emit_stream_delta(stream_exec_id, chunk)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as stream_error:
            if full_response is not None:
                # Mid-stream failure: the partial output must never reach
                # message history. Tell the UI to discard it, then let the
                # recovery policy retry the whole call with a fresh stream id.
                llm_logger.warning(
                    f"LLM stream broke mid-response ({stream_error}); discarding partial output."
                )
                _record_llm_event(
                    "llm_stream_reset",
                    {
                        "stream_exec_id": str(stream_exec_id),
                        "action": "discard",
                        "reason": "mid_stream_failure",
                        "error": str(stream_error)[:500],
                        "category": classify_failure(stream_error).category.value,
                        "message": (
                            "A request error occurred during output generation, typically caused by lower API priority. Retrying automatically..."
                        ),
                    },
                )
                raise
            if _is_stream_unsupported_error(stream_error):
                _NON_STREAMING_ENDPOINTS.add(endpoint_key)
                llm_logger.warning(
                    f"Endpoint {endpoint_key} does not support streaming"
                    f" ({stream_error}); switching to non-streaming calls."
                )
                _record_llm_event(
                    "llm_stream_downgrade",
                    {"endpoint": endpoint_key, "error": str(stream_error)[:500]},
                )
                return await self.base_model.ainvoke(*args, **kwargs)
            raise
        if full_response is None:
            llm_logger.warning("LLM stream yielded no chunks; using non-streaming call.")
            return await self.base_model.ainvoke(*args, **kwargs)
        return full_response

    async def astream(self, *args, **kwargs):
        """Deprecated compatibility shim: yields exactly one final message.

        The old chunk-level astream retry could re-deliver already-yielded
        chunks after a mid-stream failure, corrupting accumulated message
        history. Streaming now happens inside complete(); live token deltas
        still reach the UI through the data engine.
        """
        yield await self.complete(*args, **kwargs)


def _is_stream_unsupported_error(error: BaseException) -> bool:
    """Detect 'this endpoint/model cannot stream' as opposed to a transient failure.

    A positive here permanently disables streaming for the endpoint in this
    process, so a bare AttributeError/TypeError from unrelated code must not
    qualify — the message has to actually implicate streaming.
    """
    if isinstance(error, NotImplementedError):
        return True
    message = str(error).lower()
    if isinstance(error, (AttributeError, TypeError)):
        return "stream" in message
    return "stream" in message and any(
        marker in message
        for marker in ("not support", "unsupported", "not implemented", "not available")
    )


async def acomplete(llm, *args, **kwargs):
    """Get one complete response from any chat model object.

    This is the single call-shape agents should use. For gateway-managed
    models (RobustChatModelWrapper) it delegates to complete(), which owns
    streaming, classified retries, and telemetry. For raw models and test
    doubles it preserves the legacy accumulate-stream-else-ainvoke shape.
    """
    if isinstance(llm, RobustChatModelWrapper):
        return await llm.complete(*args, **kwargs)

    try:
        full_response = None
        async for chunk in llm.astream(*args, **kwargs):
            if full_response is None:
                full_response = chunk
            else:
                full_response = full_response + chunk
        if full_response is not None:
            return full_response
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as stream_error:
        llm_logger.warning(
            f"astream unavailable on {type(llm).__name__} ({stream_error}); using ainvoke."
        )
    return await llm.ainvoke(*args, **kwargs)


async def acomplete_structured(
    llm,
    messages: list,
    *,
    schema=None,
    correction_attempts: int = 1,
):
    """Complete and parse a JSON response, with one corrective re-ask.

    On a parse failure the model is shown its own output's parse error and
    asked to re-emit valid JSON (up to ``correction_attempts`` times) — giving
    the model a chance to see its mistake instead of feeding garbage
    downstream. If it still fails, raises StructuredOutputError (never
    returns raw text masquerading as parsed data). Each repair round emits an
    llm_parse_repair telemetry event.
    """
    response = await acomplete(llm, messages)
    text = content_to_text(getattr(response, "content", ""))
    parsed = parse_structured(text, schema=schema)
    attempt = 0
    while isinstance(parsed, ParseFailure) and attempt < correction_attempts:
        attempt += 1
        llm_logger.warning(
            f"Structured output parse failed ({parsed.error});"
            f" asking the model to correct itself (attempt {attempt})..."
        )
        _record_llm_event(
            "llm_parse_repair",
            {"error": parsed.error[:500], "attempt": attempt},
        )
        correction_messages = [
            *messages,
            response if isinstance(response, BaseMessage) else AIMessage(content=text),
            HumanMessage(
                content=(
                    "Your previous reply could not be parsed as the required"
                    f" JSON ({parsed.error}). Re-emit ONLY the corrected JSON"
                    " payload, with no surrounding prose or code fences."
                )
            ),
        ]
        response = await acomplete(llm, correction_messages)
        text = content_to_text(getattr(response, "content", ""))
        parsed = parse_structured(text, schema=schema)
    if isinstance(parsed, ParseFailure):
        _record_llm_event(
            "llm_gave_up",
            {
                "error": f"structured output unparseable: {parsed.error[:400]}",
                "category": "bad_request",
                "retryable": False,
            },
            status="failed",
        )
        raise StructuredOutputError(parsed)
    return parsed


# Wraps every LLM call: tells the user when a call is slow and enforces a hard
# timeout. The timeout does not count while the task is paused (PAUSE_FILE).
invoke_llm_with_timeout_message = LLMWaitNotice(
    notify=user_messages_logger.info,
    on_timeout=user_messages_logger.error,
    hold=lambda: PAUSE_FILE.exists(),
    default_hard_timeout=180,
).__call__


# Backward compatible factory functions delegating to ModelFactory
def get_google_llm(
    model_name: str = "gemini-3.8-flash",
    temperature: float | None = None,
    timeout: float | None = None,
    thinking_budget: int | None = None,
    thinking_level: str | None = "medium",
    include_thoughts: bool | None = None,
    enable_grounding: bool = False,
) -> BaseChatModel:
    ep = ModelEndpoint(
        provider=ModelProvider.GOOGLE,
        model_name=model_name,
        temperature=temperature or 0.0,
        timeout_seconds=timeout or 60.0,
        thinking_budget=thinking_budget,
        thinking_level=thinking_level,
        include_thoughts=include_thoughts,
        enable_grounding=enable_grounding,
    )
    return ModelFactory.create_model(ep)

#: Tiền tố biến môi trường khai chuỗi model cho một vai trò: ``ARTEMIS_LLM_CHAIN_OPERATOR``.
_CHAIN_ENV_PREFIX = "ARTEMIS_LLM_CHAIN_"


def _endpoint_key_of(endpoint: ModelEndpoint) -> str:
    """Khoá mạch điện của một endpoint — **phải trùng** khoá lúc gọi.

    ``RobustChatModelWrapper._endpoint_key`` dùng ``provider:model``; lệch nhau thì mạch mở cho
    model này mà lượt gọi lại ghi vào model khác, và việc xoay model không bao giờ xảy ra.
    """
    provider = getattr(endpoint.provider, "value", endpoint.provider)
    return f"{provider}:{endpoint.model_name}"


def _endpoint_from_config(cfg: Any, *, like: ModelEndpoint | None = None) -> ModelEndpoint:
    """Đọc một mục config thành endpoint; ``like`` giữ nguyên các tham số không khai."""

    def _get_val(obj, attr, expected_type):
        val = getattr(obj, attr, None)
        return val if isinstance(val, expected_type) else None

    provider_val = getattr(cfg, "provider", None) or (like.provider if like else "google")
    model_val = getattr(cfg, "model", None) or (like.model_name if like else "gemini-2.5-flash")
    return ModelEndpoint(
        provider=ModelProvider.from_string(provider_val),
        model_name=str(model_val),
        temperature=_get_val(cfg, "temperature", (int, float))
        or (like.temperature if like else 0.0),
        timeout_seconds=_get_val(cfg, "timeout", (int, float))
        or (like.timeout_seconds if like else 60.0),
        thinking_budget=_get_val(cfg, "thinking_budget", int)
        if like is None
        else (_get_val(cfg, "thinking_budget", int) or like.thinking_budget),
        thinking_level=_get_val(cfg, "thinking_level", str)
        if like is None
        else (_get_val(cfg, "thinking_level", str) or like.thinking_level),
        reasoning_effort=_get_val(cfg, "reasoning_effort", str)
        if like is None
        else (_get_val(cfg, "reasoning_effort", str) or like.reasoning_effort),
        include_thoughts=_get_val(cfg, "include_thoughts", bool)
        if like is None
        else (_get_val(cfg, "include_thoughts", bool) or like.include_thoughts),
        enable_grounding=bool(_get_val(cfg, "enable_grounding", bool))
        or (like.enable_grounding if like else False),
    )


def _chain_specs(role: str) -> list[str]:
    """Các mục trong ``ARTEMIS_LLM_CHAIN_<VAI TRÒ>``, giữ nguyên thứ tự khai.

    Ví dụ: ``ARTEMIS_LLM_CHAIN_OPERATOR=gemini-2.5-flash, google:gemini-3.1-flash-lite``.
    Mục không ghi ``provider:`` thì dùng provider của vai trò, nên "ưu tiên Google" chỉ cần
    ``_PROVIDER=google`` như đang có, không phải lặp lại ở từng mục.
    """
    raw = os.environ.get(f"{_CHAIN_ENV_PREFIX}{role.upper().replace('-', '_')}", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _endpoint_candidates(ctx: ArtemisContext, name: str, *, is_utils: bool) -> list[ModelEndpoint]:
    """Danh sách ứng viên cho một vai trò, đã bỏ trùng và giữ thứ tự ưu tiên."""
    cfg = ctx.llm_config.get_utils(name) if is_utils else ctx.llm_config.get_agent(name)
    primary = _endpoint_from_config(cfg)
    out = [primary]

    for spec in _chain_specs(name):
        provider, _, model = spec.partition(":")
        if model:
            endpoint = primary.model_copy(
                update={"provider": ModelProvider.from_string(provider), "model_name": model}
            )
        else:
            endpoint = primary.model_copy(update={"model_name": spec})
        out.append(endpoint)

    fallback = getattr(cfg, "fallback", None)
    if fallback is not None:
        out.append(_endpoint_from_config(fallback, like=primary))

    seen: set[tuple[str, str]] = set()
    unique: list[ModelEndpoint] = []
    for endpoint in out:
        key = (str(getattr(endpoint.provider, "value", endpoint.provider)), endpoint.model_name)
        if key in seen:
            continue
        seen.add(key)
        unique.append(endpoint)
    return unique


def _pick_healthy(candidates: list[ModelEndpoint], *, start: int, role: str) -> ModelEndpoint:
    """Ứng viên đầu tiên chưa nghỉ, tính từ ``start``; hết thì lấy cái sớm hồi nhất.

    Bỏ qua model đang nghỉ là toàn bộ việc "xoay model": sau một lần 429 quota ngày, mạch của
    model đó mở tới lúc nhà cung cấp nói nó hồi, nên lượt sau tự dùng model kế tiếp trong chuỗi.
    """
    ordered = candidates[start:] + candidates[:start]
    cooling: list[tuple[float, ModelEndpoint]] = []
    for endpoint in ordered:
        remaining = _ENDPOINT_BREAKER.open_remaining(_endpoint_key_of(endpoint))
        if remaining <= 0:
            if cooling:
                llm_logger.warning(
                    f"LLM model rotation for '{role}': bỏ qua "
                    + ", ".join(
                        f"{_endpoint_key_of(item)} (còn {seconds:.0f}s)" for seconds, item in cooling
                    )
                    + f" — dùng {_endpoint_key_of(endpoint)}."
                )
            return endpoint
        cooling.append((remaining, endpoint))

    soonest = min(cooling, key=lambda pair: pair[0])
    llm_logger.warning(
        f"LLM model rotation for '{role}': MỌI ứng viên đang nghỉ "
        + ", ".join(f"{_endpoint_key_of(item)} (còn {seconds:.0f}s)" for seconds, item in cooling)
        + f" — dùng tạm {_endpoint_key_of(soonest[1])}."
    )
    return soonest[1]




def _resolve_endpoint(
    ctx: ArtemisContext,
    name: str,
    is_utils: bool = False,
    use_fallback: bool = False,
) -> ModelEndpoint:
    """Endpoint cho một vai trò — **xoay model** khi model trước đã cạn quota.

    Vì sao không trả thẳng model trong config: quota của nhà cung cấp là *theo model*.  Đo
    trên máy thật 28/10/2026: bốn vai trò (planner/operator/explorer/checker) cùng trỏ
    ``google:gemini-3.5-flash-lite``, quota Free Tier là **500 request/ngày/mỗi model**, cạn
    trong một buổi chạy — và vì chỉ có *một* model nên mọi lượt sau đều 429 cho tới khi hết
    ngày (``retryDelay`` 14269s).  Chuỗi ứng viên ở đây là thứ biến "hết quota" thành "đổi
    model": model nào đang trong thời gian nghỉ (mạch mở) thì bị bỏ qua, lượt sau dùng model kế.

    Chuỗi được ghép theo thứ tự **ưu tiên**:

    1. model của vai trò trong ``config/artemis.jsonc`` (kèm provider của nó);
    2. chuỗi trong ``.env``: ``ARTEMIS_LLM_CHAIN_<VAI TRÒ>`` — mỗi mục là ``model`` hoặc
       ``provider:model``, phân cách bằng dấu phẩy.  Mục không ghi provider thì **giữ provider
       của vai trò**, nên khai ``_PROVIDER=google`` là cả chuỗi chạy trên Google;
    3. ``fallback`` trong config (tương thích với cách khai cũ).

    Hết ứng viên khoẻ mạnh thì **không** chặn tác vụ: chọn ứng viên sớm hồi nhất và ghi cảnh
    báo kèm thời gian hồi — chặn cứng sẽ biến một rate limit 60 giây thành một tác vụ chết.
    """
    if getattr(ctx, "llm_config", None) is None:
        try:
            ctx.llm_config = get_default_llm_config()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Config loading has open-ended failure modes; surface the real cause
            # instead of letting the attribute access below fail with a misleading
            # AttributeError on the still-missing config.
            raise RuntimeError(
                f"Default LLM config could not be loaded while resolving '{name}': {exc}"
            ) from exc

    candidates = _endpoint_candidates(ctx, name, is_utils=is_utils)
    start = 1 if use_fallback else 0
    if start >= len(candidates):
        raise ValueError(f"LLM configuration for '{name}' has no fallback!")
    return _pick_healthy(candidates, start=start, role=name)


def _get_llm(
    ctx: ArtemisContext,
    name: str,
    is_utils: bool = False,
    use_fallback: bool = False,
    temperature: float | None = None,
) -> BaseChatModel:
    """Resolves and instantiates the appropriate LLM wrapper for the given agent role."""
    endpoint = _resolve_endpoint(ctx, name, is_utils=is_utils, use_fallback=use_fallback)
    if temperature is not None:
        endpoint = endpoint.model_copy(update={"temperature": temperature})
    raw_model = ModelFactory.get_model(endpoint)

    handler = DataEngineCallbackHandler(ctx)
    bound_model = raw_model.with_config(callbacks=[handler])

    return RobustChatModelWrapper(bound_model, ctx, endpoint=endpoint)  # type: ignore


get_llm: GetLLM = _get_llm


def get_routed_llm(
    ctx: ArtemisContext | None,
    *,
    role: str = "summarizer",
    is_utils: bool = True,
    model_name: str | None = None,
    temperature: float = 0.0,
    timeout: float | None = None,
) -> RobustChatModelWrapper | None:
    """Resolve a helper model (compression / summarizer lens) through the CONFIGURED router.

    **Vì sao có hàm này.** Bộ nén từng gọi ``get_google_llm(model_name=...)``, mà hàm đó
    *ép* ``provider=google`` trong khi tên model đến từ ``config/artemis.jsonc`` — vốn là
    tên của gateway (``ts/gemini-3.5-flash-lite``). Endpoint Google native không biết tên
    đó, nên mọi lượt nén trả 404 (đo được: một lần chạy Pro có 60 lượt vision, tất cả
    404). Tệ hơn, một nhánh ``except`` còn *âm thầm* rơi về Google — đúng kiểu đổi
    provider mà không ai cấu hình.

    Hợp đồng của hàm:

    * provider / endpoint / khoá xác thực lấy **từ cấu hình**, không hardcode;
    * ``model_name`` chỉ ghi đè *tên model*, giữ nguyên provider đã cấu hình;
    * lỗi ⇒ trả ``None`` để bên gọi **giữ nguyên context chưa nén**; hàm này không raise
      và không bao giờ tự chuyển sang provider khác.
    """
    if ctx is None:
        llm_logger.debug("Không có context nên không resolve được model cho '%s'", role)
        return None
    try:
        endpoint = _resolve_endpoint(ctx, role, is_utils=is_utils)
        if model_name:
            # Chỉ đổi tên model — provider/base_url/api_key vẫn theo cấu hình.
            endpoint = endpoint.model_copy(update={"model_name": str(model_name)})
        if temperature is not None:
            endpoint = endpoint.model_copy(update={"temperature": float(temperature)})
        if timeout is not None:
            endpoint = endpoint.model_copy(update={"timeout_seconds": float(timeout)})
        raw_model = ModelFactory.get_model(endpoint)
        handler = DataEngineCallbackHandler(ctx)
        bound_model = raw_model.with_config(callbacks=[handler])
        return RobustChatModelWrapper(bound_model, ctx, endpoint=endpoint)  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001 - thiếu cấu hình không được làm gãy tác vụ
        llm_logger.warning(
            "Không resolve được model '%s' qua router (%s): %s — bộ nén sẽ giữ nguyên context.",
            role,
            model_name or "theo cấu hình",
            exc,
        )
        return None


class _ObservedFallback(FallbackRunner):
    """Fall back only on failures another endpoint could fix, and record it.

    Errors are classified first; a bad request, for example, is re-raised
    because a weaker model would only hide the bug. Each switch is logged at
    WARNING and recorded as an ``llm_fallback`` event. While the main call
    runs, the retry layer knows a fallback exists and hands over as soon as
    retries run out instead of pausing the task.
    """

    @contextmanager
    def main_scope(self) -> Iterator[None]:
        # Reset before the fallback call starts: nothing is behind the
        # fallback, so if it runs out of retries the task should pause.
        token = _FALLBACK_AVAILABLE.set(True)
        try:
            yield
        finally:
            _FALLBACK_AVAILABLE.reset(token)

    def fallback_reason(self, error: Exception) -> tuple[str, str | None] | None:
        if isinstance(error, LLMCallError):
            reason, failure = "terminal_error", error.failure
        else:
            reason, failure = "error", classify_failure(error)
            if failure.category is FailureCategory.CANCELLED:
                return None
        return (reason, failure.category.value) if failure.should_fallback else None

    def on_fallback(self, reason: str, category: str | None, error: str | None) -> None:
        llm_logger.warning(
            f"❗ Main LLM inference failed ({reason}"
            f"{f': {error}' if error else ''}). Falling back..."
        )
        _record_llm_event(
            "llm_fallback",
            {
                "reason": reason,
                "category": category,
                "error": error[:500] if error else None,
            },
        )


# Runs main_call; switches to fallback_call when another endpoint could succeed.
with_fallback = _ObservedFallback().run
