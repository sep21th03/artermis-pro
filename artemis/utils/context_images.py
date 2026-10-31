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

"""Trần số ảnh chụp màn hình được giữ trong context của agent.

**Vì sao cần.** Đo trên máy thật (Flash, session ``ccb9d176``): số ảnh trong một lượt gọi
tăng dần 1 → 2 → 3 → 4 → 5 và TTFT tăng theo **7,0s → 40,9s → 53,3s → 98,7s**, riêng
Operator LLM chiếm 247,7s/266,7s của cả session. Nguyên nhân không phải lỗi: ledger cắt ảnh
theo ``image_scrub_depth_relaxed`` (mặc định **6**) chừng nào occupancy còn dưới
``start_ratio`` — mà với prompt 12-16k token trên model cửa sổ lớn thì occupancy chưa bao
giờ vượt ngưỡng, nên suốt session giữ nguyên 6 ảnh.

Trần ở đây **độc lập** với transcript compression và với occupancy: nó là một phép cắt xác
định (deterministic), chạy ngay trước lượt gọi model, và **luôn giữ ảnh mới nhất** — ảnh mới
nhất là thứ duy nhất agent cần để nhận diện màn hình hiện tại. Ảnh cũ chỉ còn giá trị so
sánh UI, mà việc đó đã có ``visual_summary``/dấu vân tay UI và các bước đã ghi trong
transcript lo.
"""

from __future__ import annotations

from typing import Any

#: Chỗ đứng của ảnh bị cắt: một dòng chữ, không phải ảnh — giữ nguyên vị trí để transcript
#: vẫn đọc được mạch ("ở bước này đã có một ảnh, đã bỏ để tiết kiệm context").
IMAGE_OMITTED_PLACEHOLDER = "[screenshot of an earlier step omitted to save context]"

#: Nhãn đứng ngay trên ảnh trong tail quan sát (xem ``FlashRunner._build_tail``). Khi bỏ
#: ảnh thì bỏ luôn nhãn này, nếu không sẽ còn một dòng "--- Current Screenshot ---" trỏ
#: vào khoảng không.
SCREENSHOT_LABEL = "--- Current Screenshot ---"


def _is_image_block(block: Any) -> bool:
    return isinstance(block, dict) and (
        block.get("type") in ("image_url", "image") or "image_url" in block
    )


def cap_request_images(messages: list[Any], max_images: int) -> tuple[list[Any], int]:
    """Bản sao của ``messages`` **đã** áp trần ảnh — không sửa danh sách gốc.

    Vì sao phải sao chép: :func:`cap_context_images` thay block ảnh cũ bằng một dòng chữ **tại
    chỗ**. Với Operator (Pro), danh sách message là của transcript ledger (và là thứ đi vào trace),
    nên sửa tại chỗ là **mất ảnh khỏi lịch sử**, trong khi yêu cầu chỉ là "tối ưu ảnh *gửi vào
    model*". Ở đây mỗi message có ``content`` dạng list được nhân bản (message mới + list mới), nên
    phần gốc giữ nguyên ảnh; message không có ảnh vẫn dùng lại chính object cũ (không tốn gì).

    Trả ``(danh sách mới, số ảnh đã bỏ)``. ``max_images <= 0`` ⇒ trả nguyên danh sách gốc.
    """
    if max_images <= 0 or not messages:
        return messages, 0

    copied: list[Any] = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            try:
                copied.append(message.model_copy(update={"content": list(content)}))
                continue
            except Exception:  # noqa: BLE001 - không nhân bản được thì giữ nguyên message đó
                copied.append(message)
                continue
        copied.append(message)

    dropped = cap_context_images(copied, max_images)
    return copied, dropped


def count_context_images(messages: list[Any]) -> int:
    """Số block ảnh đang có trong danh sách message."""
    total = 0
    for message in messages or []:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            total += sum(1 for block in content if _is_image_block(block))
    return total


def cap_context_images(messages: list[Any], max_images: int) -> int:
    """Giữ tối đa ``max_images`` ảnh **mới nhất**, bỏ ảnh cũ hơn. Trả số ảnh đã bỏ.

    Quy tắc:

    * Ảnh mới nhất luôn được giữ (kể cả ``max_images=1``) — đó là màn hình hiện tại.
    * Ảnh cũ bị thay bằng :data:`IMAGE_OMITTED_PLACEHOLDER` ngay tại chỗ, nên mạch hội
      thoại và số thứ tự block không đổi.
    * Nhãn ``--- Current Screenshot ---`` đi kèm ảnh bị bỏ cũng bị bỏ, tránh nhãn mồ côi.
    * ``max_images <= 0`` ⇒ không làm gì (giữ hành vi cũ, dùng để tắt bằng cấu hình).
    """
    if not messages or max_images <= 0:
        return 0

    positions: list[tuple[int, int]] = []  # (chỉ số message, chỉ số block)
    for message_index, message in enumerate(messages):
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if _is_image_block(block):
                positions.append((message_index, block_index))

    if len(positions) <= max_images:
        return 0

    to_drop = positions[: len(positions) - max_images]
    dropped = 0
    for message_index, block_index in to_drop:
        message = messages[message_index]
        content = list(message.content)
        if block_index < len(content) and _is_image_block(content[block_index]):
            content[block_index] = {"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER}
            dropped += 1
        # Nhãn ngay trên ảnh: bỏ luôn để không còn nhãn trỏ vào khoảng không.
        label_index = block_index - 1
        if label_index >= 0:
            previous = content[label_index]
            if (
                isinstance(previous, dict)
                and previous.get("type") == "text"
                and str(previous.get("text", "")).strip() == SCREENSHOT_LABEL
            ):
                content.pop(label_index)
        message.content = content
    return dropped
