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

"""Đếm và chốt công đoạn của plan — chỗ duy nhất tính tiến độ của một lượt chạy.

Ca thật (28/10/2026, câu *"đăng nhập tài khoản 0999 chuyển nhà sang nhà 'Mesh Tạo 2' rồi vào
màn ngữ cảnh…"*): sau khi chọn được nhà, plan là ``[/] Switch active house to "Mesh Tạo 2"``
với **cả hai** sub-goal đã ``[x]``, nhưng không có bộ phận nào *tính* công đoạn nên không ai
chốt nó — agent lặp lại tap/vuốt trên màn Quản lý nhà (step 25–30, cùng toạ độ) cho tới hết
ngân sách.  Ba tính chất được khoá ở đây:

1. Milestone có mọi sub-goal ``[x]`` ⇒ được chốt ``[x]`` bằng code (và chỉ dòng đó đổi).
2. Milestone không có sub-goal, hoặc vòng lặp, thì KHÔNG bị chốt tự động.
3. Agent luôn đọc được dòng tiến độ: còn mấy công đoạn, đang ở đâu, kế tiếp là gì.
"""

from artemis.utils.plan_grammar import (
    parse_plan,
    plan_with_progress,
    render_plan_progress,
    settle_completed_milestones,
)

#: Đúng hình dạng plan của lần chạy hỏng.
STUCK_PLAN = (
    "- [x] Verify and switch account to 0999 if not already logged in\n"
    "    - [x] Tap profile card to check account and switch if needed\n"
    "- [/] Switch active house to \"Mesh Tạo 2\"\n"
    "    - [x] Dismiss unwanted house switch prompt and navigate to House Management\n"
    "    - [x] Tap \"Mesh Tạo 2\" in the house list to switch active house\n"
    "- [ ] Navigate to the Scenes (Ngữ cảnh) screen\n"
    "    - [ ] Tap Scenes tab in bottom navigation bar\n"
    "- [ ] Create manual scene with action \"công tắc ổ cắm 12\"\n"
    "    - [ ] Tap create/add button in manual scene section\n"
)


def test_a_milestone_whose_sub_goals_are_all_done_is_settled():
    settled_text, settled, _ = settle_completed_milestones(STUCK_PLAN)

    assert settled == ('Switch active house to "Mesh Tạo 2"',)
    assert '- [x] Switch active house to "Mesh Tạo 2"' in settled_text
    assert "- [/]" not in settled_text
    # Mọi thứ khác giữ nguyên từng ký tự.
    assert settled_text == STUCK_PLAN.replace(
        '- [/] Switch active house to "Mesh Tạo 2"',
        '- [x] Switch active house to "Mesh Tạo 2"',
    )


def test_settling_is_idempotent():
    settled_text, _, _ = settle_completed_milestones(STUCK_PLAN)

    again_text, again_settled, _ = settle_completed_milestones(settled_text)

    assert again_settled == ()
    assert again_text == settled_text


def test_milestones_without_sub_goals_are_never_settled():
    plan = "- [/] Đang làm gì đó chưa ghi bước\n- [ ] Việc sau\n"

    settled_text, settled, _ = settle_completed_milestones(plan)

    assert settled == ()
    assert settled_text == plan


def test_a_loop_milestone_is_never_settled_automatically():
    """Giám sát chỉ kết thúc khi người dùng dừng — code không được tự chốt."""
    plan = (
        "- [/] Watch the battery level every minute [Loop:continuous]\n"
        "    - [x] Read the battery percentage\n"
    )

    settled_text, settled, _ = settle_completed_milestones(plan)

    assert settled == ()
    assert settled_text == plan


def test_progress_line_names_the_current_and_next_stage():
    text = render_plan_progress(STUCK_PLAN)

    assert "1/4 công đoạn đã xong" in text
    assert 'đang làm: «Switch active house to "Mesh Tạo 2"»' in text
    assert "chốt nó và đi tiếp" in text
    assert "kế tiếp: «Navigate to the Scenes (Ngữ cảnh) screen»" in text


def test_progress_line_is_absent_without_milestones():
    assert render_plan_progress("") == ""
    assert render_plan_progress("No task plan yet.") == ""
    assert plan_with_progress("No task plan yet.") == "No task plan yet."


def test_plan_with_progress_puts_the_line_above_the_checklist():
    combined = plan_with_progress(STUCK_PLAN)

    first_line, rest = combined.split("\n", 1)
    assert first_line.startswith("Progress:")
    assert rest == STUCK_PLAN


def test_settled_plan_shifts_the_progress_line_to_the_next_stage():
    settled_text, _, _ = settle_completed_milestones(STUCK_PLAN)

    text = render_plan_progress(settled_text)

    assert "2/4 công đoạn đã xong" in text
    # Sau khi chốt, không milestone nào đang mở ⇒ dòng tiến độ chỉ còn "kế tiếp".
    assert "kế tiếp: «Navigate to the Scenes (Ngữ cảnh) screen»" in text
    assert "chốt nó và đi tiếp" not in text
    # Milestone đã chốt không còn được đề nghị chốt lại.
    assert "Switch active house" not in text


def test_parsed_status_matches_the_rewritten_text():
    settled_text, _, _ = settle_completed_milestones(STUCK_PLAN)

    snapshot = parse_plan(settled_text)

    assert snapshot.top_level[1].is_done
    assert not snapshot.top_level[2].is_done
