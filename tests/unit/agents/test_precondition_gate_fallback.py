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

"""Regression: XML gate fail ⇒ verdict của pixel fallback **phải** được truyền tới execution.

Đo trên máy thật 30/10/2026 (session 93882dc3, bước đăng xuất): XML báo "bị 'Quên mật khẩu' chiếm
chỗ", pixel chứng minh ``target_state=already_applied`` (dialog đã đóng, màn Đăng nhập hiện ra) —
nhưng hàm trả về category/lý do của **XML**, nên action bị tính ``failed`` và mở incident dù nó đã
thành công, và Operator có thể tap lại chính chỗ đó.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from artemis.agents.validator.categories import ValidationErrorCategory
from artemis.agents.validator.execution_loop import _run_precondition_gate

XML_FAIL = ValidationErrorCategory.TARGET_OCCUPIED
XML_ERROR = "The expected position [520, 900] is occupied by a different element: 'Quên mật khẩu'"
PIXEL_MSG = "Not executed: the target is gone because the screen already moved to the expected state"


def _action() -> dict:
    """Action có metadata index ⇒ đi qua gate XML trước (đúng nhánh đã lỗi trên máy thật)."""
    return {
        "action": "tap",
        "target_text": "Đăng xuất",
        "target_bounds": [383, 854, 658, 947],
        "target_class": "",
        "coordinates": [520, 900],
    }


def _node(xml: tuple, pixel: tuple) -> SimpleNamespace:
    async def _xml(*_a, **_k):
        return xml

    async def _pixel(*_a, **_k):
        return pixel

    engine = SimpleNamespace(step_metrics=None, current_step_number=3)
    return SimpleNamespace(
        _validate_action_precondition=_xml,
        _validate_action_precondition_pixel=_pixel,
        ctx=SimpleNamespace(data_engine=engine),
    )


def _run(node, action=None) -> tuple[bool, ValidationErrorCategory, str]:
    return asyncio.run(
        _run_precondition_gate(
            node,
            session=SimpleNamespace(),
            action_item=action or _action(),
            state=SimpleNamespace(),
            pre_screenshot_b64="pre",
            original_coords=[520, 900],
        )
    )


def test_xml_fail_then_pixel_already_applied_is_propagated():
    """Ca đã gây lỗi thật: verdict ``already_applied`` của pixel phải tới được execution."""
    node = _node(
        (False, XML_FAIL, XML_ERROR),
        (False, ValidationErrorCategory.PIXEL_ALREADY_APPLIED, PIXEL_MSG),
    )

    passed, category, message = _run(node)

    assert passed is False
    assert category == ValidationErrorCategory.PIXEL_ALREADY_APPLIED
    assert message == PIXEL_MSG


@pytest.mark.parametrize(
    "pixel_category",
    [
        ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED,
        ValidationErrorCategory.PIXEL_BYPASSED,
        ValidationErrorCategory.TARGET_OCCUPIED,
        ValidationErrorCategory.GENERAL,
    ],
)
def test_xml_fail_with_any_other_pixel_verdict_keeps_the_xml_failure(pixel_category):
    """Không nới lỏng gate: mọi verdict khác của pixel vẫn giữ nguyên thất bại của XML."""
    node = _node((False, XML_FAIL, XML_ERROR), (False, pixel_category, "pixel says no"))

    passed, category, message = _run(node)

    assert passed is False
    assert category == XML_FAIL
    assert message == XML_ERROR


def test_xml_fail_then_pixel_confirms_the_target_is_a_success():
    """Hành vi cũ giữ nguyên: pixel xác nhận target ⇒ cho thực thi."""
    node = _node((False, XML_FAIL, XML_ERROR), (True, ValidationErrorCategory.NONE, ""))

    passed, category, _ = _run(node)

    assert passed is True and category == ValidationErrorCategory.NONE


def test_xml_pass_short_circuits_the_pixel_fallback():
    node = _node(
        (True, ValidationErrorCategory.NONE, ""),
        (False, ValidationErrorCategory.PIXEL_BYPASSED, "x"),
    )

    passed, category, _ = _run(node)

    assert passed is True and category == ValidationErrorCategory.NONE


def test_xml_bypassed_still_returns_the_pixel_verdict_as_is():
    """Legacy: ``XML_BYPASSED`` ⇒ dùng thẳng verdict của pixel (kể cả already_applied)."""
    node = _node(
        (False, ValidationErrorCategory.XML_BYPASSED, "bypassed"),
        (False, ValidationErrorCategory.PIXEL_ALREADY_APPLIED, PIXEL_MSG),
    )

    passed, category, message = _run(node)

    assert passed is False
    assert category == ValidationErrorCategory.PIXEL_ALREADY_APPLIED
    assert message == PIXEL_MSG


def test_pure_coordinate_action_uses_the_pixel_gate_only():
    """Legacy: action không có metadata index ⇒ pixel là gate duy nhất, verdict giữ nguyên."""
    action = {"action": "tap", "coordinates": [10, 20]}
    node = _node(
        (True, ValidationErrorCategory.NONE, ""),
        (False, ValidationErrorCategory.PIXEL_ALREADY_APPLIED, PIXEL_MSG),
    )

    passed, category, message = _run(node, action)

    assert passed is False
    assert category == ValidationErrorCategory.PIXEL_ALREADY_APPLIED
    assert message == PIXEL_MSG


def test_the_loop_maps_already_applied_to_no_incident():
    """Bất biến phía loop: ``PIXEL_ALREADY_APPLIED`` ⇒ ``already_applied``, KHÔNG intercepted/incident.

    Kiểm ở mức mã nguồn vì nhánh này nằm giữa ``run_validation_loop`` (không có seam riêng); nếu ai
    đổi nó thành ``intercepted = True`` thì action đã thành công lại bị mở incident như cũ.
    """
    import inspect

    from artemis.agents.validator import execution_loop

    source = inspect.getsource(execution_loop)
    branch = source.split(
        "if validation_category == ValidationErrorCategory.PIXEL_ALREADY_APPLIED:", 1
    )
    assert len(branch) == 2, "nhánh already_applied phải còn tồn tại"
    body = branch[1].split("else:", 1)[0]
    assert "outcome.already_applied = True" in body
    assert "outcome.intercepted = False" in body

