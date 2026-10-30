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

"""Gộp khối milestone trùng trong note ``task_plan`` **tại nguồn ghi**.

Ca thật (session ``d110b1ef`` / task ``1371db04``): agent gọi ``update_note`` với ``target`` chỉ khớp
một phần kế hoạch nhưng ``replacement`` là cả kế hoạch ⇒ note có cùng 4 milestone lặp **4 lần**.
Hệ quả đo được: gate ledger bounce **10 lần**, checker khai **6 tiêu chí với 4 bản trùng**, prompt
phình. Bản sửa gộp **lúc ghi** (``save_note``/``update_note``) nên mọi đường đọc đều thấy một kế hoạch
duy nhất — không cần dedup lúc đọc.
"""

from pathlib import Path

from artemis.utils.notes import (
    PLAN_DEDUP_WARNING,
    collapse_duplicate_plan_blocks,
    save_note_content,
    update_note_content,
)

#: Đúng dạng kế hoạch bị nhân bản của session (rút gọn còn 3 bản để test đọc được).
BLOCK = (
    "  - [x] Verify current logged-in account via Menu tab and log in with account 0972842548\n"
    "    - [x] Tap Menu tab (bottom right, 3 horizontal bars) to view account info\n"
    "      - [x] Scroll down to find and tap Logout button at the bottom of the Menu screen\n"
    "      - [x] Tap account 0972842548 in the saved accounts list\n"
    "  - [x] Tap Login button to complete login\n"
    "  - verify: App is on main screen, login screen is no longer present\n"
    "  - [x] Verify current house via Home tab and switch to house Dled if different\n"
    "    - [ ] Tap Home tab (bottom left) to check current house name at top-left corner\n"
    "  - verify: Current house displayed at top-left of Home screen is Dled\n"
    "- assert@end: Current logged in account is 0972842548 and current selected house is Dled\n"
)
DUPLICATED = BLOCK + BLOCK + BLOCK


def test_the_duplicated_plan_from_the_session_is_collapsed():
    text, removed = collapse_duplicate_plan_blocks(DUPLICATED)

    # 3 bản × 3 milestone cấp cao nhất ⇒ 2 bản dư × 3 = 6 khối bị gộp.
    assert removed == 6
    assert text.count("Verify current logged-in account via Menu tab") == 1
    assert text.count("Verify current house via Home tab") == 1
    # Không mất tiêu chí kiểm nào.
    assert text.count("verify: App is on main screen") == 1
    assert text.count("verify: Current house displayed at top-left") == 1
    assert text.count("assert@end: Current logged in account") == 1


def test_states_are_merged_to_the_furthest_and_progress_is_not_lost():
    first = "- [ ] Milestone A\n  - [ ] Child A1\n"
    second = "- [x] Milestone A\n  - [/] Child A1\n"
    text, removed = collapse_duplicate_plan_blocks(first + second)

    assert removed == 1
    assert "- [x] Milestone A" in text  # x > ' ' ⇒ giữ tiến độ xa nhất
    assert "- [/] Child A1" in text  # '/ ' > ' ' cho dòng con


def test_distinct_milestones_are_left_alone():
    plan = "- [x] Milestone A\n  - [x] Child A1\n- [ ] Milestone B\n- [ ] Milestone C\n"

    text, removed = collapse_duplicate_plan_blocks(plan)

    assert removed == 0
    assert text == plan


def test_milestones_with_similar_but_different_purposes_are_not_merged():
    """Bất biến: chỉ gộp khi **giống nhau sau khi bỏ trạng thái** — khác mục đích thì giữ cả hai."""
    plan = (
        "- [x] Chuyển sang nhà Dled\n"
        "- [ ] Chuyển sang nhà Dled (phòng khách)\n"
        "- [ ] Chuyển sang nhà khác\n"
        "- [x] Chuyển sang nhà Dled\n"  # chỉ dòng này mới là bản trùng của dòng 1
    )

    text, removed = collapse_duplicate_plan_blocks(plan)

    assert removed == 1
    assert text.count("Chuyển sang nhà Dled\n") == 1
    assert "Chuyển sang nhà Dled (phòng khách)" in text
    assert "Chuyển sang nhà khác" in text


def test_a_plan_without_duplicates_is_byte_identical():
    plan = "- [x] A\n  - verify: done\n- [ ] B\n"

    assert collapse_duplicate_plan_blocks(plan) == (plan, 0)


def test_update_note_with_a_fragment_target_and_a_full_plan_does_not_duplicate(tmp_path: Path):
    """Đúng cơ chế sinh ra lỗi: target khớp một phần, replacement là cả kế hoạch."""
    save_note_content(tmp_path, "task_plan", BLOCK)
    # Lần sửa chỉ nhắm một dòng con nhưng dán lại toàn bộ kế hoạch.
    warning = update_note_content(
        tmp_path,
        "task_plan",
        "  - [x] Tap Login button to complete login\n",
        BLOCK,
    )

    written = (tmp_path / "notes" / "task_plan.md").read_text(encoding="utf-8")
    assert written.count("Verify current logged-in account via Menu tab") == 1
    assert written.count("Verify current house via Home tab") == 1
    assert written.count("Tap Login button to complete login") == 1
    # Agent được báo vì sao note bị gộp, để lần sau đọc lại trước khi sửa.
    assert warning and "đã được gộp tại chỗ" in warning
    assert "verify: Current house displayed at top-left" in written


def test_save_note_collapses_duplicates_for_task_plan(tmp_path: Path):
    save_note_content(tmp_path, "task_plan", DUPLICATED)

    written = (tmp_path / "notes" / "task_plan.md").read_text(encoding="utf-8")
    assert written.count("Verify current house via Home tab") == 1


def test_other_notes_are_never_rewritten(tmp_path: Path):
    """Chỉ note kế hoạch mới bị gộp; note thường giữ nguyên từng ký tự."""
    content = "line A\nline A\nline A\n"
    save_note_content(tmp_path, "scratch", content)

    assert (tmp_path / "notes" / "scratch.md").read_text(encoding="utf-8") == content
