"""Salvaging non-strict structured-output answers.

Regression (2026-10-23): a prompt asking the agent to relaunch the app made it
call ``manage_app(action="launch")``, which starts the Hopper sub-agent. Hopper's
``with_structured_output(HopperOutput)`` call failed because the model answered
with prose plus a fenced ```json block::

    LLM Error: 1 validation error for HopperOutput
      Invalid JSON: expected value at line 1 column 1
      [input_value='```json\\n{\\n  "found": f...ing to rule 4."\\n}\\n```']
      Pausing execution to wait for resume signal...

The parse error surfaced *inside* the retry/pause policy, so the call was retried
(tokens burned) and the task then paused for ``LLM_PAUSE_TIMEOUT_SECONDS``
(900s by default). The console showed "Đang chạy" forever and the app was never
launched. ``_TolerantStructuredRunnable`` re-parses the raw answer through the
shared tolerant pipeline instead.
"""

import asyncio

from langchain_core.messages import AIMessage
from pydantic import BaseModel
import pytest

from artemis.services.llm import RobustChatModelWrapper, _TolerantStructuredRunnable


class HopperOutput(BaseModel):
    found: bool
    output: str | None
    reason: str


# Shape captured verbatim from the daemon log on 2026-10-23.
FENCED_ANSWER = (
    "```json\n{\n"
    '  "found": false,\n'
    '  "output": null,\n'
    '  "reason": "Hunonic is not visible; nothing matched rule 4."\n'
    "}\n```"
)


class _Chain:
    """Stand-in for a ``with_structured_output(include_raw=True)`` chain."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def ainvoke(self, *args, **kwargs):
        self.calls += 1
        return self.payload


def _raw_payload(text: str, error: Exception | None = None) -> dict:
    return {"raw": AIMessage(content=text), "parsed": None, "parsing_error": error}


def test_recovers_fenced_json_answer():
    """The exact answer that hung the run must parse instead of raising."""
    chain = _TolerantStructuredRunnable(_Chain(_raw_payload(FENCED_ANSWER)), HopperOutput)
    result = asyncio.run(chain.ainvoke([]))
    assert isinstance(result, HopperOutput)
    assert result.found is False
    assert result.output is None
    assert "rule 4" in result.reason


def test_recovers_prose_json_answer():
    text = 'I looked at the list. {"found": true, "output": "com.iot.hunonic", "reason": "matched"}'
    chain = _TolerantStructuredRunnable(_Chain(_raw_payload(text)), HopperOutput)
    result = asyncio.run(chain.ainvoke([]))
    assert isinstance(result, HopperOutput)
    assert result.output == "com.iot.hunonic"


def test_strictly_parsed_answer_is_passed_through_untouched():
    parsed = HopperOutput(found=True, output="com.iot.hunonic", reason="cached")
    chain = _TolerantStructuredRunnable(
        _Chain({"raw": None, "parsed": parsed, "parsing_error": None}), HopperOutput
    )
    assert asyncio.run(chain.ainvoke([])) is parsed


def test_unparsable_answer_raises_the_original_error():
    boom = ValueError("1 validation error for HopperOutput")
    chain = _TolerantStructuredRunnable(
        _Chain(_raw_payload("there is no JSON in this answer", boom)), HopperOutput
    )
    with pytest.raises(ValueError, match="validation error"):
        asyncio.run(chain.ainvoke([]))


def test_non_dict_result_is_returned_unchanged():
    """FakeChatModel-style models return a message, not an include_raw dict."""
    message = AIMessage(content="plain answer")
    chain = _TolerantStructuredRunnable(_Chain(message), HopperOutput)
    assert asyncio.run(chain.ainvoke([])) is message


class _BaseModelStub:
    def __init__(self, payload):
        self.payload = payload
        self.kwargs: dict | None = None

    def with_structured_output(self, schema, **kwargs):
        self.kwargs = kwargs
        return _Chain(self.payload)


def test_wrapper_requests_raw_and_salvages_fenced_answer():
    base = _BaseModelStub(_raw_payload(FENCED_ANSWER))
    structured = RobustChatModelWrapper(base).with_structured_output(HopperOutput)
    assert base.kwargs == {"include_raw": True}
    assert isinstance(structured, RobustChatModelWrapper)
    result = asyncio.run(structured.ainvoke([]))
    assert isinstance(result, HopperOutput) and result.found is False


def test_caller_asking_for_include_raw_keeps_the_dict():
    base = _BaseModelStub(_raw_payload(FENCED_ANSWER))
    structured = RobustChatModelWrapper(base).with_structured_output(
        HopperOutput, include_raw=True
    )
    result = asyncio.run(structured.ainvoke([]))
    assert isinstance(result, dict)
    assert result["parsed"] is None
    assert result["raw"] is not None


def test_model_without_structured_output_still_raises():
    with pytest.raises(AttributeError):
        RobustChatModelWrapper(object()).with_structured_output(HopperOutput)


# --- json_schema parser raises even with include_raw=True -------------------
# langchain-openai >= 0.3 defaults to method="json_schema", and that branch has
# no include_raw fallback: the ValidationError escapes. Its errors()[*]["input"]
# still carries the model's raw answer, so it can be salvaged without a second
# LLM call.


class _RaisingChain:
    def __init__(self, error: BaseException):
        self.error = error

    async def ainvoke(self, *args, **kwargs):
        raise self.error


def _real_validation_error() -> BaseException:
    """The exact error the strict json_schema parser raises for a fenced answer."""
    try:
        HopperOutput.model_validate_json(FENCED_ANSWER)
    except Exception as exc:  # noqa: BLE001 - this is the subject under test
        return exc
    raise AssertionError("expected the strict parser to raise")


def test_recovers_from_strict_parser_validation_error():
    error = _real_validation_error()
    assert "Invalid JSON" in str(error)
    chain = _TolerantStructuredRunnable(_RaisingChain(error), HopperOutput)
    result = asyncio.run(chain.ainvoke([]))
    assert isinstance(result, HopperOutput)
    assert result.found is False
    assert "rule 4" in result.reason


def test_recovers_from_wrapped_validation_error():
    outer = RuntimeError("structured output failed")
    outer.__cause__ = _real_validation_error()
    chain = _TolerantStructuredRunnable(_RaisingChain(outer), HopperOutput)
    result = asyncio.run(chain.ainvoke([]))
    assert isinstance(result, HopperOutput)


def test_unrecoverable_error_is_re_raised():
    chain = _TolerantStructuredRunnable(_RaisingChain(ValueError("boom")), HopperOutput)
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(chain.ainvoke([]))
