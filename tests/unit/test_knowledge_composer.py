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

"""Prompt Composer: ghép KB thành prompt thực thi, bằng luật.

Mười tính chất được khoá ở đây, mỗi cái đến từ một lỗi thật hoặc một yêu cầu vận hành:

1. Thông tin lặp ở nhiều chỗ trong KB chỉ hiện một lần.
2. Hai mô tả KB *mâu thuẫn* không bị nối thành câu vô nghĩa.
3. Workflow cần thiết có trong KB thì các bước của nó phải vào prompt.
4. Đăng nhập bằng tài khoản đã lưu (không mật khẩu) không được biến thành luồng gõ mật khẩu.
5. Điều kiện trước được nêu như *điều phải xác minh*, không phải sự thật mặc định.
6. Thiếu thông tin điều hướng ⇒ yêu cầu quan sát UI, **không** bịa vị trí nút.
7. Workflow phụ không liên quan không lọt vào các bước.
8. Nhiều kết quả trung gian tách khỏi một mục tiêu cuối.
9. KB thật sự thiếu ⇒ giữ trạng thái "chưa biết", không suy diễn quy trình.
10. Luật này không phụ thuộc ứng dụng: cùng hàm, id và nội dung của app khác vẫn chạy.
"""

from __future__ import annotations

from artemis.knowledge.composer import (
    CONFLICT_NOTE,
    SECTION_NOTES,
    SECTION_STOP,
    SECTION_STATE,
    SECTION_STEPS,
    SECTION_TASK,
    SECTION_VERIFY,
    compose_prompt,
    dedupe_sentences,
    render_steps,
)
from artemis.knowledge.intent import (
    ROLE_PRIMARY,
    ROLE_SUPPORTING,
    IntentPlan,
    RoleAssignment,
    TaskKnowledge,
    TaskSpec,
)
from artemis.knowledge.models import Workflow, WorkflowHit


def workflow(
    workflow_id: str,
    name: str,
    description: str = "",
    *,
    aliases=(),
    preconditions=(),
    success_criteria=(),
    warnings=(),
    steps=(),
) -> Workflow:
    return Workflow(
        id=workflow_id,
        app_id="demo",
        name=name,
        description=description,
        aliases=list(aliases),
        preconditions=list(preconditions),
        success_criteria=list(success_criteria),
        steps=[{"action": "tap", "target": target} for target in steps],
        metadata={"warnings": list(warnings)} if warnings else {},
        revision_no=1,
        updated_at="2026-10-27T10:00:00",
    )


def hit(workflow_object: Workflow, *, role: str = ROLE_PRIMARY, trusted: bool = True) -> WorkflowHit:
    return WorkflowHit(workflow=workflow_object, score=0.5, channels={"keyword": 1}, trusted=trusted)


def entry(
    action: str,
    *,
    primary: Workflow | None = None,
    supporting=(),
    target: str = "",
    condition: str = "",
    index: int = 0,
) -> TaskKnowledge:
    task = TaskSpec(action=action, target=target, condition=condition)
    roles: list[RoleAssignment] = []
    if primary is not None:
        roles.append(RoleAssignment(workflow_id=primary.id, role=ROLE_PRIMARY, reason="test"))
    roles.extend(
        RoleAssignment(workflow_id=item.id, role=ROLE_SUPPORTING, reason="test")
        for item in supporting
    )
    return TaskKnowledge(
        task=task,
        index=index,
        primary=[hit(primary)] if primary is not None else [],
        supporting=[hit(item, role=ROLE_SUPPORTING) for item in supporting],
        roles=roles,
    )


def compose(entries, goal=None, *, max_chars: int = 6000) -> str:
    return compose_prompt(
        "yêu cầu của tester", entries, goal or {"type": "kết quả cuối", "expected_value": "X"},
        max_chars=max_chars,
    )


# ── 1. Thông tin lặp chỉ hiện một lần ─────────────────────────────────────
def test_the_same_information_from_two_places_appears_once():
    lines = [
        "Mục 'Đăng xuất' nằm ở cuối danh sách Menu.",
        "Mục 'Đăng xuất' nằm ở cuối danh sách Menu.",
        "Hàng ngay trên 'Đăng xuất' là 'Đánh giá 5 sao'.",
    ]

    assert dedupe_sentences(lines) == [
        "Mục 'Đăng xuất' nằm ở cuối danh sách Menu.",
        "Hàng ngay trên 'Đăng xuất' là 'Đánh giá 5 sao'.",
    ]


# ── 2. Mâu thuẫn không bị nối thành câu vô nghĩa ──────────────────────────
def test_a_conditional_rule_supersedes_the_unconditional_imperative():
    """Ca thật trong KB: "hãy scroll xuống dưới cùng" và "Chỉ cuộn khi nó thật sự chưa hiện"."""
    lines = [
        "Ở màn hình Menu, mục 'Đăng xuất' ở dưới cùng, hãy scroll xuống dưới cùng và ĐÃ HIỆN SẴN.",
        "Chỉ cuộn khi nó thật sự chưa hiện.",
    ]

    kept = dedupe_sentences(lines)

    assert "Chỉ cuộn khi nó thật sự chưa hiện." in kept
    assert not any("hãy scroll xuống dưới cùng" in line for line in kept)


def test_a_real_conflict_is_kept_but_marked_uncertain():
    """Chỉ báo xung đột khi chỉ ra được *chính hai câu* đang mâu thuẫn."""
    lines = [
        "Mục 'Đăng xuất' đã hiện sẵn trên màn hình Menu.",
        "Mục 'Đăng xuất' không hiện sẵn trên màn hình Menu.",
    ]

    kept = dedupe_sentences(lines)

    assert len(kept) == 3
    note = kept[-1]
    assert note.startswith(CONFLICT_NOTE)
    assert "quan sát UI" in note
    # Bằng chứng cụ thể: cả hai câu gốc đều được trích lại.
    assert "đã hiện sẵn trên màn hình Menu" in note
    assert "không hiện sẵn trên màn hình Menu" in note


def test_two_sentences_about_different_actions_are_never_merged():
    """Chốt chặn của yêu cầu "chỉ gộp khi thực sự tương đương": hai việc khác nhau thì
    câu có điều kiện **không** được phép xoá câu kia."""
    lines = [
        "Chỉ cuộn khi danh sách chưa hiện đủ.",
        "Nhập số điện thoại vào ô nhập ở đầu màn hình.",
    ]

    kept = dedupe_sentences(lines)

    assert len(kept) == 2
    assert any("Nhập số điện thoại" in line for line in kept)



# ── 3. Workflow cần thiết: bước của nó phải vào prompt ────────────────────
def test_the_steps_of_the_chosen_workflow_reach_the_prompt():
    logout = workflow(
        "auth.logout",
        "Đăng xuất tài khoản",
        "Đăng xuất khỏi tài khoản đang đăng nhập.",
        preconditions=["Người dùng đã đăng nhập"],
        success_criteria=["Màn hình đăng nhập hiện ra"],
        steps=["Tab ngoài cùng bên phải trên thanh điều hướng dưới cùng"],
    )

    prompt = compose([entry("đăng xuất", primary=logout)])

    assert SECTION_STEPS in prompt
    assert "Tab ngoài cùng bên phải trên thanh điều hướng dưới cùng" in prompt


# ── 4. Không biến "tài khoản đã lưu" thành luồng gõ mật khẩu ──────────────
def test_the_saved_account_flow_is_the_one_rendered():
    """Chỉ workflow *đã được chọn* mới vào bước — nên cách làm bằng mật khẩu không thể lọt."""
    by_saved = workflow(
        "auth.switch_account",
        "Đăng nhập bằng tài khoản đã lưu",
        "Mở danh sách tài khoản đã lưu rồi chọn một tài khoản.",
        steps=["Mũi tên caret-down ở CUỐI ô nhập số điện thoại", "Nút 'Đăng nhập'"],
    )
    by_password = workflow(
        "auth.login_password",
        "Đăng nhập bằng mật khẩu",
        "Nhập số điện thoại và mật khẩu.",
        steps=["Ô Mật khẩu"],
    )
    forgot = workflow("auth.forgot_password", "Khôi phục mật khẩu", steps=["Ô email"])

    prompt = compose(
        [
            entry(
                "đăng nhập",
                target="0972842548",
                primary=by_saved,
                supporting=[by_password, forgot],
            )
        ]
    )

    assert "caret-down" in prompt
    assert "auth.login_password" not in prompt
    assert "auth.forgot_password" not in prompt


# ── 5. Điều kiện trước là điều phải xác minh ──────────────────────────────
def test_preconditions_are_things_to_verify_not_assumed_facts():
    logout = workflow(
        "auth.logout",
        "Đăng xuất",
        preconditions=["Người dùng đã đăng nhập vào app"],
        steps=["Mục 'Đăng xuất'"],
    )

    prompt = compose([entry("đăng xuất", primary=logout)])

    assert SECTION_STATE in prompt
    assert "Người dùng đã đăng nhập vào app" in prompt


# ── 6. Thiếu điều hướng: không bịa vị trí nút ─────────────────────────────
def test_a_missing_screen_instruction_asks_for_observation_instead_of_inventing():
    logout = workflow(
        "auth.logout",
        "Đăng xuất",
        description="Ở màn hình Menu, mục 'Đăng xuất' nằm ở cuối danh sách.",
        steps=["Mục 'Đăng xuất'"],
    )

    prompt = compose([entry("đăng xuất", primary=logout)])

    # Không có workflow điều hướng nào ⇒ composer chỉ được phép nhắc *quan sát*.
    assert "quan sát" in prompt.lower()
    assert "icon" not in prompt.lower()
    assert "góc trên bên phải" not in prompt


# ── 7. Workflow phụ không lọt vào các bước ────────────────────────────────
def test_an_unrelated_supporting_workflow_does_not_become_a_step():
    logout = workflow("auth.logout", "Đăng xuất", steps=["Mục 'Đăng xuất'"])
    modal = workflow(
        "home.switch_house_modal",
        "Xử lý modal 'Chuyển nhà'",
        "Modal này là gợi ý của app, không phải lỗi.",
        steps=["nút 'Huỷ' trong modal"],
    )

    prompt = compose([entry("đăng xuất", primary=logout, supporting=[modal])])
    steps_block = prompt.split(SECTION_STEPS, 1)[1]

    assert "nút 'Huỷ'" not in steps_block



# ── 8. Trung gian tách khỏi mục tiêu cuối ─────────────────────────────────
def test_intermediate_results_are_separated_from_the_final_goal():
    logout = workflow(
        "auth.logout",
        "Đăng xuất",
        success_criteria=["Màn hình đăng nhập hiện ra"],
        steps=["Mục 'Đăng xuất'"],
    )
    login = workflow(
        "auth.switch_account",
        "Đăng nhập bằng tài khoản đã lưu",
        success_criteria=["Màn hình chính hiện ra"],
        steps=["Chọn tài khoản đã lưu"],
    )

    prompt = compose(
        [entry("đăng xuất", primary=logout), entry("đăng nhập", primary=login, index=1)],
        {"type": "tài khoản đăng nhập", "expected_value": "0972842548"},
    )

    assert SECTION_VERIFY in prompt
    assert "Màn hình đăng nhập hiện ra" in prompt
    assert "MỤC TIÊU CUỐI" in prompt
    assert "0972842548" in prompt
    assert "CHƯA XÁC MINH" in prompt


# ── 9. KB thật sự thiếu: giữ trạng thái chưa biết ─────────────────────────
def test_a_task_without_knowledge_is_declared_unknown_not_invented():
    prompt = compose([entry("bật điều hoà", primary=None)])

    assert "chưa có hướng dẫn" in prompt
    assert "KHÔNG tự suy diễn" in prompt


def test_a_primary_without_steps_asks_for_observation():
    vague = workflow("room.toggle", "Bật tắt thiết bị", steps=())

    prompt = compose([entry("bật thiết bị", primary=vague)])

    assert "chưa ghi các bước cụ thể" in prompt


# ── 10. Không phụ thuộc ứng dụng ──────────────────────────────────────────
def test_the_same_rules_work_for_another_app():
    other = workflow(
        "order.checkout",
        "Thanh toán đơn hàng",
        "Hoàn tất thanh toán đơn đang mở.",
        preconditions=["Đơn hàng đang mở"],
        success_criteria=["Màn hình xác nhận đơn hiện ra"],
        steps=["Nút 'Thanh toán' ở cuối màn hình"],
    )

    prompt = compose([entry("thanh toán đơn", primary=other)])

    assert "Nút 'Thanh toán' ở cuối màn hình" in prompt
    assert "Đơn hàng đang mở" in prompt


# ── Thứ tự & cấu trúc: chứng minh logic tổng quát, không phải "đẹp cho một ví dụ" ──
def test_the_workflow_is_normalized_into_four_parts():
    from artemis.knowledge.composer import normalize_workflow

    shape = normalize_workflow(
        Workflow(
            id="app.do_thing",
            app_id="demo",
            name="Làm việc",
            preconditions=["Đang ở màn hình X"],
            success_criteria=["Màn hình Y hiện ra"],
            steps=[
                {"action": "tap", "target": "Nút A", "expected_state": "Hộp thoại mở"},
                {"action": "tap", "target": "Nút B", "expected_state": "Xong"},
            ],
            revision_no=1,
            updated_at="x",
        )
    )

    assert shape.preconditions == ("Đang ở màn hình X",)
    assert shape.actions == ("tap: Nút A", "tap: Nút B")
    assert shape.expected_results == ("Hộp thoại mở", "Xong")
    assert shape.verification == ("Màn hình Y hiện ra",)


def test_step_order_follows_the_kb_not_the_retrieval_order():
    """Bước 2 cần kết quả của bước 1: đảo thứ tự là phá phụ thuộc."""
    ordered = Workflow(
        id="app.ordered",
        app_id="demo",
        name="Việc có phụ thuộc",
        steps=[
            {"action": "tap", "target": "Mở hộp", "expected_state": "Hộp mở"},
            {"action": "tap", "target": "Chọn mục trong hộp", "expected_state": "Mục được chọn"},
            {"action": "tap", "target": "Xác nhận", "expected_state": "Đã lưu"},
        ],
        revision_no=1,
        updated_at="x",
    )

    steps = render_steps([entry("làm việc", primary=ordered)])

    assert steps[1].strip().startswith("1.")
    assert "Mở hộp" in steps[1]
    assert "Chọn mục trong hộp" in steps[2]
    assert "Xác nhận" in steps[3]


def test_multi_step_navigation_is_emitted_before_the_main_workflow():
    navigation = Workflow(
        id="navigation.to_screen",
        app_id="demo",
        name="Tới màn hình cần dùng",
        steps=[
            {"action": "tap", "target": "Tab thứ ba", "expected_state": "Màn hình khu vực"},
            {"action": "tap", "target": "Mục cài đặt", "expected_state": "Màn hình cài đặt"},
        ],
        revision_no=1,
        updated_at="x",
    )
    main = Workflow(
        id="app.main",
        app_id="demo",
        name="Việc chính",
        steps=[{"action": "tap", "target": "Công tắc", "expected_state": "Đã bật"}],
        revision_no=1,
        updated_at="x",
    )

    steps = render_steps([entry("bật công tắc", primary=main, supporting=[navigation])])

    assert "Tab thứ ba" in steps[1]
    assert "Mục cài đặt" in steps[2]
    assert "Công tắc" in steps[3]
    assert steps[3].strip().startswith("3.")


def test_a_conditional_step_keeps_its_condition():
    """Thao tác có điều kiện phải giữ điều kiện, không bị biến thành bước bắt buộc."""
    conditional = Workflow(
        id="app.maybe",
        app_id="demo",
        name="Việc có bước tuỳ điều kiện",
        steps=[
            {"action": "tap", "target": "Nút luôn có"},
            {
                "action": "tap",
                "target": "Nút chỉ có khi đã đăng nhập",
                "optional": True,
            },
        ],
        revision_no=1,
        updated_at="x",
    )

    steps = render_steps([entry("làm việc", primary=conditional)])

    assert "Nút luôn có" in steps[1]
    assert "Nút chỉ có khi đã đăng nhập" in steps[2]
    assert "(chỉ khi cần)" in steps[2]


# ── Nhánh thất bại & điều kiện dừng ──────────────────────────────────────
def search_workflow(app_id: str = "demo") -> Workflow:
    """Workflow có một bước *tìm/chọn một mục trong danh sách*."""
    return Workflow(
        id="account.pick_saved",
        app_id=app_id,
        name="Chọn tài khoản đã lưu",
        preconditions=["Đang ở màn hình chọn tài khoản"],
        success_criteria=["Vào được màn hình chính"],
        steps=[
            {"action": "tap", "target": "Mở danh sách tài khoản đã lưu"},
            {
                "action": "tap",
                "target": "Chọn đúng dòng tài khoản cần dùng trong danh sách",
                "expected_state": "Danh sách đóng lại",
            },
        ],
        revision_no=1,
        updated_at="x",
    )


def test_a_search_step_carries_the_stop_guard():
    """Tìm/chọn trong danh sách mà không thấy thì phải dừng — chốt nằm ngay trên bước đó."""
    steps = render_steps([entry("chọn tài khoản", primary=search_workflow())])

    guarded = [line for line in steps if "danh sách" in line and "⟵" in line]
    assert len(guarded) == 1
    assert "ĐÚNG mục tiêu" in guarded[0]
    assert "DỪNG" in guarded[0]
    assert "NOT_FOUND" in guarded[0]


def test_the_stop_section_separates_not_found_from_not_verified():
    """Hết danh sách ≠ chưa kiểm tra hết: hai kết luận khác nhau, phải nói rõ cả hai."""
    prompt = compose([entry("chọn tài khoản", primary=search_workflow())])

    assert SECTION_STOP in prompt
    stop = prompt.split(SECTION_STOP, 1)[1].split(SECTION_NOTES, 1)[0]
    assert "HẾT danh sách" in stop
    assert "NOT_FOUND" in stop
    assert "CHƯA kiểm tra hết" in stop
    assert "NOT_VERIFIED" in stop
    assert "Không chọn mục khác thay thế" in stop
    assert "quá 2 lần" in stop
    # Điều kiện tiên quyết của mỗi bước: chỉ đi tiếp khi kết quả bước trước đã thấy.
    assert "kết quả mà bước ngay trước đó nói phải thấy" in stop


def test_the_stop_section_stays_small_when_there_is_no_search_step():
    """Không có bước tìm/chọn thì không in phần nói về danh sách — prompt không phình."""
    plain = workflow("app.simple", "Việc đơn giản", steps=["Nhấn nút Xác nhận"])

    prompt = compose([entry("làm việc", primary=plain)])
    stop = prompt.split(SECTION_STOP, 1)[1].split(SECTION_NOTES, 1)[0]

    assert "kết quả mà bước ngay trước đó" in stop
    assert "NOT_FOUND" not in stop
    assert "danh sách" not in stop


def test_search_steps_are_detected_from_kb_language_not_from_the_app():
    from artemis.knowledge.composer import is_search_step

    in_list = {"action": "tap", "target": "Chọn dòng cần dùng trong danh sách"}
    not_in_list = {"action": "tap", "target": "Nhấn nút Xác nhận"}
    english = {"action": "tap", "target": "Pick the right row from the list"}

    assert is_search_step(in_list) is True
    assert is_search_step(not_in_list) is False
    assert is_search_step(english) is True


def test_a_search_step_in_another_app_gets_the_same_treatment():
    prompt = compose([entry("chọn đơn", primary=search_workflow(app_id="shop"))])

    assert "NOT_FOUND" in prompt
    assert "NOT_VERIFIED" in prompt
    assert "shop" not in prompt  # app_id không thuộc prompt, chỉ id workflow


def test_kb_value_and_until_reach_the_prompt():
    """Hai trường KB đang bị bỏ trước đây: ``value`` (giá trị cần nhập) và ``until`` (điều
    kiện dừng của vòng lặp).  Bỏ chúng là agent phải tự đoán giá trị / tự đoán khi nào dừng."""
    typed = Workflow(
        id="app.fill_form",
        app_id="demo",
        name="Điền biểu mẫu",
        steps=[
            {"action": "input_text", "target": "Ô Tên", "value": "<tên do tester cung cấp>"},
            {"action": "scroll", "target": "Danh sách", "until": "Nút Lưu xuất hiện"},
        ],
        revision_no=1,
        updated_at="x",
    )

    steps = render_steps([entry("điền biểu mẫu", primary=typed)])

    assert any("giá trị: <tên do tester cung cấp>" in line for line in steps)
    assert any("lặp cho tới khi: Nút Lưu xuất hiện" in line for line in steps)

# ── Chạm vs nhấn giữ: không được đổi thao tác, không được gộp hai thao tác ──
def test_a_long_press_step_keeps_its_action_and_duration():
    """Lỗi đã gặp: KB ghi "chạm thẻ thiết bị" trong khi thực tế phải NHẤN GIỮ ≥2s mới vào cài
    đặt — chạm chỉ bật/tắt.  Composer phải in đúng thao tác KB ghi, kèm thời lượng."""
    device_settings = Workflow(
        id="navigation.open_device_settings",
        app_id="demo",
        name="Mở thông tin và cài đặt thiết bị",
        steps=[
            {
                "action": "long_press",
                "target": "Thẻ thiết bị đích — NHẤN GIỮ ít nhất 2 giây",
                "expected_state": "Màn hình chi tiết mở ra",
            }
        ],
        revision_no=1,
        updated_at="x",
    )

    steps = render_steps([entry("mở cài đặt thiết bị", primary=device_settings)])

    assert steps[1].strip().startswith("1. long_press:")
    assert "NHẤN GIỮ ít nhất 2 giây" in steps[1]


def test_a_long_press_and_a_tap_are_never_treated_as_the_same_action():
    """Gộp "nhấn giữ" với "chạm" là xoá mất thao tác đúng — hai câu phải được giữ riêng."""
    kept = dedupe_sentences(
        [
            "Nhấn giữ thẻ thiết bị để mở màn chi tiết cài đặt.",
            "Chạm thẻ thiết bị để bật hoặc tắt thiết bị.",
        ]
    )

    assert len(kept) == 2
    assert any("Nhấn giữ" in line for line in kept)
    assert any("Chạm thẻ" in line for line in kept)


def test_the_stop_section_forbids_repeating_the_same_failing_action():
    """Hiện tượng thật: agent chạm cùng một thẻ 2 lần mà màn hình không đổi."""
    plain = workflow("app.any", "Việc bất kỳ", steps=["Nhấn nút Xác nhận"])

    prompt = compose([entry("làm việc", primary=plain)])
    stop = prompt.split(SECTION_STOP, 1)[1].split(SECTION_NOTES, 1)[0]

    assert "KHÔNG lặp lại y nguyên quá 2 lần" in stop
    assert "nhấn giữ thay vì chạm" in stop
    assert "dừng và báo chưa làm được" in stop



def test_the_composer_is_deterministic_and_needs_no_llm():
    """Cùng đầu vào ⇒ cùng đầu ra, và không có tham số nào gọi mạng."""
    logout = workflow(
        "auth.logout",
        "Đăng xuất",
        steps=["Mục 'Đăng xuất'"],
        success_criteria=["Về màn hình Đăng nhập"],
    )
    entries = [entry("đăng xuất", primary=logout)]

    assert compose(entries) == compose(entries)


# ── Mục tiêu cuối luôn có mặt & luật dừng khi đã xong ────────────────────
def test_the_final_goal_is_present_even_when_the_plan_has_none():
    """Lỗi đã gặp: phân tích mục tiêu hỏng ⇒ ``final_goal`` rỗng ⇒ prompt **không** có dòng
    nào về mục tiêu cuối.  Agent vào màn hẹn giờ, thấy "Có 0 hẹn giờ" (đúng điều kiện tester
    hỏi) mà không biết là đã xong, nên thoát ra rồi vào lại nhiều lần."""
    plain = workflow("app.any", "Việc bất kỳ", steps=["Nhấn nút Xác nhận"])

    prompt = compose(
        [entry("làm việc", primary=plain)],
        {},
    )

    assert "MỤC TIÊU CUỐI" in prompt
    assert "yêu cầu của tester" in prompt  # nguyên văn câu tester, dùng khi thiếu final_goal


def test_a_conditional_goal_is_quoted_not_mangled():
    """Điều kiện dạng "… lớn hơn 0 thì false, còn ngược lại" phải giữ nguyên văn: quy về
    ``type = value`` tạo câu vô nghĩa và làm mất chính điều kiện cần đánh giá."""
    plain = workflow("app.any", "Việc bất kỳ", steps=["Nhấn nút Xác nhận"])

    prompt = compose(
        [entry("đếm hẹn giờ", primary=plain)],
        {"type": "số lượng hẹn giờ", "expected_value": "lớn hơn 0 thì false, còn ngược lại"},
    )

    assert "số lượng hẹn giờ — lớn hơn 0 thì false, còn ngược lại" in prompt
    assert " = " not in prompt.split("MỤC TIÊU CUỐI", 1)[1].split("\n", 1)[0]


def test_the_verify_section_says_to_finish_once_the_goal_is_verified():
    plain = workflow("app.any", "Việc bất kỳ", steps=["Nhấn nút Xác nhận"])

    prompt = compose([entry("làm việc", primary=plain)])
    verify = prompt.split(SECTION_VERIFY, 1)[1]

    assert "KẾT THÚC ngay" in verify
    assert "không thoát ra rồi vào lại" in verify
    assert "CHƯA XÁC MINH" in verify

    """Cùng đầu vào ⇒ cùng đầu ra, và không có tham số nào gọi mạng."""
    logout = workflow(
        "auth.logout",
        "Đăng xuất",
        steps=["Mục 'Đăng xuất'"],
        success_criteria=["Về màn hình Đăng nhập"],
    )
    entries = [entry("đăng xuất", primary=logout)]

    assert compose(entries) == compose(entries)


def test_the_task_section_keeps_the_tester_wording_and_conditions():
    logout = workflow("auth.logout", "Đăng xuất", steps=["Mục 'Đăng xuất'"])

    prompt = compose([entry("đăng xuất", primary=logout, condition="chỉ khi đang đăng nhập")])

    assert SECTION_TASK in prompt
    assert "chỉ khi đang đăng nhập" in prompt


def test_the_steps_section_survives_a_tight_budget():
    """Các bước là phần nói *cách làm* — hết chỗ thì cắt mục khác trước."""
    logout = workflow(
        "auth.logout",
        "Đăng xuất",
        preconditions=["Người dùng đã đăng nhập vào app Hunonic"],
        warnings=["Hàng trên 'Đăng xuất' là 'Đánh giá 5 sao' — đừng tap nhầm"],
        steps=["Mục 'Đăng xuất' ở CUỐI danh sách"],
        success_criteria=["Màn hình đăng nhập hiện ra"],
    )

    prompt = compose([entry("đăng xuất", primary=logout)], max_chars=260)

    assert SECTION_STEPS in prompt
    assert "Mục 'Đăng xuất' ở CUỐI danh sách" in prompt


def test_navigation_knowledge_is_rendered_before_the_main_steps():
    logout = workflow("auth.logout", "Đăng xuất", steps=["Mục 'Đăng xuất'"])
    navigation = workflow(
        "navigation.open_menu",
        "Mở màn hình Menu",
        steps=["Tab cuối cùng trên thanh điều hướng dưới cùng"],
    )

    steps = render_steps([entry("đăng xuất", primary=logout, supporting=[navigation])])

    # Dòng đầu là tiêu đề việc; bước điều hướng đứng trước bước chính, và được đánh số từ 1.
    assert steps[0].startswith("«")
    assert "navigation.open_menu" in steps[1]
    assert steps[1].strip().startswith("1.")
    assert "Mục 'Đăng xuất'" in steps[-1]
