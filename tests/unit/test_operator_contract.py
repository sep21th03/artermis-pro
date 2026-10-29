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

"""Hợp đồng thực thi: luật chạy bằng code, mặc định chỉ ghi log.

Bốn luật ở đây là hệ quả trực tiếp của lần chạy thật 29/10/2026 (task "đăng nhập 0999 → chuyển nhà
Mesh Tạo 2 → tạo ngữ cảnh chạy khi ấn"): agent tự nhập tài khoản, tự bấm Đăng xuất giữa công đoạn
chuyển nhà, và bị safety net chặn 3 lần. Điều phải khoá bằng test:

1. Luật **bắt đúng** hành vi sai (không phải chỉ "không báo lỗi").
2. Mặc định **không chặn** — bật cưỡng chế là hành động có chủ ý, theo từng luật.
3. Bật cưỡng chế thì action bị chặn **kèm lý do đọc được**, không im lặng.
"""

import pytest

from artemis.agents.operator import contract
from artemis.agents.operator.contract import (
    ContractContext,
    blocking_violations,
    check_actions,
    describe,
    is_enforced,
    tester_supplied_password,
)


def _ctx(**overrides) -> ContractContext:
    payload = {
        "goal_text": "đăng nhập tài khoản 0999 rồi chuyển nhà sang Mesh Tạo 2",
        "subtask_text": "chuyển nhà sang Mesh Tạo 2",
        "saved_account_required": True,
    }
    payload.update(overrides)
    return ContractContext(**payload)


def _rules(violations) -> set[str]:
    return {item.rule for item in violations}


# ── luật 1: không tự nhập mật khẩu ──────────────────────────────────────
def test_typing_into_a_password_field_without_a_supplied_password_is_a_violation():
    actions = [{"action": "input_text", "target": "Ô nhập mật khẩu", "text": "123456"}]
    assert _rules(check_actions(actions, _ctx())) == {"no_invented_password"}


def test_a_supplied_password_makes_the_same_action_legal():
    actions = [{"action": "input_text", "target": "Ô nhập mật khẩu", "text": "Abc@1234"}]
    assert check_actions(actions, _ctx(tester_provided_password=True)) == []


def test_typing_into_the_account_field_is_not_a_password_violation():
    """Chốt này không được chặn việc đăng nhập — chỉ chặn việc bịa mật khẩu."""
    actions = [{"action": "input_text", "target": "Ô số điện thoại", "text": "0999"}]
    assert check_actions(actions, _ctx()) == []


def test_a_link_labelled_forgot_password_is_not_a_password_field():
    """Nhãn có chữ "mật khẩu" mà không phải thao tác nhập thì không được coi là vi phạm."""
    actions = [{"action": "click", "target": "Quên mật khẩu"}]
    assert check_actions(actions, _ctx()) == []


def test_password_detection_needs_a_value_not_just_the_word():
    """Prompt đã ghép luôn chứa chữ "mật khẩu" (tên workflow, bước KB) nên không thể dựa vào chữ."""
    assert tester_supplied_password("đăng nhập tài khoản 0999") is False
    assert tester_supplied_password("đăng nhập 0999 mật khẩu Abc@1234") is True
    assert tester_supplied_password("dùng tài khoản đã lưu, không cần mật khẩu") is False


# ── luật 2: thao tác nhạy cảm ngoài phạm vi ─────────────────────────────
def test_logging_out_while_switching_house_is_out_of_scope():
    """Đúng hành vi đã xảy ra trên máy thật: bấm Đăng xuất giữa công đoạn chuyển nhà."""
    actions = [{"action": "click", "target": "Nút Đăng xuất"}]
    assert _rules(check_actions(actions, _ctx())) == {"sensitive_action_out_of_scope"}


def test_logging_out_is_fine_when_the_subtask_asks_for_it():
    actions = [{"action": "click", "target": "Đăng xuất"}]
    ctx = _ctx(subtask_text="đăng xuất tài khoản hiện tại")
    assert check_actions(actions, ctx) == []


def test_switching_house_is_not_a_sensitive_action():
    actions = [{"action": "click", "target": "Chọn nhà Mesh Tạo 2"}]
    assert check_actions(actions, _ctx()) == []


# ── luật 3: không tự đóng overlay thuộc workflow ────────────────────────
def test_closing_a_modal_that_belongs_to_the_subtask_is_a_violation():
    actions = [{"action": "click", "target": "Đóng"}]
    assert _rules(check_actions(actions, _ctx(open_overlay_expected=True))) == {
        "overlay_closed_unintended"
    }


def test_closing_an_overlay_is_fine_when_no_overlay_is_expected():
    actions = [{"action": "click", "target": "Đóng"}]
    assert check_actions(actions, _ctx(open_overlay_expected=False)) == []


# ── luật 4: không báo xong khi chưa có bằng chứng ───────────────────────
def test_reporting_success_without_evidence_is_a_violation():
    actions = [{"action": "report_task_status", "name": "report_task_status"}]
    assert _rules(check_actions(actions, _ctx(postcondition_verified=False))) == {
        "success_requires_evidence"
    }


def test_reporting_success_with_evidence_is_fine():
    actions = [{"action": "report_task_status"}]
    assert check_actions(actions, _ctx(postcondition_verified=True)) == []


def test_an_ordinary_action_never_triggers_the_evidence_rule():
    """Nếu luật này áp cho mọi action thì mọi lượt bấm đều bị coi là "tuyên bố thành công"."""
    actions = [{"action": "click", "target": "Cài đặt"}]
    assert check_actions(actions, _ctx(postcondition_verified=False)) == []


# ── chế độ: log-only (mặc định) vs cưỡng chế theo từng luật ─────────────
def test_by_default_nothing_is_enforced(monkeypatch):
    """Mặc định phải là log-only: bật cưỡng chế là hành động có chủ ý của người vận hành."""
    monkeypatch.delenv(contract.ENFORCE_ENV, raising=False)
    monkeypatch.delenv(contract.RULES_ENV, raising=False)
    actions = [{"action": "click", "target": "Đăng xuất"}]

    assert contract.enforcement_enabled() is False
    assert blocking_violations(actions, _ctx()) == []
    # Nhưng vi phạm vẫn được **thấy**: đó là dữ liệu để quyết định bật luật nào.
    assert describe(check_actions(actions, _ctx())) != ""


def test_enforcing_one_rule_leaves_the_others_log_only(monkeypatch):
    """Bật theo từng luật: thu thập dương tính giả của luật này mà không đụng luật khác."""
    monkeypatch.setenv(contract.ENFORCE_ENV, "1")
    monkeypatch.setenv(contract.RULES_ENV, "no_invented_password")

    assert is_enforced("no_invented_password") is True
    assert is_enforced("sensitive_action_out_of_scope") is False

    actions = [{"action": "click", "target": "Đăng xuất"}]
    assert blocking_violations(actions, _ctx()) == []  # luật này chưa bật ⇒ vẫn đi tiếp

    password = [{"action": "input_text", "target": "Mật khẩu", "text": "x1234"}]
    assert _rules(blocking_violations(password, _ctx())) == {"no_invented_password"}


def test_enforcing_without_a_rule_list_enforces_everything(monkeypatch):
    monkeypatch.setenv(contract.ENFORCE_ENV, "1")
    monkeypatch.delenv(contract.RULES_ENV, raising=False)

    assert is_enforced("sensitive_action_out_of_scope") is True
    actions = [{"action": "click", "target": "Đăng xuất"}]
    assert _rules(blocking_violations(actions, _ctx())) == {"sensitive_action_out_of_scope"}


def test_violation_payload_is_json_ready_and_says_whether_it_is_enforced(monkeypatch):
    monkeypatch.setenv(contract.ENFORCE_ENV, "1")
    monkeypatch.setenv(contract.RULES_ENV, "sensitive_action_out_of_scope")
    violation = check_actions([{"action": "click", "target": "Đăng xuất"}], _ctx())[0]

    payload = violation.as_dict()

    assert payload["rule"] == "sensitive_action_out_of_scope"
    assert payload["enforced"] is True
    assert payload["term"] == "đăng xuất"


def test_every_rule_has_a_name_and_a_reason():
    """Răng chung: luật không tên hoặc không lý do thì không ghi log được ⇒ vô dụng."""
    actions = [
        {"action": "input_text", "target": "Mật khẩu", "text": "x1234"},
        {"action": "click", "target": "Đăng xuất"},
        {"action": "click", "target": "Đóng"},
        {"action": "report_task_status"},
    ]
    violations = check_actions(actions, _ctx(open_overlay_expected=True))

    assert {item.rule for item in violations} == {
        "no_invented_password",
        "sensitive_action_out_of_scope",
        "overlay_closed_unintended",
        "success_requires_evidence",
    }
    assert all(item.reason for item in violations)
    assert all(item.severity == "block" for item in violations)


def test_no_actions_means_no_violations():
    assert check_actions([], _ctx()) == []
    assert check_actions(None, _ctx()) == []
