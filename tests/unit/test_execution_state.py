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

"""Trạng thái thực thi có cấu trúc: không tin vào việc LLM tự nhớ, và không tự coi là xong.

Plan và câu gốc ở đây dựng đúng hình dạng lần chạy thật 29/10/2026 (task "đăng nhập tài khoản 0999 →
chuyển nhà Mesh Tạo 2 → tạo ngữ cảnh chạy khi ấn"), gồm cả dấu ``[x]`` mà agent tự đánh khi chưa hề
thấy tài khoản 0999 trên màn hình.
"""

from artemis.utils.execution_state import (
    STATUS_ACTIVE,
    STATUS_NOT_VERIFIED,
    STATUS_PENDING,
    STATUS_VERIFIED,
    advanceable,
    build_execution_state,
    render_execution_state,
)

GOAL = (
    "đăng nhập tài khoản 0999 rồi chuyển nhà sang nhà 'Mesh Tạo 2' rồi vào màn ngữ cảnh chạy khi ấn"
    " tạo ngữ cảnh chạy khi ấn có hành động là công tắc ổ cắm 12"
)

PLAN = (
    "# Task Plan\n"
    "- [x] 1. đăng nhập tài khoản 0999\n"
    "- [/] 2. chuyển nhà sang Mesh Tạo 2 ⇒ nhà hiện tại là Mesh Tạo 2\n"
    "- [ ] 3. tạo ngữ cảnh chạy khi ấn với công tắc ổ cắm 12\n"
)


# ── dựng trạng thái ─────────────────────────────────────────────────────
def test_active_subtask_is_the_one_in_progress():
    state = build_execution_state(PLAN, GOAL, "")
    active = state.active()

    assert active is not None
    assert active.status == STATUS_ACTIVE
    assert "chuyển nhà" in active.text
    assert active.postcondition == "nhà hiện tại là Mesh Tạo 2"


def test_a_milestone_marked_done_without_evidence_is_not_verified():
    """Đúng lỗi đã xảy ra: agent tự đánh ``[x]`` cho "đã đăng nhập" mà chưa từng thấy 0999."""
    state = build_execution_state(PLAN, GOAL, observed_text="Trang chủ · Nhà Mesh Tạo 1")

    login = next(item for item in state.subtasks if "đăng nhập" in item.text)
    assert login.status == STATUS_NOT_VERIFIED
    assert "0999" in login.reason
    assert login.verified is False


def test_the_same_milestone_becomes_verified_once_the_value_was_seen():
    state = build_execution_state(PLAN, GOAL, observed_text="Tài khoản 0999 · Nhà Mesh Tạo 2")

    login = next(item for item in state.subtasks if "đăng nhập" in item.text)
    assert login.status == STATUS_VERIFIED
    assert login.verified is True
    assert state.missing_values == []


def test_pending_subtasks_stay_pending():
    state = build_execution_state(PLAN, GOAL, "")
    third = state.subtasks[-1]

    assert third.status == STATUS_PENDING
    assert "ngữ cảnh" in third.text


def test_required_values_come_from_the_goal_not_from_the_plan():
    """Giá trị bắt buộc là dữ liệu của tester (``0999``), không phải thứ agent tự ghi vào plan."""
    state = build_execution_state(PLAN, GOAL, "")

    assert any(value == "0999" for _label, value in state.required_values)
    assert state.missing_values == ["0999"]


def test_state_is_json_ready_for_the_graph_channel():
    payload = build_execution_state(PLAN, GOAL, "").as_dict()

    assert set(payload) == {
        "subtasks",
        "required_values",
        "observed_values",
        "missing_values",
    }
    assert payload["subtasks"][0]["status"] in {
        STATUS_PENDING,
        STATUS_ACTIVE,
        STATUS_VERIFIED,
        STATUS_NOT_VERIFIED,
    }


def test_an_empty_plan_yields_an_empty_state_instead_of_raising():
    state = build_execution_state("", GOAL, "")

    assert state.subtasks == []
    assert state.active() is None
    assert advanceable(state) == (True, "")


# ── chốt chuyển công đoạn ───────────────────────────────────────────────
def test_advancing_is_blocked_with_not_verified_while_a_value_is_missing():
    """A.5/A.6: chỉ chuyển khi tiêu chí đã xác minh; chưa xác minh được ⇒ NOT_VERIFIED, không im lặng."""
    allowed, reason = advanceable(build_execution_state(PLAN, GOAL, "Nhà Mesh Tạo 1"))

    assert allowed is False
    assert reason.startswith("NOT_VERIFIED")
    assert "0999" in reason


def test_advancing_is_allowed_once_every_required_value_was_seen():
    allowed, reason = advanceable(build_execution_state(PLAN, GOAL, "Tài khoản 0999"))

    assert allowed is True
    assert reason == ""


def test_no_required_value_means_nothing_to_block_on():
    """Câu không nêu định danh nào thì không có gì để xác minh — không được chặn vô cớ."""
    allowed, _ = advanceable(build_execution_state(PLAN, "mở app rồi vào cài đặt", ""))
    assert allowed is True


# ── khối chữ cho prompt ─────────────────────────────────────────────────
def test_rendered_block_is_about_the_current_subtask_only():
    """Ngân sách ngữ cảnh theo công đoạn: không in lại cả kế hoạch."""
    block = render_execution_state(build_execution_state(PLAN, GOAL, "Nhà Mesh Tạo 1"))

    assert "Đang làm" in block
    assert "chuyển nhà" in block
    assert "CHƯA xác minh" in block and "0999" in block
    assert "NOT_VERIFIED" in block
    # Công đoạn 3 chưa tới lượt thì không cần xuất hiện như một chỉ dẫn.
    assert len(block) < 800


def test_rendered_block_is_empty_when_there_is_nothing_to_say():
    assert render_execution_state(build_execution_state("", "mở app", "")) == ""


def test_rendered_block_stays_within_its_budget():
    long_plan = "# Task Plan\n" + "\n".join(
        f"- [/] {index}. công đoạn rất dài " + "x" * 200 for index in range(1, 6)
    )
    block = render_execution_state(build_execution_state(long_plan, GOAL, ""), max_chars=300)

    assert len(block) <= 320
