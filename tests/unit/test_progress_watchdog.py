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

"""PHASE B — watchdog tiến triển sub-goal: phân biệt 5 trạng thái và chỉ yêu cầu khi cần.

Đo trên máy thật 30/10/2026 (session ``4806881d``): sub-goal ``Tap "Dled" card…`` ở ``[/]`` suốt 64
bước với các cú tap lặp, nhưng tín hiệu cũ không phân biệt lượt **chỉ chờ retry 429** (không phát
action) hay lượt **bị gate chặn** với lượt **thất bại thật** — nên vừa bỏ sót ca đang lặp, vừa dễ
yêu cầu sai lượt.
"""

from __future__ import annotations

import json

from artemis.utils.progress_watchdog import (
    PROGRESS_WATCHDOG_TURNS,
    UI_CHANGED,
    UI_UNCHANGED,
    UI_UNKNOWN,
    classify_step,
    observation_block,
    progress_streak,
    should_emit,
    watchdog_message,
)


def _step(status: str, *, pre="a.jpg", post="b.jpg", action=True) -> dict:
    return {
        "action_taken": json.dumps([{"action": "tap"}]) if action else None,
        "last_execution_result": json.dumps({"execution": [], "status": status}),
        "pre_image_name": pre,
        "post_image_name": post,
    }


def _hash_by_content(mapping: dict):
    """Resolver giả: tra **hash nội dung** theo tên file (không dùng tên để so sánh)."""

    def _resolve(name):
        return mapping.get(name)

    return _resolve


#: Hai ảnh khác TÊN nhưng cùng NỘI DUNG (hash giống nhau) — ca phải phân loại là unchanged.
SAME_CONTENT = _hash_by_content({"a.jpg": "h1", "b.jpg": "h1", "c.jpg": "h2"})


def test_classifies_the_states_the_watchdog_must_tell_apart():
    assert classify_step({"action_taken": None}, image_hash=SAME_CONTENT) == "no_action"
    assert (
        classify_step(_step("dispatched", pre="a.jpg", post="c.jpg"), image_hash=SAME_CONTENT)
        == UI_CHANGED
    )
    # Khác tên file nhưng cùng nội dung ⇒ unchanged (không được kết luận theo tên).
    assert (
        classify_step(_step("dispatched", pre="a.jpg", post="b.jpg"), image_hash=SAME_CONTENT)
        == UI_UNCHANGED
    )
    assert classify_step(_step("failed"), image_hash=SAME_CONTENT) == "failed"
    assert classify_step(_step("repeat_blocked"), image_hash=SAME_CONTENT) == "repeat_blocked"
    assert classify_step(_step("already_applied"), image_hash=SAME_CONTENT) == "already_applied"


def test_missing_images_are_unknown_and_never_progress():
    """Thiếu ảnh/không hash được ⇒ ``ui_unknown``, và unknown **không** phải tiến triển."""
    step = _step("dispatched", pre="a.jpg", post="missing.jpg")

    assert classify_step(step, image_hash=SAME_CONTENT) == UI_UNKNOWN
    assert classify_step(step, image_hash=None) == UI_UNKNOWN

    streak, kinds = progress_streak([step], image_hash=SAME_CONTENT)
    assert streak == 1 and kinds[UI_UNKNOWN] == 1


def test_a_ledger_blocked_action_is_neutral():
    step = _step("failed")
    step["last_execution_result"] = json.dumps(
        {"execution": [], "status": "failed", "attempts": ["Not executed: no milestone is marked"]}
    )

    assert classify_step(step) == "gate_blocked"


def test_retry_wait_and_gate_blocks_do_not_count_toward_the_streak():
    steps = [
        _step("failed"),
        {"action_taken": None},  # lượt chỉ chờ retry 429
        _step("failed"),
    ]
    step = _step("failed")
    step["last_execution_result"] = json.dumps({"execution": [], "attempts": ["Not executed: x"]})
    steps.append(step)

    streak, kinds = progress_streak(steps, image_hash=SAME_CONTENT)

    assert streak == 2 and kinds == {"failed": 2}


def test_meaningful_progress_resets_the_streak():
    steps = [_step("failed"), _step("failed"), _step("dispatched", pre="a.jpg", post="c.jpg")]

    streak, kinds = progress_streak(steps, image_hash=SAME_CONTENT)

    assert streak == 0 and not kinds


def test_observation_block_composes_the_recovery_and_watchdog_messages():
    """Agent phải nhận **chỉ dẫn phục hồi cụ thể**, không chỉ một thông báo chung."""
    from collections import Counter

    block = observation_block(
        "Chặn đã được MỞ để hành động đi tiếp qua safety gate — quan sát lại màn hình.",
        PROGRESS_WATCHDOG_TURNS,
        Counter({"repeat_blocked": PROGRESS_WATCHDOG_TURNS}),
        "Tap Dled card",
    )

    assert block and "safety gate" in block
    assert "[progress-watchdog]" in block and "Tap Dled card" in block
    assert observation_block(None, 0, Counter(), "x") is None


def test_the_watchdog_emits_at_the_threshold_and_then_periodically():
    assert not should_emit(PROGRESS_WATCHDOG_TURNS - 1)
    assert should_emit(PROGRESS_WATCHDOG_TURNS)
    assert not should_emit(PROGRESS_WATCHDOG_TURNS + 1)
    assert should_emit(PROGRESS_WATCHDOG_TURNS * 2)


def test_the_message_is_specific_and_never_auto_completes_or_drops_criteria():
    from collections import Counter

    message = watchdog_message(
        PROGRESS_WATCHDOG_TURNS,
        Counter({"failed": 2, "dispatched_ui_unchanged": 1}),
        "Tap Dled card to switch current house",
    )

    assert message and "Tap Dled card" in message
    assert "failed ×2" in message and "dispatched_ui_unchanged ×1" in message
    assert "KHÔNG tap lại toạ độ cũ" in message
    assert "Không tự đánh `[x]`" in message and "verify`/`assert" in message
    assert watchdog_message(1, Counter(), "x") is None
