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

"""Pre-execution pixel check: phân biệt "target biến mất vì đã xong" với "target biến mất = lỗi".

Ba ca thật của session ``d110b1ef`` (task ``1371db04``) — cả ba từng bị báo ``Action failed``:

1. nút xác nhận **Đăng xuất** — dialog đã đóng, app sang màn đăng nhập;
2. **caret-down** cạnh ô số điện thoại — icon không còn vì picker tài khoản đã mở;
3. **Quản lý nhà** — UI đã đổi sang màn quản lý nhà.

Bản sửa: VLM trả thêm ``target_state``; chỉ khi ``already_applied`` **và** ảnh trước/sau khác nhau đủ
nhiều thì mới kết luận "đã áp dụng" (không thực thi, không tính thất bại). Mọi trường hợp khác —
kể cả ảnh y hệt hoặc ``blocked`` — giữ nguyên kết luận thất bại như trước (không nới lỏng an toàn).
"""

import asyncio
import base64
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from artemis.agents.validator import precondition_pixel as pp
from artemis.agents.validator.categories import ValidationErrorCategory


def _png(seed: int) -> bytes:
    """Ảnh PNG 8×8 có gradient khác nhau theo ``seed`` (dHash cần ảnh thật)."""
    img = Image.new("L", (8, 8))
    img.putdata([(i * 31 + seed) % 256 for i in range(64)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _uniform() -> bytes:
    img = Image.new("L", (8, 8), color=128)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _Session:
    """Session tối thiểu: chỉ trả ảnh hiện tại."""

    def __init__(self, live: bytes):
        self._live = live

    async def screenshot_b64(self) -> str:
        return base64.b64encode(self._live).decode()


def _verdict(model_json: dict, *, pre: bytes, live: bytes) -> tuple:
    """Chạy một attempt với VLM giả trả ``model_json``."""
    action = {"action": "tap", "coordinates": [100, 200], "target_description": "some control"}
    with (
        patch.object(pp, "acomplete_structured", AsyncMock(return_value=model_json)),
        patch.object(pp.visualization, "crop_and_annotate_target", lambda *a, **k: b"crop"),
    ):
        return asyncio.run(
            pp._run_attempt(
                _Session(live),
                MagicMock(),
                "SYSTEM",
                b"orig-crop",
                [100, 200],
                action,
                None,
                1,
                pre_screenshot_b64=base64.b64encode(pre).decode(),
            )
        )


# ── 3 ca thật: "đã áp dụng" ⇒ KHÔNG phải thất bại ───────────────────────
@pytest.mark.parametrize(
    ("label", "transition"),
    (
        ("Đăng xuất", "logout dialog closed, login screen shown"),
        ("Quản lý nhà", "house management screen opened"),
        ("caret chọn tài khoản", "saved accounts picker opened"),
    ),
)
def test_control_gone_because_the_screen_advanced_is_not_a_failure(label, transition):
    verdict = _verdict(
        {
            "reasoning": "target is gone",
            "is_present": False,
            "confidence": 1.0,
            "target_state": "already_applied",
            "transition": transition,
        },
        pre=_png(1),
        live=_png(200),  # ảnh đổi rõ ⇒ có bằng chứng trước/sau
    )

    passed, category, message = verdict
    assert passed is False  # không thực thi cú tap vô nghĩa
    assert category == ValidationErrorCategory.PIXEL_ALREADY_APPLIED
    assert "Not executed" in message and "Re-observe" in message
    assert transition in message
    assert "failed" not in message.lower()


# ── an toàn: không nới lỏng ─────────────────────────────────────────────
def test_identical_screens_keep_the_failure_verdict():
    """VLM nói already_applied nhưng ảnh y hệt ⇒ không có chuyển trạng thái ⇒ vẫn thất bại."""
    verdict = _verdict(
        {
            "reasoning": "gone",
            "is_present": False,
            "confidence": 1.0,
            "target_state": "already_applied",
            "transition": "claimed change",
        },
        pre=_uniform(),
        live=_uniform(),
    )

    assert verdict[0] is False
    assert verdict[1] == ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED
    assert "Pixel-level validation failed" in verdict[2]


def test_blocked_target_is_still_a_failure():
    verdict = _verdict(
        {
            "reasoning": "covered by popup",
            "is_present": False,
            "confidence": 0.95,
            "target_state": "blocked",
            "transition": "",
        },
        pre=_png(1),
        live=_png(200),
    )

    assert verdict[0] is False
    assert verdict[1] == ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED


def test_low_confidence_still_bypasses():
    verdict = _verdict(
        {
            "reasoning": "unclear",
            "is_present": False,
            "confidence": 0.4,
            "target_state": "blocked",
            "transition": "",
        },
        pre=_png(1),
        live=_png(200),
    )

    assert verdict[0] is True
    assert verdict[1] == ValidationErrorCategory.PIXEL_BYPASSED


def test_target_present_still_passes():
    verdict = _verdict(
        {"reasoning": "still there", "is_present": True, "confidence": 1.0},
        pre=_png(1),
        live=_png(1),
    )

    assert verdict == (True, ValidationErrorCategory.NONE, "")


def test_old_model_answer_without_target_state_keeps_old_behaviour():
    """VLM cũ không trả ``target_state`` ⇒ giữ nguyên kết luận cũ (tương thích ngược)."""
    verdict = _verdict(
        {"reasoning": "gone", "is_present": False, "confidence": 1.0},
        pre=_png(1),
        live=_png(200),
    )

    assert verdict[1] == ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED
