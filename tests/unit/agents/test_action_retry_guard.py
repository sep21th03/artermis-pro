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

"""PHASE A — action retry guard: chỉ tính **thất bại thật**, chặn sticky theo fingerprint.

Đo trên máy thật 30/10/2026 (session ``4806881d``): guard cũ chặn được nhưng (a) tính cả lần không
dispatch, (b) mở incident như một thất bại, (c) không dính qua các lượt nên cùng cú tap bị chặn lại
9 lần, và (d) khoá chỉ có toạ độ nên cùng toạ độ trên màn hình **đã đổi** cũng bị chặn oan.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from artemis.agents.validator.execution_loop import (
    OUTCOME_FAILED,
    OUTCOME_NEUTRAL,
    OUTCOME_NO_PROGRESS,
    OUTCOME_PROGRESS,
    REPEAT_BLOCK_AFTER,
    REPEAT_BLOCK_RECOVERY_TURNS,
    REPEAT_OBSERVE_AFTER,
    action_ui_fingerprint,
    active_subgoal_text,
    REPEAT_CYCLE_OBSERVE_AFTER,
    note_action_outcome,
    note_blocked_attempt,
    note_swipe_cycle,
    repeat_fingerprint,
    repeat_guard_verdict,
    swipe_cycle_key,
    swipe_cycle_verdict,
    swipe_direction,
)

TAP = {"action": "tap", "coordinates": [110, 593], "target_text": "Dled"}


def _node():
    return SimpleNamespace(ctx=SimpleNamespace())


def _fp(action=None, *, ui="ui-a", subgoal="Tap Dled card") -> str:
    return repeat_fingerprint("tap", action or TAP, ui_fingerprint=ui, subgoal=subgoal)


def test_fingerprint_covers_action_target_coords_ui_and_subgoal():
    base = _fp()

    assert base.startswith("tap|Dled|110,590")
    assert "ui=ui-a" in base and "sg=Tap Dled card" in base
    assert _fp(ui="ui-b") != base, "UI đổi ⇒ hành động mới"
    assert _fp(subgoal="Verify on Home") != base, "sub-goal đổi ⇒ hành động mới"
    assert _fp({"action": "tap", "coordinates": [110, 593], "target_text": "Khác"}) != base
    assert _fp({"action": "tap", "coordinates": [360, 637], "target_text": "Dled"}) != base


def test_only_real_failures_accumulate():
    node = _node()
    fp = _fp()

    assert note_action_outcome(node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="s") == 1
    assert note_action_outcome(node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="s") == 2
    # Thành công/không phải thất bại (already_applied, gate chặn, không dispatch) ⇒ xoá bộ đếm.
    assert note_action_outcome(node, fp, outcome=OUTCOME_PROGRESS, ui_fingerprint="ui-a", subgoal="s") == 0
    assert note_action_outcome(node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="s") == 1


def test_a_changed_screen_restarts_the_count():
    node = _node()
    fp = _fp(ui="ui-a")

    note_action_outcome(node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="s")
    note_action_outcome(node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="s")
    # Cùng fingerprint nhưng UI đã đổi ⇒ đếm lại từ 1 (không chặn oan).
    assert note_action_outcome(node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-b", subgoal="s") == 1


def test_two_failures_make_the_third_attempt_blocked_and_request_re_observation():
    """Bất biến: sau 2 thất bại thật, **lần thử thứ 3** bị chặn và Operator nhận yêu cầu cụ thể."""
    node = _node()
    fp = _fp()

    for _ in range(REPEAT_OBSERVE_AFTER):
        note_action_outcome(
            node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="Tap Dled card"
        )

    request = node.ctx.repeat_observation_request
    assert "thất bại thật" in request and "KHÔNG tap lại cùng toạ độ" in request
    reason = repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card")
    assert reason and "KHÔNG chạy lại" in reason


def test_neutral_outcomes_neither_count_nor_reset_the_failure_counter():
    """Yêu cầu rà soát: gate blocked / no_action / repeat_blocked **không** được tự reset bộ đếm."""
    node = _node()
    fp = _fp()
    for _ in range(2):
        note_action_outcome(
            node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="Tap Dled card"
        )

    for _ in range(3):  # ba lượt trung tính liên tiếp
        assert (
            note_action_outcome(
                node, fp, outcome=OUTCOME_NEUTRAL, ui_fingerprint="ui-a", subgoal="Tap Dled card"
            )
            == 2
        )

    assert repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card"), (
        "bộ đếm phải giữ nguyên ⇒ lần thử kế tiếp vẫn bị chặn"
    )


def test_two_failures_then_a_gate_block_then_the_third_tap_is_still_blocked():
    """Ca yêu cầu 1: 2 tap failed → 1 gate blocked → tap lần 3 vẫn bị chặn."""
    node = _node()
    fp = _fp()
    for _ in range(2):
        note_action_outcome(
            node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="Tap Dled card"
        )
    note_action_outcome(
        node, fp, outcome=OUTCOME_NEUTRAL, ui_fingerprint="ui-a", subgoal="Tap Dled card"
    )  # lượt bị gate ledger chặn (không tới thiết bị)

    assert repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card")


def test_two_failures_then_a_no_action_turn_then_the_third_tap_is_still_blocked():
    """Ca yêu cầu 2: 2 tap failed → 1 lượt no_action (vd chờ retry 429) → tap lần 3 vẫn bị chặn."""
    node = _node()
    fp = _fp()
    for _ in range(2):
        note_action_outcome(
            node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="Tap Dled card"
        )
    note_action_outcome(
        node, fp, outcome=OUTCOME_NEUTRAL, ui_fingerprint="ui-a", subgoal="Tap Dled card"
    )  # lượt không phát action nào

    assert repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card")


def test_a_dispatch_without_ui_change_does_not_reset_the_counter():
    """``no_progress`` giữ nguyên bộ đếm — chỉ bằng chứng tiến triển thật mới reset."""
    node = _node()
    fp = _fp()
    for _ in range(2):
        note_action_outcome(
            node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="Tap Dled card"
        )

    assert (
        note_action_outcome(
            node, fp, outcome=OUTCOME_NO_PROGRESS, ui_fingerprint="ui-a", subgoal="Tap Dled card"
        )
        == 2
    )
    assert repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card")


def test_progress_evidence_resets_the_counter():
    node = _node()
    fp = _fp()
    for _ in range(2):
        note_action_outcome(
            node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="Tap Dled card"
        )

    assert (
        note_action_outcome(
            node, fp, outcome=OUTCOME_PROGRESS, ui_fingerprint="ui-a", subgoal="Tap Dled card"
        )
        == 0
    )
    assert repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card") is None


def test_a_sticky_block_recovers_after_the_operator_ignores_it():
    """Không có vòng lặp chỉ toàn ``repeat_blocked``: sau N lượt bị chặn, mở chặn để đi tiếp vào gate."""
    node = _node()
    fp = _fp()
    for _ in range(REPEAT_BLOCK_AFTER - 1):
        note_action_outcome(
            node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="Tap Dled card"
        )

    for _ in range(REPEAT_BLOCK_RECOVERY_TURNS):
        assert repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card")
        note_blocked_attempt(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card")

    # Hết ngân sách chặn ⇒ mở để hành động đi tiếp qua safety gate (gate vẫn có thể từ chối).
    assert repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card") is None
    request = node.ctx.repeat_observation_request
    assert "Chặn đã được MỞ" in request and "safety gate" in request


def test_an_unknown_ui_fingerprint_recovers_earlier():
    """UI fingerprint không xác định được ⇒ phục hồi sớm hơn, không giữ chặn mà không có căn cứ."""
    node = _node()
    fp = _fp(ui=None)
    for _ in range(REPEAT_BLOCK_AFTER - 1):
        note_action_outcome(node, fp, outcome=OUTCOME_FAILED, ui_fingerprint=None, subgoal="s")

    assert repeat_guard_verdict(node, fp, ui_fingerprint=None, subgoal="s")
    note_blocked_attempt(node, fp, ui_fingerprint=None, subgoal="s")

    assert repeat_guard_verdict(node, fp, ui_fingerprint=None, subgoal="s") is None
    assert "không xác định được UI fingerprint" in node.ctx.repeat_observation_request


def test_three_failures_block_the_action_sticky():
    node = _node()
    fp = _fp()
    for _ in range(REPEAT_BLOCK_AFTER):
        note_action_outcome(
            node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="Tap Dled card"
        )

    reason = repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card")
    assert reason and "KHÔNG chạy lại" in reason
    # sticky: lần sau vẫn chặn (không phụ thuộc "N lần liên tiếp" như guard cũ)
    assert repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card")


def test_block_lifts_when_ui_target_or_subgoal_changes():
    node = _node()
    fp = _fp()
    for _ in range(REPEAT_BLOCK_AFTER):
        note_action_outcome(node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="Tap Dled card")

    assert repeat_guard_verdict(node, fp, ui_fingerprint="ui-b", subgoal="Tap Dled card") is None
    assert repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Verify on Home") is None
    other_target = _fp({"action": "tap", "coordinates": [360, 637], "target_text": "Dled"})
    assert repeat_guard_verdict(node, other_target, ui_fingerprint="ui-a", subgoal="s") is None


def test_the_guard_only_blocks_and_never_allows_through_the_safety_gate():
    """Bất biến: guard chạy TRƯỚC safety gate và chỉ có hai kết cục — chặn, hoặc để gate tự quyết."""
    from artemis.agents.validator import execution_loop

    source = inspect.getsource(execution_loop._process_action)
    guard_at = source.index("repeat_guard_verdict(")
    gate_at = source.index("_run_precondition_gate(")
    assert guard_at < gate_at, "guard phải chạy trước safety gate"
    blocked = source[guard_at:gate_at]
    assert "return outcome" in blocked, "chặn thì phải return ngay, không đi tiếp vào gate"


def test_repeat_blocked_is_not_a_failure_in_the_loop_and_in_metrics():
    from artemis.agents.validator import execution_loop
    from artemis.utils.step_metrics import StepMetrics, finish_execution

    source = inspect.getsource(execution_loop.run_validation_loop)
    branch = source.split("if outcome.repeat_blocked:", 1)
    assert len(branch) == 2 and "repeat_blocked = True" in branch[1][:400]

    metrics = StepMetrics()
    holder = SimpleNamespace(step_metrics=metrics, current_step_number=1)
    finish_execution(
        SimpleNamespace(data_engine=holder),
        dispatched=0,
        already_applied=0,
        failed=0,
        repeat_blocked=1,
    )
    assert metrics.actions_repeat_blocked == 1
    assert metrics.actions_failed == 0
    assert metrics.action == "repeat_blocked" and metrics.no_progress is True


def test_active_subgoal_text_reads_the_open_leaf():
    plan = "- [/] Switch house\n  - [/] Tap Dled card\n  - [ ] Verify on Home\n"
    state = SimpleNamespace(task_plan=plan)

    assert active_subgoal_text(state) == "Tap Dled card"
    assert active_subgoal_text(SimpleNamespace(task_plan="")) == ""


def test_ui_fingerprint_is_a_hash_or_none():
    assert action_ui_fingerprint(None) is None
    assert action_ui_fingerprint("not-base64!!") is None



def test_a_blocked_turn_opens_no_incident_and_no_action_failed():
    """Yêu cầu rà soát: khi bị chặn KHÔNG mở incident và KHÔNG ghi `Action failed`."""
    from artemis.agents.validator import execution_loop

    loop_source = inspect.getsource(execution_loop.run_validation_loop)
    tail = loop_source.split("if outcome.repeat_blocked:", 1)[1]
    branch = tail.split("if not success:", 1)[0]  # chỉ thân nhánh chặn
    assert "_build_incident" not in branch, "nhánh chặn không được mở incident"
    assert "Action failed" not in branch, "nhánh chặn không được ghi Action failed"

    process_source = inspect.getsource(execution_loop._process_action)
    assert "and not outcome.repeat_blocked" in process_source, (
        "lượt bị chặn không được chụp ảnh thất bại"
    )


def test_blocked_turns_do_not_loop_forever():
    """Bất biến: mỗi fingerprint chỉ bị chặn tối đa REPEAT_BLOCK_RECOVERY_TURNS lượt liên tiếp."""
    node = _node()
    fp = _fp()
    for _ in range(REPEAT_BLOCK_AFTER - 1):
        note_action_outcome(
            node, fp, outcome=OUTCOME_FAILED, ui_fingerprint="ui-a", subgoal="Tap Dled card"
        )

    blocked_turns = 0
    for _ in range(10):  # agent cố lặp 10 lần
        if repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card"):
            blocked_turns += 1
            note_blocked_attempt(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card")
        else:
            break  # đã mở chặn ⇒ hành động đi tiếp vào safety gate

    assert blocked_turns == REPEAT_BLOCK_RECOVERY_TURNS


# ── PHASE 2 (fix): guard cho SWIPE theo chu kỳ khứ hồi ────────────────────────────────────
#: Đúng ca đo được ở session d94158f5: cuộn lên–xuống xen kẽ 6 lần tại [432,1046] ↔ [432,489].
SWIPE_DOWN = {"action": "swipe", "coordinates": [432, 1046, 432, 489]}
SWIPE_UP = {"action": "swipe", "coordinates": [432, 489, 432, 1046]}
SWIPE_DOWN_2 = {"action": "swipe", "coordinates": [432, 900, 432, 489]}


def test_swipe_cycle_key_ignores_direction():
    assert swipe_cycle_key(SWIPE_DOWN) == swipe_cycle_key(SWIPE_UP)
    assert swipe_cycle_key(SWIPE_DOWN) != swipe_cycle_key(SWIPE_DOWN_2)
    assert swipe_cycle_key({"action": "swipe"}) is None
    assert swipe_direction(SWIPE_DOWN) == "up"  # ngón tay đi lên
    assert swipe_direction(SWIPE_UP) == "down"


def test_alternating_scroll_is_flagged_then_blocked():
    node = _node()
    key = swipe_cycle_key(SWIPE_DOWN)

    # 1 chu kỳ dở (xuống rồi lên) ⇒ yêu cầu quan sát lại, chưa chặn.
    note_swipe_cycle(node, key, "up", ui_fingerprint="ui-a", subgoal="Cuộn tìm Đăng xuất", progress=False)
    assert (
        note_swipe_cycle(
            node, key, "down", ui_fingerprint="ui-a", subgoal="Cuộn tìm Đăng xuất", progress=False
        )
        == REPEAT_CYCLE_OBSERVE_AFTER
    )
    # Ở 1 lần đảo chiều: **chưa chặn** nhưng phải gửi yêu cầu quan sát lại.
    assert swipe_cycle_verdict(node, key, ui_fingerprint="ui-a", subgoal="Cuộn tìm Đăng xuất") is None
    assert "cuộn lên" in node.ctx.repeat_observation_request.lower()

    # 2 chu kỳ ⇒ chặn.
    note_swipe_cycle(node, key, "up", ui_fingerprint="ui-a", subgoal="Cuộn tìm Đăng xuất", progress=False)
    note_swipe_cycle(node, key, "down", ui_fingerprint="ui-a", subgoal="Cuộn tìm Đăng xuất", progress=False)
    reason = swipe_cycle_verdict(node, key, ui_fingerprint="ui-a", subgoal="Cuộn tìm Đăng xuất")
    assert reason and "KHÔNG cuộn qua lại" in reason


def test_scrolling_in_one_direction_is_never_blocked():
    node = _node()
    key = swipe_cycle_key(SWIPE_DOWN)

    for _ in range(6):
        note_swipe_cycle(
            node, key, "up", ui_fingerprint="ui-a", subgoal="Cuộn tìm mục ở dưới", progress=False
        )

    assert swipe_cycle_verdict(node, key, ui_fingerprint="ui-a", subgoal="Cuộn tìm mục ở dưới") is None


def test_progress_and_context_change_reset_the_cycle():
    node = _node()
    key = swipe_cycle_key(SWIPE_DOWN)
    note_swipe_cycle(node, key, "up", ui_fingerprint="ui-a", subgoal="s", progress=False)
    note_swipe_cycle(node, key, "down", ui_fingerprint="ui-a", subgoal="s", progress=False)

    # UI đổi (màn hình khác) ⇒ chu kỳ bắt đầu lại.
    assert note_swipe_cycle(node, key, "up", ui_fingerprint="ui-b", subgoal="s", progress=False) == 0
    # Tiến triển thật ⇒ xoá hẳn.
    note_swipe_cycle(node, key, "up", ui_fingerprint="ui-a", subgoal="s", progress=False)
    assert note_swipe_cycle(node, key, "down", ui_fingerprint="ui-a", subgoal="s", progress=True) == 0
    assert swipe_cycle_verdict(node, key, ui_fingerprint="ui-a", subgoal="s") is None


def test_the_cycle_block_is_classified_as_repeat_blocked_not_failed():
    from artemis.agents.validator import execution_loop

    source = inspect.getsource(execution_loop._process_action)
    cycle_at = source.index("swipe_cycle_verdict(")
    gate_at = source.index("_run_precondition_gate(")
    assert cycle_at < gate_at, "guard chu kỳ phải chạy trước safety gate"
    branch = source[cycle_at:gate_at]
    assert "outcome.repeat_blocked = True" in branch and "return outcome" in branch
    assert "outcome.intercepted = True" not in branch


def test_the_step_ui_hash_is_recorded_for_the_watchdog():
    from artemis.agents.validator import execution_loop

    node = _node()
    node.ctx.data_engine = SimpleNamespace(get_step_number=lambda _sid: 3)
    state = SimpleNamespace(
        task_plan="- [/] A\n", current_step_id="11111111-2222-3333-4444-555555555555"
    )

    execution_loop._note_step_ui_hash(node, state, "hash-3")

    assert node.ctx.ui_hash_by_step == {3: "hash-3"}
