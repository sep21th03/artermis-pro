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

"""Gate ledger: tự mở ``[/]`` khi duy nhất một ứng viên, và không bounce lặp cùng lý do.

Ca thật (session ``d110b1ef`` / task ``1371db04``): kế hoạch bị nhân bản và mất dòng ``[/]`` ⇒ gate
**bounce 10 lần**, mỗi lần một lượt LLM vô ích. Yêu cầu:

* chỉ tự phục hồi khi **xác định duy nhất** sub-goal hợp lệ (kiểm cả ExecutionState);
* mơ hồ ⇒ giữ gate và báo lý do;
* cùng một lý do mà kế hoạch không đổi ⇒ **không** bounce lại.
"""

from artemis.utils.plan_grammar import (
    plan_digest,
    plan_self_heal_candidate,
    repeat_bounce_suppressed,
)


def test_single_open_milestone_is_opened_automatically():
    plan = (
        "- [x] Vào màn ngữ cảnh\n"
        "  - [x] Tap Tab Ngữ cảnh\n"
        "- [ ] Chuyển sang nhà Dled\n"
        "  - [ ] Mở Quản lý nhà\n"
        "- verify: Current house is Dled\n"
    )

    healed, reason = plan_self_heal_candidate(plan)

    assert "- [/] Chuyển sang nhà Dled" in healed
    assert "- [/] Mở Quản lý nhà" in healed  # dòng con duy nhất chưa xong cũng được mở
    assert "- [x] Vào màn ngữ cảnh" in healed  # tiến độ cũ giữ nguyên
    assert "verify: Current house is Dled" in healed  # tiêu chí kiểm giữ nguyên
    assert "milestone" in reason and "sub-goal" in reason


def test_two_open_milestones_are_ambiguous_and_left_alone():
    plan = "- [x] A\n- [ ] B\n- [ ] C\n"

    assert plan_self_heal_candidate(plan) is None


def test_two_open_children_are_ambiguous_unless_execution_state_points_at_one():
    plan = "- [/] Chuyển nhà\n  - [ ] Mở Quản lý nhà\n  - [ ] Chọn Dled\n"

    assert plan_self_heal_candidate(plan) is None
    healed = plan_self_heal_candidate(plan, prefer="Chọn Dled")
    assert healed is not None
    assert "- [/] Chọn Dled" in healed[0]
    assert "- [ ] Mở Quản lý nhà" in healed[0]  # dòng kia không bị đụng


def test_nothing_to_heal_when_the_ledger_is_already_open():
    plan = "- [/] A\n  - [/] A1\n"

    assert plan_self_heal_candidate(plan) is None


def test_plan_without_any_checkbox_is_left_to_the_gate():
    assert plan_self_heal_candidate("") is None
    assert plan_self_heal_candidate("- verify: chỉ có dòng kiểm, không có checkbox\n") is None


def test_an_indented_only_plan_counts_as_one_milestone():
    """Chuẩn hoá thụt lề (B1) biến kế hoạch chỉ có dòng con thành một milestone ⇒ mở được."""
    healed, _reason = plan_self_heal_candidate("  - [ ] chỉ có dòng con\n")

    assert healed is not None and "- [/] chỉ có dòng con" in healed


def test_healing_only_changes_the_status_character():
    plan = "- [ ] Chuyển sang nhà Dled\n  - [ ] Chọn Dled\n"
    healed, _reason = plan_self_heal_candidate(plan)

    assert healed.replace("[/]", "[ ]") == plan


def test_a_repeat_bounce_with_an_unchanged_plan_is_suppressed():
    plan = "- [ ] A\n- [ ] B\n"
    previous = ("Not executed: no milestone is marked in progress", plan_digest(plan))

    assert repeat_bounce_suppressed(previous, plan) is True
    # Kế hoạch đổi (có tiến triển) ⇒ được phép nhắc lại.
    assert repeat_bounce_suppressed(previous, plan + "- [x] C\n") is False
    # Chưa từng bounce ⇒ không chặn gì.
    assert repeat_bounce_suppressed(None, plan) is False
