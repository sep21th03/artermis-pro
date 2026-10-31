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

"""Trần ảnh trong context của Flash: giữ ảnh mới nhất, bỏ ảnh cũ.

Bối cảnh đo được (session ``ccb9d176``): số ảnh 1 → 2 → 3 → 4 → 5 làm TTFT tăng
7,0s → 40,9s → 53,3s → 98,7s.  Cơ chế ở đây là phép cắt *xác định* trước lượt gọi model:
không gọi LLM, không phụ thuộc ngưỡng transcript, và **luôn giữ ảnh mới nhất** — ảnh mới
nhất là màn hình hiện tại, thứ duy nhất cần cho việc nhận diện UI.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from artemis.utils.context_images import (
    IMAGE_OMITTED_PLACEHOLDER,
    SCREENSHOT_LABEL,
    cap_context_images,
    cap_request_images,
    count_context_images,
)


def image_block(tag: str = "a") -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{tag * 20}"}}


def observation(tag: str, *, with_label: bool = True) -> HumanMessage:
    """Một tail quan sát giống Flash dựng: tiêu đề + nhãn + ảnh + danh sách UI."""
    blocks: list[dict] = [{"type": "text", "text": "# CURRENT OBSERVATION [T+00:01]"}]
    if with_label:
        blocks.append({"type": "text", "text": SCREENSHOT_LABEL})
    blocks.append(image_block(tag))
    blocks.append({"type": "text", "text": "[UI list] 1: Nút A"})
    return HumanMessage(content=blocks)


# ── Đếm ──────────────────────────────────────────────────────────────────
def test_counting_sees_images_across_messages():
    messages = [SystemMessage(content="static"), observation("a"), observation("b")]

    assert count_context_images(messages) == 2


# ── Giữ ảnh mới nhất ─────────────────────────────────────────────────────
def test_capping_keeps_the_newest_image_and_replaces_the_rest():
    messages = [observation("a"), observation("b"), observation("c")]

    dropped = cap_context_images(messages, 1)

    assert dropped == 2
    assert count_context_images(messages) == 1
    # Ảnh mới nhất (c) vẫn nguyên vẹn.
    assert messages[-1].content[2] == image_block("c")
    # Hai ảnh cũ thành một dòng chữ (vị trí trong mảng dịch lên vì nhãn đã bị bỏ).
    for stale in messages[:2]:
        texts = [b.get("text") for b in stale.content if isinstance(b, dict)]
        assert IMAGE_OMITTED_PLACEHOLDER in texts
        assert not any(isinstance(b, dict) and b.get("type") == "image_url" for b in stale.content)


def test_the_label_above_a_dropped_image_leaves_with_it():
    """Không để lại nhãn '--- Current Screenshot ---' trỏ vào khoảng không."""
    messages = [observation("a"), observation("b")]

    cap_context_images(messages, 1)

    texts = [b.get("text") for b in messages[0].content if isinstance(b, dict)]
    assert SCREENSHOT_LABEL not in texts
    # Ảnh được giữ thì nhãn của nó vẫn còn.
    kept_texts = [b.get("text") for b in messages[1].content if isinstance(b, dict)]
    assert SCREENSHOT_LABEL in kept_texts


# ── Bản sao cho request (Operator/Pro) ───────────────────────────────────
def test_cap_request_images_leaves_the_original_messages_untouched():
    """Ledger/trace phải giữ nguyên ảnh: chỉ **request** gửi model bị cắt.

    Đây là yêu cầu tường minh của Phase B ("không xoá ảnh khỏi trace hoặc evidence storage;
    chỉ tối ưu ảnh gửi vào model"). Nếu cắt tại chỗ thì lịch sử hội thoại mất ảnh và trace
    cũng mất theo.
    """
    ledger = [observation("a"), observation("b"), observation("c")]

    request, dropped = cap_request_images(ledger, 1)

    assert dropped == 2
    # Request chỉ còn 1 ảnh (ảnh mới nhất)…
    assert count_context_images(request) == 1
    # …nhưng lịch sử gốc vẫn đủ 3 ảnh, nguyên vẹn từng block.
    assert count_context_images(ledger) == 3
    assert ledger[-1].content[2] == image_block("c")
    assert ledger[0].content[2] == image_block("a")


def test_cap_request_images_is_a_no_op_when_disabled():
    """``max_images <= 0`` ⇒ giữ nguyên hành vi cũ, và không nhân bản gì."""
    ledger = [observation("a"), observation("b")]

    request, dropped = cap_request_images(ledger, 0)

    assert dropped == 0
    assert request is ledger


def test_cap_request_images_keeps_text_only_messages_shared():
    """Message không có ảnh dùng lại chính object cũ (không tốn bộ nhớ vô ích)."""
    system = SystemMessage(content="static instructions")
    ledger = [system, observation("a"), observation("b")]

    request, dropped = cap_request_images(ledger, 1)

    assert dropped == 1
    assert request[0] is system


def test_capping_keeps_two_when_asked_for_two():
    messages = [observation("a"), observation("b"), observation("c"), observation("d")]

    dropped = cap_context_images(messages, 2)

    assert dropped == 2
    assert count_context_images(messages) == 2
    assert messages[2].content[2] == image_block("c")
    assert messages[3].content[2] == image_block("d")


def test_a_cap_of_zero_or_less_changes_nothing():
    messages = [observation("a"), observation("b"), observation("c")]

    assert cap_context_images(messages, 0) == 0
    assert cap_context_images(messages, -3) == 0
    assert count_context_images(messages) == 3


def test_a_context_already_within_the_cap_is_untouched():
    messages = [observation("a"), observation("b")]

    assert cap_context_images(messages, 5) == 0
    assert count_context_images(messages) == 2


def test_the_rest_of_the_transcript_is_never_touched():
    """Chỉ ảnh bị thay: mạch hội thoại và danh sách UI còn nguyên."""
    messages = [
        SystemMessage(content="static prompt"),
        observation("a"),
        HumanMessage(content="AI: tôi đã bấm nút"),
        observation("b"),
    ]

    cap_context_images(messages, 1)

    assert messages[0].content == "static prompt"
    assert messages[2].content == "AI: tôi đã bấm nút"
    assert len(messages) == 4  # không thêm/bớt message
    assert "[UI list] 1: Nút A" in str(messages[1].content)


def test_string_content_messages_are_ignored():
    messages = [HumanMessage(content="chỉ chữ"), observation("a")]

    assert cap_context_images(messages, 1) == 0


def test_images_inside_one_message_are_capped_too():
    """Một message nhiều ảnh (burst) cũng phải tôn trọng trần."""
    message = HumanMessage(content=[image_block("a"), image_block("b"), image_block("c")])

    dropped = cap_context_images([message], 1)

    assert dropped == 2
    assert count_context_images([message]) == 1
