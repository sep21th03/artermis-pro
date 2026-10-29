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

"""Bước KB của *công đoạn đang làm* phải nằm ngay lượt quan sát hiện tại.

Prompt do KB chuẩn bị dài 4.000+ ký tự và mục kiến thức nằm ở cuối: agent đọc nó **một lần** ở
đầu phiên rồi tới lượt thứ 20 thì không còn nhớ.  Đo trên máy thật 29/10/2026: prompt có
``auth.switch_account`` (mũi tên caret-down ở cuối ô số điện thoại, không phải gõ tay) mà agent
vẫn xoá số cũ rồi gõ ``0999`` vào ô tài khoản — sai mật khẩu.

Ở đây id workflow lấy từ **chính plan** (dòng "kiến thức: <id>" hoặc "<id>" trong tiêu đề công
đoạn), nội dung lấy từ mục kiến thức của prompt: không truy xuất KB lần nữa, không hard-code
workflow nào, và chỉ in công đoạn đang làm.
"""

from artemis.agents.operator.prompts import KB_FOCUS_MARKER, render_kb_focus

GOAL = (
    "đăng nhập tài khoản 0999 rồi vào màn ngữ cảnh\n\n"
    "KIẾN THỨC ỨNG DỤNG:\n"
    "- auth.switch_account — Đăng nhập bằng tài khoản đã lưu (Chuyển tài khoản) [verified]\n"
    "  Mô tả: Chọn một tài khoản đã từng đăng nhập trên máy này.\n"
    "  Các bước (tìm phần tử trên UI hiện tại, KHÔNG dùng toạ độ):\n"
    "    1. tap: Mũi tên caret-down ở CUỐI ô nhập số điện thoại\n"
    "    2. tap: Nút 'Đăng nhập'\n"
    "- home.switch_house — Chuyển sang nhà khác [draft]\n"
    "  Các bước (tìm phần tử trên UI hiện tại, KHÔNG dùng toạ độ):\n"
    "    1. tap: Mục Quản lý nhà\n"
)

LOGIN_PLAN = (
    "- [/] Đăng nhập tài khoản 0999 (auth.switch_account)\n"
    "    - [/] Chọn tài khoản đã lưu rồi bấm Đăng nhập\n"
)


def test_the_active_subgoal_kb_steps_are_surfaced():
    block = render_kb_focus(GOAL, LOGIN_PLAN)

    assert block.startswith(KB_FOCUS_MARKER)
    assert "auth.switch_account" in block
    assert "caret-down" in block
    assert "Công đoạn đang làm" in block
    # Chỉ công đoạn đang làm — không kéo cả mục kiến thức vào mỗi lượt.
    assert "Quản lý nhà" not in block


def test_the_block_follows_the_active_subgoal():
    house_plan = (
        '- [/] Switch house to "Mesh Tạo 2" (home.switch_house)\n'
        "    - [/] Tap the house in the list\n"
    )

    block = render_kb_focus(GOAL, house_plan)

    assert "home.switch_house" in block
    assert "Quản lý nhà" in block
    assert "caret-down" not in block


def test_no_block_when_nothing_identifies_a_workflow():
    assert render_kb_focus(GOAL, "- [/] Việc gì đó không nêu workflow\n") == ""
    assert render_kb_focus("prompt không có mục kiến thức", LOGIN_PLAN) == ""
    assert render_kb_focus(GOAL, "") == ""
