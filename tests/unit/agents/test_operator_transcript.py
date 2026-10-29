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

"""Operator prompt snapshots and transcript mode.

Flag off (``agent.memory.transcript.enabled=false``): the legacy 2-message
build must stay byte-for-byte identical — pinned by SHA-256 goldens recorded
for the current template.

Flag on: ``_build_prompt`` renders ``S + F + A + tail`` from the session
transcript ledger; the turn's messages are staged after the tool loop and
committed (with step key and validator result) at the next build.
"""

import base64
import hashlib
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from artemis.agents.operator.operator import OperatorNode
from artemis.agents.operator.prompts import load_operator_prompts
from artemis.agents.operator.prompts import (
    PLAN_HISTORY_STATIC_POINTER,
    PLAN_HISTORY_TEMPLATE_SECTION,
    PromptBuilder,
    TemplatePromptComponent,
    render_transcript_static_system,
)
from artemis.config.agent import MemoryTranscriptConfig
from artemis.context import ArtemisContext
from artemis.memory.step_memory import StepMemoryService
from artemis.memory.transcript import (
    EPHEMERAL_BLOCKS_KEY,
    EXECUTION_RESULT_MARKER,
    PLAN_RECITATION_MARKER,
    RESTORED_HISTORY_HEADER,
    TranscriptLedger,
)

# SHA-256 snapshots of the legacy system message with the fixed inputs below.
# Update these when an intentional template change alters the rendered prompt.
#
# Cập nhật 29/10/2026: thêm đoạn dạy ``kb_lookup`` (tra cứu KB giữa lúc chạy) — thay đổi có chủ ý,
# không phải trôi byte. Hash cũ: empty=513e0341…, sentinel=012fe1b0…
#
# Cập nhật 29/10/2026 (Phase 1 tối ưu token): system message **không còn chứa** section
# ``## Current Plan & Execution History`` và **nhận thêm** đuôi template (phần sau mốc
# ``# CURRENT OBSERVATION``). Hai thay đổi đều là *di chuyển khối*, không thêm/bớt quy tắc nào:
# chỉ dẫn đứng trước thành tiền tố chung giữa các lượt (đo trên trace 5b459634: 5.311 → 7.372 token
# tiền tố chung, tức 55% → 76% prompt có thể được cache), còn kế hoạch + lịch sử (đổi mỗi lượt)
# xuống đầu message human. Vì thế **hai plan khác nhau giờ cho cùng một hash** — đó chính là tính
# chất cần giữ, và test dưới đây kiểm cả hai mặt (byte-stable + độc lập với plan).
#
# Cập nhật 29/10/2026 (lần 2): mốc ``# CURRENT OBSERVATION`` kèm **đồng hồ phiên** ``[T+mm:ss]``
# (đổi mỗi lượt) được chuyển hẳn sang message human — system không còn ký tự nào đổi theo thời gian,
# nên tiền tố ổn định là **toàn bộ** system message. Hash cũ (lần 1): 67bd6227…
GOLDEN_EMPTY_PLAN = "7721bbc3fc869f462ae6812a5c591ab11425026db923f1278f767e070ffab4e6"
GOLDEN_SENTINEL_PLAN = "7721bbc3fc869f462ae6812a5c591ab11425026db923f1278f767e070ffab4e6"

SCREENSHOT_B64 = base64.b64encode(b"fake-jpeg-bytes").decode("utf-8")


async def _render_legacy(plan_and_history: str, ctx=None) -> tuple[str, list[str]]:
    """(system text, human text parts) của đường legacy với đầu vào cố định."""
    builder = PromptBuilder()
    component = TemplatePromptComponent()
    ctx = ctx or SimpleNamespace(execution_setup=None, actuator=None, data_engine=None)
    state = SimpleNamespace(initial_goal="GOLDEN_FIXED_GOAL")
    await component(
        builder,
        state,
        ctx,
        prompts=load_operator_prompts(),
        template_name="main_template",
        plan_and_history=plan_and_history,
    )
    human = [p for p in builder.human_parts if isinstance(p, str)]
    if builder.human_footer:
        human.append(builder.human_footer)
    return "".join(builder.system_parts), human


async def _render_legacy_system(plan_and_history: str) -> str:
    return (await _render_legacy(plan_and_history))[0]


@pytest.mark.asyncio
async def test_legacy_prompt_bytes_match_pre_split_golden():
    empty = await _render_legacy_system("No plan or history yet.")
    sentinel = await _render_legacy_system("PLAN_AND_HISTORY_SENTINEL\nline2")
    assert hashlib.sha256(empty.encode("utf-8")).hexdigest() == GOLDEN_EMPTY_PLAN
    assert hashlib.sha256(sentinel.encode("utf-8")).hexdigest() == GOLDEN_SENTINEL_PLAN


@pytest.mark.asyncio
async def test_legacy_system_is_plan_independent_and_the_plan_moves_to_the_human():
    """Phase 1 tối ưu token: system message = chỉ dẫn bất biến, không phụ thuộc kế hoạch.

    Kế hoạch + lịch sử (đổi mỗi lượt) nằm ở **đầu message human**; nội dung không mất chữ nào.
    """
    system_a, human_a = await _render_legacy("PLAN_A")
    system_b, human_b = await _render_legacy("PLAN_B")

    assert system_a == system_b  # tiền tố chung giữa hai lượt = toàn bộ system message
    assert "PLAN_A" not in system_a and "PLAN_B" not in system_b
    assert human_a[0].strip().endswith("PLAN_A")
    assert human_b[0].strip().endswith("PLAN_B")
    # Quy tắc của đuôi template vẫn còn, giờ nằm trong system (bất biến).
    for rule in (
        "Loop Exit Evaluation",
        "Preserve Memory & Pre-Action Snapshotting",
        "Phase 3: Target Selection & Physical Execution",
        "Target Resolution & Index Safety",
        "Action Selection",
    ):
        assert rule in system_a


@pytest.mark.asyncio
async def test_the_session_clock_never_enters_the_system_message():
    """``# CURRENT OBSERVATION [T+mm:ss]`` đổi mỗi lượt ⇒ phải nằm ở human, không ở system.

    Kiểm bằng hai ngữ cảnh có đồng hồ phiên **khác nhau**: system phải giống nhau từng byte (đó là
    tiền tố ổn định để provider cache), còn đồng hồ thật phải xuất hiện trong message human. Mẫu
    ``T+12:30`` trong system là ví dụ tĩnh của template, không phải đồng hồ phiên.
    """
    import re
    import time

    def ctx_with(start: float) -> SimpleNamespace:
        return SimpleNamespace(
            execution_setup=None,
            actuator=None,
            data_engine=SimpleNamespace(session_start_time=start),
        )

    now = time.time()
    system_a, human_a = await _render_legacy("PLAN", ctx=ctx_with(now))
    system_b, human_b = await _render_legacy("PLAN", ctx=ctx_with(now - 125))

    assert system_a == system_b  # không có gì đổi theo thời gian trong system
    clocks_a = [p for p in human_a if re.search(r"T\+\d\d:\d\d", p)]
    clocks_b = [p for p in human_b if re.search(r"T\+\d\d:\d\d", p)]
    assert clocks_a and clocks_b
    assert clocks_a[0] != clocks_b[0]  # đồng hồ thật sự đổi giữa hai lượt
    assert "# CURRENT OBSERVATION" in clocks_a[0]


def test_main_template_contains_exactly_one_plan_history_section():
    template = load_operator_prompts()["main_template"]
    assert template.count(PLAN_HISTORY_TEMPLATE_SECTION) == 1


def test_transcript_static_system_is_stable_and_carries_no_history():
    ctx = SimpleNamespace(execution_setup=None, actuator=None, data_engine=None)
    state = SimpleNamespace(initial_goal="STATIC_GOAL")
    prompts = load_operator_prompts()

    first = render_transcript_static_system(prompts, ctx, state)
    second = render_transcript_static_system(prompts, ctx, state)
    assert first == second  # byte-stable S region

    assert "STATIC_GOAL" in first
    # The volatile section is replaced by the static pointer...
    assert PLAN_HISTORY_STATIC_POINTER.splitlines()[1] in first
    # ...and no unrendered slot or trailing observation header survives.
    assert "{{" not in first
    assert not first.endswith("# CURRENT OBSERVATION\n")
    # The history guide names the marker a resolved screenshot renders as.
    assert "--- Historical Visual Transition ---" in first


def _transcript_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = None
    ctx.actuator = None
    ctx.data_engine = None
    ctx.step_memory = StepMemoryService(ctx=None)
    ctx.transcript_ledger = None
    return ctx


def _transcript_state(**overrides):
    state = MagicMock()
    state.subagent_calls = []
    state.initial_goal = "Transcript goal"
    state.operator_feedback = None
    state.injected_instruction = None
    state.operator_tool_limit_exceeded = False
    state.structured_decisions = None
    state.last_execution_result = None
    state.current_step_id = None
    state.latest_ui_hierarchy = []
    state.operator_raw_data = {
        "screenshot_b64": SCREENSHOT_B64,
        "xml_hierarchy": [],
        "ocr_results": None,
        "width": 1080,
        "height": 2400,
    }
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _no_action_llm(captured: list):
    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.tool_calls = []
    mock_response.content = "no action this turn"

    async def mock_ainvoke(*args, **kwargs):
        captured.append(list(args[0]))
        return mock_response

    mock_llm.ainvoke = AsyncMock(side_effect=mock_ainvoke)
    mock_llm.bind_tools.return_value = mock_llm
    return mock_llm


@pytest.mark.asyncio
async def test_transcript_mode_two_turns_build_four_regions(tmp_path):
    ctx = _transcript_ctx()
    # Turn 2 has a recorded step but no committed ledger turn yet. This is
    # an active session, so it must not trigger cold-start history seeding.
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    ctx.data_engine.global_base_dir = str(tmp_path)
    ctx.data_engine.get_agent_friendly_steps.return_value = []
    captured: list = []
    mock_llm = _no_action_llm(captured)

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=True))

        # Turn 1
        await node(_transcript_state())
        ledger = ctx.transcript_ledger
        assert isinstance(ledger, TranscriptLedger)
        assert ledger.has_static_prefix
        assert ledger.has_staged_turn
        assert ledger.turn_count == 0

        turn1_messages = captured[0]
        assert isinstance(turn1_messages[0], SystemMessage)
        assert isinstance(turn1_messages[-1], HumanMessage)
        tail1_text = str(turn1_messages[-1].content)
        assert "# CURRENT OBSERVATION [T+" in tail1_text
        assert PLAN_RECITATION_MARKER in tail1_text

        # Turn 2: step 1 is now recorded, and the previous turn is committed
        # with its step id and result.
        ctx.data_engine.get_agent_friendly_steps.return_value = [
            {
                "step_id": "step-1",
                "step_number": 1,
                "summary": "Step 1 summary",
                "relative_time": "1.0s",
                "extra_metadata": {"subgoal_hash": "default"},
            }
        ]
        await node(
            _transcript_state(
                current_step_id="step-1",
                structured_decisions='[{"action": "tap"}]',
                last_execution_result={"status": "dispatched"},
            )
        )

        # The staged-but-uncommitted turn 2 state never triggers cold-start
        # seeding (the transcript path stayed live instead of falling back).
        assert not ledger.has_restored_history
        assert ledger.turn_count == 1
        turn2_messages = captured[1]
        # S region is the same object with the same bytes.
        assert turn2_messages[0] is turn1_messages[0]

        active = list(ledger.active_messages)
        # Committed turn-1 observation followed by its validator result.
        assert any(EXECUTION_RESULT_MARKER in str(m.content) for m in active)
        committed_obs = active[0]
        # Text edge at depth 1: the committed observation has lost its plan
        # recitation as soon as the next tail exists (its screenshot stays
        # until the screenshot edge).
        assert PLAN_RECITATION_MARKER not in str(committed_obs.content)
        # The live tail still recites the plan.
        assert PLAN_RECITATION_MARKER in str(turn2_messages[-1].content)
        # Message layout is S + A + tail.
        assert turn2_messages[-1] is not turn1_messages[-1]
        assert re.search(r"T\+\d{2,}:\d{2}", str(turn2_messages[-1].content))


@pytest.mark.asyncio
async def test_transcript_tail_marks_per_turn_notices_ephemeral():
    """The hand-built tail carries the builder's ephemeral indices: per-turn
    notices are deleted by the scrub edge; header, plan recitation and the
    observation itself are not."""
    from artemis.agents.operator.prompts import USER_GUIDANCE_MARKER

    ctx = _transcript_ctx()
    captured: list = []
    mock_llm = _no_action_llm(captured)
    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=True))
        await node(
            _transcript_state(
                injected_instruction="Skip the login step",
                user_stop_requested=False,
                operator_tool_limit_exceeded=True,
            )
        )

    tail = captured[0][-1]
    assert isinstance(tail, HumanMessage)
    texts = {
        i: block.get("text", "")
        for i, block in enumerate(tail.content)
        if isinstance(block, dict) and block.get("type") == "text"
    }
    guidance = next(i for i, t in texts.items() if t.startswith(USER_GUIDANCE_MARKER))
    warning = next(i for i, t in texts.items() if "tool-call budget" in t)
    assert set(tail.additional_kwargs[EPHEMERAL_BLOCKS_KEY]) == {guidance, warning}
    # The user's stop bit is relayed on the guidance block itself.
    assert "authorized stopping" not in texts[guidance]
    # The verbatim instruction is its own persistent block right after the
    # wrapper (stamped with the ledger's session offset), never ephemeral:
    # a standing instruction must outlive the turn it arrived on.
    body = guidance + 1
    assert re.fullmatch(r'User instruction \(T\+\d{2,}:\d{2}\): "Skip the login step"', texts[body])
    assert body not in tail.additional_kwargs[EPHEMERAL_BLOCKS_KEY]
    assert "Skip the login step" not in texts[guidance]
    # Header, plan recitation and the observation stay in the history.
    assert texts[0].startswith("# CURRENT OBSERVATION")
    plan = next(i for i, t in texts.items() if t.startswith(PLAN_RECITATION_MARKER))
    screenshot = next(i for i, t in texts.items() if t == "--- Current Screenshot ---")
    assert not {0, plan, screenshot} & set(tail.additional_kwargs[EPHEMERAL_BLOCKS_KEY])


@pytest.mark.asyncio
async def test_transcript_actionless_turn_commits_without_validator_message():
    ctx = _transcript_ctx()
    captured: list = []
    mock_llm = _no_action_llm(captured)

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=True))
        await node(_transcript_state())
        # Planner-rejected turns clear structured_decisions: no validator ran.
        await node(
            _transcript_state(
                current_step_id="step-1",
                structured_decisions="",
                last_execution_result={"status": "dispatched"},
            )
        )

    ledger = ctx.transcript_ledger
    assert ledger.turn_count == 1
    assert not any(EXECUTION_RESULT_MARKER in str(m.content) for m in ledger.active_messages)


@pytest.mark.asyncio
async def test_transcript_cold_start_builds_restored_history_block(tmp_path):
    ctx = _transcript_ctx()
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    ctx.data_engine.global_base_dir = str(tmp_path)
    ctx.data_engine.get_agent_friendly_steps.return_value = [
        {
            "step_id": f"step_{i}",
            "step_number": i,
            "summary": f"Step {i} summary",
            "relative_time": f"{i}.0s",
            "extra_metadata": {"subgoal_hash": "default"},
        }
        for i in range(1, 4)
    ]

    captured: list = []
    mock_llm = _no_action_llm(captured)

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=True))
        await node(_transcript_state())

    ledger = ctx.transcript_ledger
    assert ledger.has_restored_history

    messages = captured[0]
    restored_text = str(messages[1].content)
    assert RESTORED_HISTORY_HEADER in restored_text
    assert "--- Execution History ---" in restored_text
    assert "Step 1 summary" in restored_text
    # The live tail still follows normally and A stays appendable.
    assert "# CURRENT OBSERVATION [T+" in str(messages[-1].content)
    assert ledger.has_staged_turn


@pytest.mark.asyncio
async def test_flag_off_keeps_legacy_two_message_build_and_no_ledger():
    ctx = _transcript_ctx()
    captured: list = []
    mock_llm = _no_action_llm(captured)

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=False))
        await node(_transcript_state())

    assert len(captured[0]) == 2
    assert isinstance(captured[0][0], SystemMessage)
    assert isinstance(captured[0][1], HumanMessage)
    assert ctx.transcript_ledger is None
