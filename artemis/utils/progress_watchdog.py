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

"""Watchdog tiến triển sub-goal (PHASE B) — **chỉ đọc**: không tự chốt `[x]`, không bỏ tiêu chí.

Vì sao cần (máy thật 30/10/2026, session ``4806881d``): sub-goal ``Tap "Dled" card to switch current
house`` nằm ở ``[/]`` suốt 64 bước, agent tap lại cùng toạ độ, màn hình không đổi — nhưng tín hiệu
duy nhất hiện có ("stayed in progress for N action turns") **không phân biệt** lượt chờ retry 429,
lượt bị gate chặn, lượt thất bại thật và lượt dispatch mà UI không đổi. Kết quả: yêu cầu gửi cho
Operator vừa chung chung vừa bỏ sót đúng ca đang lặp.

Phân loại mỗi lượt (từ bản ghi step):

* ``no_action`` — lượt không phát action (**gồm cả lượt chỉ chờ retry 429**) ⇒ trung tính.
* ``gate_blocked`` — action bị gate ledger chặn, chưa từng tới thiết bị ⇒ trung tính.
* ``repeat_blocked`` — retry guard chặn lặp ⇒ **không tiến triển**.
* ``failed`` — action thất bại thật (dispatch lỗi hoặc safety net từ chối) ⇒ **không tiến triển**.
* ``dispatched_ui_unchanged`` — dispatch xong mà ảnh trước/sau y nhau ⇒ **không tiến triển**.
* ``dispatched_ui_changed`` / ``already_applied`` — UI đổi có ý nghĩa ⇒ **tiến triển** (đặt lại chuỗi).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable

#: Số lượt "không tiến triển thật" liên tiếp trước khi gửi yêu cầu quan sát lại.
PROGRESS_WATCHDOG_TURNS = 3

#: Ba trạng thái UI sau một lần dispatch — xác định bằng **hash nội dung ảnh**, không bằng tên file.
UI_CHANGED = "dispatched_ui_changed"
UI_UNCHANGED = "dispatched_ui_unchanged"
UI_UNKNOWN = "dispatched_ui_unknown"

#: Lượt **không tiến triển** (tính vào chuỗi). ``ui_unknown`` nằm ở đây vì **thiếu ảnh/không hash
#: được KHÔNG được mặc định là tiến triển**.
NO_PROGRESS_KINDS = frozenset({"repeat_blocked", "failed", UI_UNCHANGED, UI_UNKNOWN})

#: Lượt **có tiến triển thật** (đặt lại chuỗi).
PROGRESS_KINDS = frozenset({UI_CHANGED, "already_applied"})

#: Lượt **trung tính**: không tính vào chuỗi, cũng không đặt lại (ví dụ chỉ chờ retry 429).
NEUTRAL_KINDS = frozenset({"no_action", "gate_blocked", "unknown"})


def _execution_status(step: dict) -> str:
    raw = step.get("last_execution_result")
    parsed = raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:  # noqa: BLE001 - bản ghi lạ chỉ làm phân loại kém, không được gãy run
            return ""
    if isinstance(parsed, dict):
        return str(parsed.get("status") or "")
    return ""


def _hash_for_step(ui_hash_by_step, step_number) -> str | None:
    if not isinstance(ui_hash_by_step, dict) or step_number is None:
        return None
    return ui_hash_by_step.get(int(step_number))


def _next_step_hash(ui_hash_by_step, step_number) -> str | None:
    """Hash ảnh **trước** của bước kế tiếp — tín hiệu "màn hình có đổi sau hành động này"."""
    if not isinstance(ui_hash_by_step, dict) or step_number is None:
        return None
    later = [k for k in ui_hash_by_step if k > int(step_number)]
    if not later:
        return None
    return ui_hash_by_step[min(later)]


def classify_step(step: dict, *, image_hash=None, ui_hash_by_step=None) -> str:
    """Phân loại một lượt đã ghi thành loại mà watchdog cần phân biệt.

    Trạng thái UI được quyết định bằng **hash nội dung ảnh** (``image_hash(tên_ảnh) -> hash|None``),
    KHÔNG bằng so sánh tên file: hai ảnh khác tên nhưng cùng nội dung là **unchanged**, và thiếu ảnh
    (hoặc không hash được) là **``ui_unknown``** — không bao giờ mặc định là tiến triển.
    """
    if not step.get("action_taken"):
        return "no_action"  # gồm cả lượt chỉ chờ retry 429 (không phát action nào)
    raw = step.get("last_execution_result")
    text = json.dumps(raw, ensure_ascii=False) if isinstance(raw, dict) else str(raw or "")
    if "Not executed" in text:
        return "gate_blocked"  # bị gate ledger chặn: chưa từng tới thiết bị
    status = _execution_status(step)
    if status == "dispatched":
        # Nguồn 1 (ưu tiên): hash ảnh **trước** của bước này so với bước kế tiếp — không cần ảnh sau
        # (``post_image_name`` thường rỗng), vẫn đúng nghĩa "màn hình có đổi sau hành động".
        step_number = step.get("step_number", step.get("step"))
        current = _hash_for_step(ui_hash_by_step, step_number)
        following = _next_step_hash(ui_hash_by_step, step_number)
        if current and following:
            return UI_CHANGED if current != following else UI_UNCHANGED
        # Nguồn 2: hash nội dung hai ảnh trước/sau của chính bước đó.
        if image_hash is None:
            return UI_UNKNOWN
        try:
            pre = image_hash(step.get("pre_image_name"))
            post = image_hash(step.get("post_image_name"))
        except Exception:  # noqa: BLE001 - lỗi đọc ảnh chỉ làm trạng thái thành unknown
            return UI_UNKNOWN
        if not pre or not post:
            return UI_UNKNOWN
        return UI_CHANGED if pre != post else UI_UNCHANGED
    if status in ("repeat_blocked", "already_applied", "failed"):
        return status
    return "unknown"


def progress_streak(
    steps: Iterable[dict], *, image_hash=None, ui_hash_by_step=None
) -> tuple[int, Counter]:
    """Đếm chuỗi lượt **không tiến triển thật** gần nhất (bỏ qua lượt trung tính)."""
    kinds: Counter = Counter()
    streak = 0
    for step in reversed(list(steps or [])):
        kind = classify_step(
            step, image_hash=image_hash, ui_hash_by_step=ui_hash_by_step
        )
        if kind in PROGRESS_KINDS:
            break
        if kind in NEUTRAL_KINDS:
            continue
        streak += 1
        kinds[kind] += 1
    return streak, kinds


def should_emit(streak: int, threshold: int = PROGRESS_WATCHDOG_TURNS) -> bool:
    """Chỉ gửi ở đúng mốc ngưỡng rồi mỗi ``threshold`` lượt — không lặp vô hạn, không spam."""
    if threshold <= 0 or streak < threshold:
        return False
    return (streak - threshold) % threshold == 0


def watchdog_message(
    streak: int,
    kinds: Counter,
    leaf_text: str | None,
    *,
    threshold: int = PROGRESS_WATCHDOG_TURNS,
) -> str | None:
    """Yêu cầu **cụ thể** cho Operator khi sub-goal không tiến triển thật; ``None`` khi chưa tới mốc."""
    if not should_emit(streak, threshold):
        return None
    detail = ", ".join(f"{kind} ×{count}" for kind, count in sorted(kinds.items()))
    leaf = " ".join((leaf_text or "").split())[:80] or "(chưa xác định)"
    return (
        f"[progress-watchdog] Sub-goal «{leaf}» đã {streak} lượt liên tiếp **không có tiến triển"
        f" thật** ({detail}). Bắt buộc, theo thứ tự:\n"
        "1. Đọc lại màn hình hiện tại (các phần tử thật đang có) và KHÔNG tap lại toạ độ cũ.\n"
        "2. Xác định mục tiêu mới từ những gì đang thấy.\n"
        "3. Nếu UI khác kỳ vọng: cập nhật `task_plan` (đổi sub-goal hoặc ghi lại kết quả thật).\n"
        "4. Không tự đánh `[x]` khi chưa có bằng chứng; không bỏ tiêu chí `verify`/`assert`."
    )


def observation_block(
    request: str | None,
    streak: int,
    kinds: Counter,
    leaf_text: str | None,
    *,
    threshold: int = PROGRESS_WATCHDOG_TURNS,
) -> str | None:
    """Ghép khối yêu cầu gửi Operator: chỉ dẫn phục hồi của retry guard + watchdog tiến triển.

    Tách thành hàm thuần để kiểm được nội dung mà không cần dựng OperatorNode.
    """
    parts: list[str] = []
    if request and str(request).strip():
        parts.append(str(request).strip())
    message = watchdog_message(streak, kinds, leaf_text, threshold=threshold)
    if message:
        parts.append(message)
    return "\n\n".join(parts) if parts else None
