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

"""The agent must not be allowed to tap the same spot forever.

Measured on trace ``232d2382``: the agent tapped ``[135,631][586,670]`` **ten
times in a row** with the screen unchanged. Each round trip costs 4-10 LLM calls
of ~17k prompt tokens, so the whole run spent 47 calls and 2m35s going nowhere --
which a tester reads as "the scroll took two minutes".

The guard blocks on the third identical action and hands the Operator a reason
it can act on, instead of letting it burn another ten round trips.
"""

from __future__ import annotations

import pytest

from artemis.agents.validator.categories import ValidationErrorCategory
from artemis.agents.validator.execution_loop import (
    REPEAT_GUARD_LIMIT,
    repeat_loop_reason,
)


class FakeContext:
    pass


class FakeNode:
    def __init__(self) -> None:
        self.ctx = FakeContext()


def tap(bounds=(135, 631, 586, 670), **extra) -> dict:
    return {"coordinates": list(bounds), **extra}


# ── chặn ─────────────────────────────────────────────────────────────────
def test_the_third_identical_tap_is_blocked():
    node = FakeNode()
    for _ in range(REPEAT_GUARD_LIMIT - 1):
        assert repeat_loop_reason(node, "tap", tap()) is None
    reason = repeat_loop_reason(node, "tap", tap())
    assert reason is not None
    assert "KHÔNG lặp lại nữa" in reason


def test_a_nearby_tap_is_the_same_spot():
    """Toạ độ lệch vài pixel trong cùng một khoảng 10px vẫn là cùng một chỗ."""
    node = FakeNode()
    repeat_loop_reason(node, "tap", tap((132, 632, 582, 672)))
    repeat_loop_reason(node, "tap", tap((133, 633, 583, 673)))
    assert repeat_loop_reason(node, "tap", tap((134, 634, 584, 674))) is not None


def test_a_tap_on_the_next_row_is_a_different_spot():
    """Hàng trong Menu cao ~55px, nên lệch hẳn một hàng phải khác khoá."""
    node = FakeNode()
    repeat_loop_reason(node, "tap", tap((135, 631, 586, 670)))
    repeat_loop_reason(node, "tap", tap((135, 686, 586, 725)))
    assert repeat_loop_reason(node, "tap", tap((135, 741, 586, 780))) is None


def test_long_press_is_guarded_too():
    node = FakeNode()
    for _ in range(REPEAT_GUARD_LIMIT - 1):
        repeat_loop_reason(node, "long_press", tap())
    assert repeat_loop_reason(node, "long_press", tap()) is not None


# ── không chặn nhầm ──────────────────────────────────────────────────────
def test_scrolling_repeatedly_is_normal():
    """Cuộn xuống nhiều lần để tới cuối danh sách là hợp lệ."""
    node = FakeNode()
    for _ in range(10):
        assert repeat_loop_reason(node, "scroll", {"direction": "down"}) is None


def test_tapping_different_spots_is_normal():
    node = FakeNode()
    for y in (100, 200, 300, 400, 500):
        assert repeat_loop_reason(node, "tap", tap((135, y, 586, y + 40))) is None


def test_a_different_action_breaks_the_chain():
    node = FakeNode()
    repeat_loop_reason(node, "tap", tap())
    repeat_loop_reason(node, "tap", tap())
    repeat_loop_reason(node, "scroll", {"direction": "down"})
    assert repeat_loop_reason(node, "tap", tap()) is None


def test_the_guard_only_fires_after_the_limit():
    node = FakeNode()
    for index in range(REPEAT_GUARD_LIMIT - 1):
        assert repeat_loop_reason(node, "tap", tap()) is None, f"lần {index + 1}"


# ── an toàn ──────────────────────────────────────────────────────────────
def test_a_node_without_a_context_is_not_an_error():
    class Bare:
        pass

    assert repeat_loop_reason(Bare(), "tap", tap()) is None


def test_the_category_is_reported():
    assert ValidationErrorCategory.REPEAT_LOOP == "repeat_loop"


def test_a_tap_without_coordinates_falls_back_to_the_label():
    """Một số hành động chỉ có nhãn, không có toạ độ."""
    node = FakeNode()
    for _ in range(REPEAT_GUARD_LIMIT - 1):
        repeat_loop_reason(node, "tap", {"target_text": "Đăng xuất"})
    assert repeat_loop_reason(node, "tap", {"target_text": "Đăng xuất"}) is not None
