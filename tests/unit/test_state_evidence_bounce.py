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

"""Bằng chứng cho giá trị trạng thái: code kiểm trước, LLM kết luận sau.

Ca thật (28/10/2026, câu *"đăng nhập tài khoản 0999 chuyển nhà sang nhà 'Mesh Tạo 2' …"*): agent
mở đúng màn Menu — màn hình ghi ``Hoà RD · 0972842548`` — rồi viết *"We are logged in as account
0999 (verified from previous steps or profile details)"* và đi tiếp sang Quản lý nhà của tài
khoản SAI thay vì đăng xuất/đăng nhập lại.  Prompt đã yêu cầu so giá trị, nhưng khi chỉ có LLM
tự kiểm thì nó "xác minh" bằng giá trị nằm trong câu yêu cầu.

Ở đây: giá trị được chốt **phải từng xuất hiện** trong màn hình đã quan sát; không thì lượt đó
bị chặn kèm bằng chứng thật đang thấy.  Bốn tính chất được khoá:

1. Đọc đúng cặp (nhãn, giá trị) từ chính câu guard của composer.
2. Định danh lấy từ màn hình (số điện thoại/email), không lấy từ câu yêu cầu.
3. Khớp được cả khi màn hình hiển thị kèm mã vùng (``+84 0999``).
4. Chỉ chặn một lần cho mỗi giá trị, và không chặn khi đã có bằng chứng.
"""

from artemis.agents.operator.prompts import (
    STATE_EVIDENCE_MARKER,
    observed_identifiers,
    render_state_evidence,
    render_state_evidence_bounce,
    required_state_values,
    value_was_observed,
)

#: Đúng câu composer sinh ra cho trạng thái "tài khoản đang đăng nhập".
GOAL = (
    "đăng nhập tài khoản 0999 rồi vào màn ngữ cảnh\n\n"
    "KIỂM TRA TRẠNG THÁI (xác minh trên UI, KHÔNG mặc định là đúng):\n"
    "- «đăng nhập tài khoản 0999 — chỉ khi chưa đăng nhập bằng tài khoản 0999»: điều kiện"
    " «chỉ khi chưa đăng nhập bằng tài khoản 0999» chỉ được coi là ĐÃ ĐÚNG khi quan sát thấy"
    " tài khoản đang đăng nhập LÀ «0999». Đang là giá trị KHÁC thì điều kiện CHƯA đúng.\n"
    "- Cách quan sát tài khoản đang đăng nhập (theo KB auth.current_account) — làm lần lượt 2 bước:\n"
    "- Bước 1: tap: Tab Menu ở ngoài cùng bên phải của thanh điều hướng dưới cùng\n"
)

#: Danh sách phần tử UI của màn Menu (tài khoản thật đang đăng nhập là 0972842548).
MENU_ELEMENTS = (
    "[1] Hoà RD\n[2] 0972842548\n[3] Thành viên thường\n"
    "[35] Quản lý nhà\n[36] Quản lý thiết bị chia sẻ\n"
)

#: Plan đúng hình dạng của lần chạy hỏng: agent tự chốt là đã xác minh tài khoản 0999.
PLAN = (
    "- [x] Verify and switch account to 0999 if not already logged in\n"
    '    - [x] Select account "0999" from the account list and log in\n'
    '- [/] Switch active house to "Mesh Tạo 2"\n'
    "    - [x] Tap the house in the list to switch active house\n"
)


#: Prompt do tester tự viết (không có câu guard nào của composer) — đúng dạng đã gây lỗi 29/10.
HAND_WRITTEN_GOAL = (
    'đăng nhập tài khoản 0999 chuyển nhà sang nhà "Mesh Tạo 2" rồi vào màn ngữ cảnh\n\n'
    "KẾ HOẠCH:\n"
    "1. đăng nhập tài khoản 0999 — chỉ khi chưa đăng nhập bằng tài khoản 0999\n"
    '2. chuyển nhà nhà "Mesh Tạo 2" — chỉ khi nhà hiện tại khác nhà "Mesh Tạo 2"\n'
    "3. tạo ngữ cảnh chạy khi ấn có hành động là công tắc ổ cắm 12\n"
    "TIÊU CHÍ XÁC MINH:\n- Mục tiêu cuối: ngữ cảnh chạy khi ấn đã được tạo\n"
)


def test_required_values_come_from_the_composer_guard_line():
    assert required_state_values(GOAL) == (("tài khoản đang đăng nhập", "0999"),)
    assert required_state_values("không có guard nào ở đây") == ()


def test_values_are_read_from_a_hand_written_prompt_too():
    """Prompt tester tự viết không có câu guard ⇒ vẫn phải lấy được định danh cần xác minh."""
    values = required_state_values(HAND_WRITTEN_GOAL)

    assert ("giá trị mà yêu cầu nêu", "0999") in values
    # "12" trong tên thiết bị "công tắc ổ cắm 12" KHÔNG phải định danh.
    assert all(value != "12" for _, value in values)


def test_a_hand_written_prompt_still_gets_the_evidence_check():
    plan = "- [x] Verify current account and switch account to 0999 (auth.switch_account)\n"

    message, blocked = render_state_evidence_bounce(
        plan, HAND_WRITTEN_GOAL, observed_identifiers(MENU_ELEMENTS)
    )

    assert blocked == "0999"
    assert "0972842548" in message


def test_observed_identifiers_come_from_the_screen():
    assert observed_identifiers(MENU_ELEMENTS) == {"0972842548"}
    # Màn hình đăng nhập hiển thị kèm mã vùng, và số tài khoản ngắn cũng là định danh đã thấy.
    assert observed_identifiers("[7] +84 0999\n[8] ••••") == {"840999", "0999"}


def test_a_short_account_number_is_observable_so_the_gate_can_verify_it():
    """Hai phía phải **đối xứng**: ``required_state_values`` coi ``0999`` (4 chữ số) là giá trị bắt
    buộc, nên phía bằng chứng cũng phải quan sát được nó — nếu không thì không run nào xác minh được
    tài khoản, và chốt bằng chứng chỉ còn biết kêu.
    """
    from artemis.agents.operator.prompts import required_state_values

    assert ("Tài khoản đang đăng nhập", "0999") in required_state_values(GOAL) or any(
        value == "0999" for _label, value in required_state_values(GOAL)
    )
    assert value_was_observed("0999", observed_identifiers("Trang chủ · Tài khoản 0999"))
    # Số ngắn trong ngữ cảnh khác (dung lượng, nhiệt độ) cũng được ghi nhận — chấp nhận đánh đổi:
    # thà ghi nhận thừa một định danh còn hơn không bao giờ xác minh được giá trị bắt buộc.
    assert "1234" in observed_identifiers("1234 kWh")


def test_a_value_with_country_code_still_counts_as_seen():
    assert value_was_observed("0999", {"840999"})
    assert value_was_observed("0972842548", {"840972842548"})
    assert not value_was_observed("0999", {"0972842548"})
    assert not value_was_observed("0999", set())


def test_a_claimed_value_never_seen_on_screen_bounces_the_turn():
    message, blocked = render_state_evidence_bounce(PLAN, GOAL, observed_identifiers(MENU_ELEMENTS))

    assert blocked == "0999"
    assert "0999" in message
    # Bằng chứng thật đang thấy phải nằm trong thông báo để agent tự sửa.
    assert "0972842548" in message
    assert "CHƯA từng xuất hiện" in message


def test_a_claim_backed_by_the_screen_passes():
    observed = observed_identifiers(MENU_ELEMENTS) | {"840999"}

    assert render_state_evidence_bounce(PLAN, GOAL, observed) == (None, None)


def test_a_blocked_value_is_not_blocked_again():
    observed = observed_identifiers(MENU_ELEMENTS)

    assert render_state_evidence_bounce(PLAN, GOAL, observed, already_bounced={"0999"}) == (
        None,
        None,
    )


def test_a_non_identifier_value_is_left_to_the_llm():
    """Tên nhà/tên ngữ cảnh không phải định danh máy đọc được ⇒ code không chặn (tránh chặn nhầm)."""
    goal = (
        "- điều kiện «chỉ khi nhà hiện tại khác nhà \"Mesh Tạo 2\"» chỉ được coi là ĐÃ ĐÚNG khi"
        ' quan sát thấy nhà đang chọn LÀ «Mesh Tạo 2».\n'
    )
    plan = (
        '- [x] Switch active house to "Mesh Tạo 2"\n'
        '    - [x] Tap "Mesh Tạo 2" in the house list\n'
    )

    assert render_state_evidence_bounce(plan, goal, observed_identifiers(MENU_ELEMENTS)) == (
        None,
        None,
    )


def test_the_evidence_block_tells_the_truth_about_what_was_seen():
    """Khối mỗi lượt: nói thẳng giá trị nào đã/chưa thấy, và cách đạt giá trị còn thiếu."""
    block = render_state_evidence(HAND_WRITTEN_GOAL, observed_identifiers(MENU_ELEMENTS))

    assert block.startswith(STATE_EVIDENCE_MARKER)
    assert "«0999»" in block
    assert "CHƯA thấy" in block
    assert "0972842548" in block
    assert "auth.logout" in block
    assert "auth.switch_account" in block


def test_the_evidence_block_confirms_a_value_that_was_seen():
    block = render_state_evidence(
        HAND_WRITTEN_GOAL, observed_identifiers(MENU_ELEMENTS) | {"840999"}
    )

    assert "ĐÃ thấy" in block
    # Không thiếu gì thì không nhắc cách đổi tài khoản.
    assert "auth.logout" not in block


def test_the_evidence_block_is_empty_without_a_value_to_verify():
    assert render_state_evidence("mở app và bấm nút bất kỳ", set()) == ""


def test_nothing_to_check_without_a_guard_or_a_completed_item():
    observed = observed_identifiers(MENU_ELEMENTS)

    assert render_state_evidence_bounce(PLAN, "không có guard", observed) == (None, None)
    pending_only = PLAN.replace("- [x] Verify and switch account to 0999 if not already logged in",
                               "- [ ] Verify and switch account to 0999 if not already logged in").replace(
        '    - [x] Select account "0999" from the account list and log in',
        '    - [ ] Select account "0999" from the account list and log in',
    )
    assert render_state_evidence_bounce(pending_only, GOAL, observed) == (None, None)
