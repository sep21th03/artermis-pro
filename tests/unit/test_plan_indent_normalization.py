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

"""Chuẩn hoá thụt lề của ``task_plan`` trước khi xác định top-level.

Ca thật 29/10/2026 (``task_2910_1428``): model viết **cả** kế hoạch trong một khối thụt 2 space ⇒
``parse_plan().has_top_level == False`` ⇒ ``convergence_gate`` kết thúc run ngay sau khi agent mở
bảng chọn thiết bị, final check chốt ``blocked`` dù ngữ cảnh chưa được lưu. 3/8 plan gần nhất ở
``traces/*/notes/task_plan.md`` ở tình trạng này.
"""

from __future__ import annotations

from pathlib import Path

from artemis.utils.execution_state import build_execution_state
from artemis.utils.plan_grammar import parse_plan

#: Nguyên văn plan của session hỏng (rút gọn phần đuôi, giữ đúng cấu trúc thụt lề).
INDENTED_PLAN = (
    "  - [/] Vào màn ngữ cảnh từ màn hình quản lý nhà hiện tại\n"
    "    - [x] Tap back button from Quản lý nhà to main home screen\n"
    "    - [x] Tap Tab Ngữ cảnh trên thanh điều hướng dưới cùng\n"
    "    - [/] Tap add action (Thêm hành động)\n"
    "      - [x] Tap add button to open action bottom sheet\n"
    '      - [/] Select a switch device and choose action "Bật" (Turn on)\n'
    '        - [/] Select "Công tắc 10" from device list\n'
    '  - [ ] Đặt tên là "Ngữ cảnh bật" rồi lưu lại\n'
    '    - [ ] Input text "Ngữ cảnh bật" into scene name field\n'
    "  - [ ] Kích hoạt Ngữ cảnh bật\n"
    "    - [ ] Tap run/play icon on the right\n"
    "- assert@end: Ngữ cảnh bật đã được kích hoạt thành công trên UI\n"
)


def test_an_indented_plan_still_yields_its_top_level_milestones():
    snapshot = parse_plan(INDENTED_PLAN)

    assert snapshot.has_top_level is True
    assert len(snapshot.top_level) == 3
    assert [item.text for item in snapshot.top_level] == [
        "Vào màn ngữ cảnh từ màn hình quản lý nhà hiện tại",
        'Đặt tên là "Ngữ cảnh bật" rồi lưu lại',
        "Kích hoạt Ngữ cảnh bật",
    ]


def test_relative_depth_and_parent_child_relations_survive():
    snapshot = parse_plan(INDENTED_PLAN)
    milestone = snapshot.top_level[0]
    children = snapshot.children_of(milestone)

    # Độ sâu tương đối giữ nguyên (đã trừ 2 của mức chung); dòng `assert@end` ở cột 0 là dòng kiểm
    # chung, không phải item con.
    assert [child.indent for child in children] == [2, 2, 2, 4, 4, 6]
    assert 'Select "Công tắc 10" from device list' in [child.text for child in children]
    assert snapshot.parent_of(children[0]).text == milestone.text
    assert snapshot.check_items, "dòng assert@end phải được parse thành check item"


def test_content_and_checkbox_status_are_not_rewritten():
    snapshot = parse_plan(INDENTED_PLAN)
    statuses = [(item.text, item.status) for item in snapshot.items]

    assert ("Tap back button from Quản lý nhà to main home screen", "x") in statuses
    assert ("Tap add action (Thêm hành động)", "/") in statuses
    assert ("Kích hoạt Ngữ cảnh bật", " ") in statuses
    assert len(snapshot.items) == 11  # không mất/thêm dòng nào


def test_a_plan_at_column_zero_is_untouched():
    flush = "- [ ] A\n  - [ ] A.1\n- [ ] B\n"
    snapshot = parse_plan(flush)

    assert [item.text for item in snapshot.top_level] == ["A", "B"]
    assert snapshot.children_of(snapshot.top_level[0])[0].indent == 2


def test_a_uniformly_deep_indent_is_normalised_too():
    deep = "    - [ ] M1\n        - [ ] M1.1\n    - [ ] M2\n"
    snapshot = parse_plan(deep)

    assert [item.text for item in snapshot.top_level] == ["M1", "M2"]
    assert snapshot.children_of(snapshot.top_level[0])[0].indent == 4


def test_a_mixed_indent_plan_is_not_shifted():
    """Đã có dòng ở cột 0 thì mức nhỏ nhất là 0 ⇒ không chuẩn hoá gì (không làm hỏng plan đúng)."""
    mixed = "- [ ] M1\n  - [ ] M1.1\n    - [ ] M1.1.1\n- [ ] M2\n"
    snapshot = parse_plan(mixed)

    assert [item.text for item in snapshot.top_level] == ["M1", "M2"]
    assert [c.indent for c in snapshot.children_of(snapshot.top_level[0])] == [2, 4]


def test_execution_state_gets_subtasks_from_an_indented_plan():
    """Hệ quả trực tiếp: trước khi sửa, plan thụt lề cho 0 subtask ⇒ không thể truy xuất KB theo
    công đoạn (đó là lý do B1 là điều kiện tiên quyết của B3)."""
    state = build_execution_state(INDENTED_PLAN, "tạo ngữ cảnh cho công tắc", "")

    assert len(state.subtasks) == 3
    assert state.active() is not None
    assert "Vào màn ngữ cảnh" in state.active().text


def test_real_plans_in_traces_keep_their_milestones():
    """Chạy trên plan thật nếu có: không plan nào được phép mất milestone vì thụt lề.

    Bỏ qua khi máy không có ``traces/`` (CI sạch) — nhưng khi có thì đây là bằng chứng mạnh nhất.
    """
    plans = sorted(Path("traces").glob("*/notes/task_plan.md"))
    if not plans:
        return
    recovered: list[str] = []
    for path in plans:
        snapshot = parse_plan(path.read_text(encoding="utf-8"))
        assert snapshot.has_top_level or not snapshot.items, f"{path} vẫn không có milestone"
        if len(snapshot.top_level) >= 1:
            recovered.append(f"{path.parent.parent.name[:8]}:{len(snapshot.top_level)}")
    assert recovered
