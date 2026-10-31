"""Tests for the LLM gateway: complete(), classified recovery, and fallback."""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk
import pytest

from artemis.llm.reliability import (
    Failure,
    FailureCategory,
    LLMExhaustedError,
    LLMPermanentError,
)
from artemis.services import llm as llm_service
from artemis.services.llm import (
    RobustChatModelWrapper,
    acomplete,
    acomplete_structured,
    with_fallback,
)
from third_party.mobile_use.llm_service import WAITING_MESSAGE, LLMWaitNotice


@pytest.fixture(autouse=True)
def _isolate_gateway_state(monkeypatch, tmp_path):
    llm_service._ENDPOINT_BREAKER._states.clear()
    llm_service._NON_STREAMING_ENDPOINTS.clear()
    monkeypatch.setattr(llm_service, "PAUSE_FILE", tmp_path / ".artemis_paused")
    monkeypatch.setattr(
        llm_service,
        "retry_policy_for",
        lambda category: SimpleNamespace(max_attempts=2, delay_for=lambda attempt: 0.0),
    )
    monkeypatch.setattr(llm_service.settings, "LLM_PAUSE_TIMEOUT_SECONDS", 1.0, raising=False)
    yield
    llm_service._ENDPOINT_BREAKER._states.clear()
    llm_service._NON_STREAMING_ENDPOINTS.clear()


@pytest.mark.asyncio
async def test_mid_stream_failure_retries_without_duplicating_chunks():
    class BreaksMidStream:
        def __init__(self):
            self.astream_calls = 0

        async def astream(self, *args, **kwargs):
            self.astream_calls += 1
            if self.astream_calls == 1:
                yield AIMessageChunk(content="Hello")
                raise RuntimeError("503 service unavailable")
            yield AIMessageChunk(content="Hello")
            yield AIMessageChunk(content=" world")

    base = BreaksMidStream()
    wrapper = RobustChatModelWrapper(base)
    result = await wrapper.complete([])

    assert base.astream_calls == 2
    # The partial first attempt was discarded entirely: no duplicated prefix.
    assert result.content == "Hello world"


@pytest.mark.asyncio
async def test_auth_error_fails_fast_without_retry_or_pause(tmp_path):
    class AuthFails:
        def __init__(self):
            self.astream_calls = 0
            self.ainvoke_calls = 0

        async def astream(self, *args, **kwargs):
            self.astream_calls += 1
            raise RuntimeError("401 unauthorized: invalid api key")
            yield  # pragma: no cover - makes this an async generator

        async def ainvoke(self, *args, **kwargs):
            self.ainvoke_calls += 1
            return AIMessage(content="should never happen")

    base = AuthFails()
    wrapper = RobustChatModelWrapper(base)
    with pytest.raises(LLMPermanentError):
        await wrapper.complete([])

    assert base.astream_calls == 1
    assert base.ainvoke_calls == 0
    assert not llm_service.PAUSE_FILE.exists()


@pytest.mark.asyncio
async def test_stream_unsupported_downgrades_loudly_and_is_remembered():
    class NoStreaming:
        def __init__(self):
            self.astream_calls = 0
            self.ainvoke_calls = 0

        async def astream(self, *args, **kwargs):
            self.astream_calls += 1
            raise NotImplementedError("streaming is not supported")
            yield  # pragma: no cover

        async def ainvoke(self, *args, **kwargs):
            self.ainvoke_calls += 1
            return AIMessage(content="ok")

    base = NoStreaming()
    wrapper = RobustChatModelWrapper(base)

    first = await wrapper.complete([])
    assert first.content == "ok"
    assert wrapper._endpoint_key() in llm_service._NON_STREAMING_ENDPOINTS

    second = await wrapper.complete([])
    assert second.content == "ok"
    # The capability was cached: streaming is not probed again.
    assert base.astream_calls == 1
    assert base.ainvoke_calls == 2


@pytest.mark.asyncio
async def test_exhausted_retries_pause_then_raise_on_deadline():
    class AlwaysUnavailable:
        def __init__(self):
            self.calls = 0

        async def astream(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("503 service unavailable")
            yield  # pragma: no cover

    wrapper = RobustChatModelWrapper(AlwaysUnavailable())
    with pytest.raises(LLMExhaustedError):
        await wrapper.complete([])
    # The pause file was written for the pause window, then cleaned up.
    assert not llm_service.PAUSE_FILE.exists()


@pytest.mark.asyncio
async def test_with_fallback_takes_over_without_pausing():
    class AlwaysUnavailable:
        async def astream(self, *args, **kwargs):
            raise RuntimeError("503 service unavailable")
            yield  # pragma: no cover

    wrapper = RobustChatModelWrapper(AlwaysUnavailable())

    async def fallback_call():
        return AIMessage(content="fallback answer")

    result = await with_fallback(
        main_call=lambda: wrapper.complete([]),
        fallback_call=fallback_call,
    )
    assert result.content == "fallback answer"
    # Because a fallback existed, exhaustion handed over instead of pausing.
    assert not llm_service.PAUSE_FILE.exists()


@pytest.mark.asyncio
async def test_with_fallback_refuses_to_hide_bad_requests():
    class CodedError(Exception):
        code = 400

    calls = {"fallback": 0}

    async def main_call():
        raise CodedError("malformed tool schema")

    async def fallback_call():
        calls["fallback"] += 1
        return "hidden bug"

    with pytest.raises(CodedError):
        await with_fallback(main_call=main_call, fallback_call=fallback_call)
    assert calls["fallback"] == 0


@pytest.mark.asyncio
async def test_with_fallback_clears_fallback_flag_when_cancelled():
    seen = {}

    async def main_call():
        seen["during"] = llm_service._FALLBACK_AVAILABLE.get()
        raise asyncio.CancelledError

    async def fallback_call():  # pragma: no cover - must not run
        raise AssertionError("fallback must not run on cancellation")

    with pytest.raises(asyncio.CancelledError):
        await with_fallback(main_call=main_call, fallback_call=fallback_call)
    assert seen["during"] is True
    assert llm_service._FALLBACK_AVAILABLE.get() is False


@pytest.mark.asyncio
async def test_timeout_wrapper_enforces_hard_timeout():
    notices = []
    wait = LLMWaitNotice(notify=notices.append, on_timeout=notices.append, poll_interval=0.01)

    with pytest.raises(TimeoutError):
        await wait(asyncio.sleep(10), timeout_seconds=0.01, hard_timeout=0.05)
    assert notices == [WAITING_MESSAGE, "LLM call timed out after 0.05 seconds."]


@pytest.mark.asyncio
async def test_timeout_wrapper_holds_clock_while_paused():
    wait = LLMWaitNotice(
        notify=lambda _: None, on_timeout=lambda _: None, hold=lambda: True, poll_interval=0.01
    )

    async def slow():
        await asyncio.sleep(0.1)
        return "done"

    # The 0.02s hard timeout never runs out while the task is held (paused).
    assert await wait(slow(), timeout_seconds=0.01, hard_timeout=0.02) == "done"


@pytest.mark.asyncio
async def test_acomplete_structured_corrective_reask():
    class CorrectsItself:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, messages, **kwargs):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return AIMessage(content="oops, here is prose instead of JSON")
            return AIMessage(content='{"a": 1}')

    base = CorrectsItself()
    result = await acomplete_structured(base, [])
    assert result == {"a": 1}
    assert len(base.calls) == 2
    correction_text = str(base.calls[1][-1].content)
    assert "could not be parsed" in correction_text


@pytest.mark.asyncio
async def test_acomplete_compat_path_for_plain_models():
    class PlainModel:
        async def astream(self, *args, **kwargs):
            yield AIMessageChunk(content="a")
            yield AIMessageChunk(content="b")

    result = await acomplete(PlainModel(), [])
    assert result.content == "ab"

    class InvokeOnly:
        async def ainvoke(self, *args, **kwargs):
            return AIMessage(content="direct")

    result = await acomplete(InvokeOnly(), [])
    assert result.content == "direct"


@pytest.mark.asyncio
async def test_mid_stream_failure_records_stream_reset_payload(monkeypatch):
    recorded_events = []

    def mock_record_event(name, payload, *, status="retrying"):
        recorded_events.append((name, payload, status))

    monkeypatch.setattr(llm_service, "_record_llm_event", mock_record_event)

    class BreaksMidStream:
        def __init__(self):
            self.calls = 0

        async def astream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield AIMessageChunk(content="Partial thoughts...")
                raise RuntimeError("503 high demand spike")
            yield AIMessageChunk(content="Recovered output")

    base = BreaksMidStream()
    wrapper = RobustChatModelWrapper(base)
    result = await wrapper.complete([])

    assert result.content == "Recovered output"
    resets = [e for e in recorded_events if e[0] == "llm_stream_reset"]
    assert len(resets) == 1
    event_name, payload, status = resets[0]
    assert event_name == "llm_stream_reset"
    assert payload["action"] == "discard"
    assert payload["reason"] == "mid_stream_failure"
    assert "stream_exec_id" in payload
    assert "lower API priority" in payload["message"]


#: 429 quota **theo ngày** của Google Free Tier: non-retryable, nhưng phải **đổi model**.
DAILY_QUOTA_ERROR = (
    "429 Too Many Requests: You exceeded your current quota, please check your plan and billing"
    " details. * Quota exceeded for metric:"
    " generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 500, model:"
    " gemini-3.5-flash-lite ... 'quotaMetric':"
    " 'generate_content_free_tier_requests', 'retryDelay': '14456s'"
)


@pytest.mark.asyncio
async def test_a_daily_quota_hands_over_to_the_fallback_instead_of_dying():
    """Quota theo ngày + có model dự phòng ⇒ bàn giao, không chết task.

    Đo trên máy thật 31/10/2026 (task 3110_0957): Operator cạn quota ``gemini-3.5-flash-lite``
    rồi cả task chết bằng ``LLMPermanentError``, trong khi các model khác trong
    ``ARTEMIS_LLM_CHAIN_OPERATOR`` còn nguyên quota — nhánh "non-retryable" cũ bỏ qua
    ``should_fallback`` nên **không bao giờ** bàn giao cho model kế.
    """

    class QuotaDead:
        async def astream(self, *args, **kwargs):
            raise RuntimeError(DAILY_QUOTA_ERROR)
            yield  # pragma: no cover

    wrapper = RobustChatModelWrapper(QuotaDead())
    calls = {"fallback": 0}

    async def fallback_call():
        calls["fallback"] += 1
        return AIMessage(content="câu trả lời từ model kế")

    result = await with_fallback(
        main_call=lambda: wrapper.complete([]),
        fallback_call=fallback_call,
    )

    assert result.content == "câu trả lời từ model kế"
    assert calls["fallback"] == 1


@pytest.mark.asyncio
async def test_a_daily_quota_without_a_fallback_still_fails_fast():
    """Không có model dự phòng thì vẫn là lỗi chết — không pause chờ quota hàng giờ."""

    class QuotaDead:
        async def astream(self, *args, **kwargs):
            raise RuntimeError(DAILY_QUOTA_ERROR)
            yield  # pragma: no cover

    wrapper = RobustChatModelWrapper(QuotaDead())
    with pytest.raises(LLMPermanentError):
        await wrapper.complete([])
    assert not llm_service.PAUSE_FILE.exists()


@pytest.mark.asyncio
async def test_an_open_circuit_hands_over_instead_of_waiting():
    """Mạch mở (model vừa cạn quota) + có dự phòng ⇒ bàn giao ngay, không chờ cooldown.

    Đo trên máy thật 31/10/2026 (task 3110_0959): Planner chờ mạch ``google:gemini-3.5-flash-lite``
    tới 120s rồi vẫn 429, trong khi model kế trong chuỗi còn quota — người dùng phải bấm Dừng.
    """
    key = "google:gemini-3.5-flash-lite"
    llm_service._ENDPOINT_BREAKER.record_failure(
        key, Failure(FailureCategory.RATE_LIMIT, False, True), open_for=3600
    )
    token = llm_service._FALLBACK_AVAILABLE.set(True)
    try:
        with pytest.raises(LLMExhaustedError):
            await llm_service._wait_for_breaker(key)
    finally:
        llm_service._FALLBACK_AVAILABLE.reset(token)


@pytest.mark.asyncio
async def test_an_open_circuit_without_a_fallback_still_waits():
    """Không có dự phòng thì vẫn chờ hết cooldown rồi đi tiếp (không bỏ lượt gọi)."""
    key = "google:gemini-3.5-flash-lite"
    llm_service._ENDPOINT_BREAKER.record_failure(
        key, Failure(FailureCategory.RATE_LIMIT, False, True), open_for=0.05
    )

    await llm_service._wait_for_breaker(key)  # không ném lỗi


def test_the_perf_span_records_provider_cache_hits():
    """``cached_tokens``/``cache_ratio`` phải vào span của **mọi** lượt gọi.

    Trước đây chỉ số cache chỉ có trong log ``step-metrics`` của Operator, nên không đo được tỉ lệ
    cache cho Planner/Checker/sub-agent — mà đây là thứ duy nhất trả lời được "tiền tố prompt có ổn
    định không" khi tối ưu context (Phase A).
    """
    response = AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 10_000,
            "output_tokens": 20,
            "total_tokens": 10_020,
            "input_token_details": {"cache_read": 8_000},
        },
    )

    class Span:
        def __init__(self):
            self.values: dict = {}

        def set(self, **kwargs):
            self.values.update(kwargs)

        def note(self, **kwargs):
            self.values.update(kwargs)

    span = Span()
    llm_service.RobustChatModelWrapper(object())._finish_perf_span(span, response)

    assert span.values["input_tokens"] == 10_000
    assert span.values["cached_tokens"] == 8_000
    assert span.values["cache_ratio"] == 0.8
