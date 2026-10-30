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
    REPEAT_BLOCK_AFTER,
    REPEAT_OBSERVE_AFTER,
    action_ui_fingerprint,
    active_subgoal_text,
    note_action_outcome,
    repeat_fingerprint,
    repeat_guard_verdict,
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

    assert note_action_outcome(node, fp, failed=True, ui_fingerprint="ui-a", subgoal="s") == 1
    assert note_action_outcome(node, fp, failed=True, ui_fingerprint="ui-a", subgoal="s") == 2
    # Thành công/không phải thất bại (already_applied, gate chặn, không dispatch) ⇒ xoá bộ đếm.
    assert note_action_outcome(node, fp, failed=False, ui_fingerprint="ui-a", subgoal="s") == 0
    assert note_action_outcome(node, fp, failed=True, ui_fingerprint="ui-a", subgoal="s") == 1


def test_a_changed_screen_restarts_the_count():
    node = _node()
    fp = _fp(ui="ui-a")

    note_action_outcome(node, fp, failed=True, ui_fingerprint="ui-a", subgoal="s")
    note_action_outcome(node, fp, failed=True, ui_fingerprint="ui-a", subgoal="s")
    # Cùng fingerprint nhưng UI đã đổi ⇒ đếm lại từ 1 (không chặn oan).
    assert note_action_outcome(node, fp, failed=True, ui_fingerprint="ui-b", subgoal="s") == 1


def test_two_failures_request_re_observation_without_blocking():
    node = _node()
    fp = _fp()

    for _ in range(REPEAT_OBSERVE_AFTER):
        note_action_outcome(node, fp, failed=True, ui_fingerprint="ui-a", subgoal="Tap Dled card")

    assert repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card") is None
    request = node.ctx.repeat_observation_request
    assert "thất bại thật" in request and "KHÔNG tap lại cùng toạ độ" in request


def test_three_failures_block_the_action_sticky():
    node = _node()
    fp = _fp()
    for _ in range(REPEAT_BLOCK_AFTER):
        note_action_outcome(node, fp, failed=True, ui_fingerprint="ui-a", subgoal="Tap Dled card")

    reason = repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card")
    assert reason and "KHÔNG chạy lại" in reason
    # sticky: lần sau vẫn chặn (không phụ thuộc "N lần liên tiếp" như guard cũ)
    assert repeat_guard_verdict(node, fp, ui_fingerprint="ui-a", subgoal="Tap Dled card")


def test_block_lifts_when_ui_target_or_subgoal_changes():
    node = _node()
    fp = _fp()
    for _ in range(REPEAT_BLOCK_AFTER):
        note_action_outcome(node, fp, failed=True, ui_fingerprint="ui-a", subgoal="Tap Dled card")

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

