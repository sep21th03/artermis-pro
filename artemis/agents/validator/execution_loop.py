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

"""Per-turn action execution loop for the Validator.

Two execution tiers, selected by how many turn-ending actions the Operator
emitted in the turn:

* **Vetted single action** (one action): the pre-execution Safety Net runs
  first (XML-first with pixel fallback), then the action executes locally with
  a short retry loop.
* **Fast-action burst** (two or more actions): the actions fire back to back
  with no safety net, no retries and no screenshots in between. The burst is
  the Operator's tool against turn latency (transient menus, toasts, control
  bars); the Operator owns the risk. The first failure aborts the rest.

Either way, a failure does not spawn a repair agent. The loop opens an
*execution incident* (:mod:`artemis.agents.validator.incidents`) that rides in
graph state and in the step's execution report, and the Operator resolves it
on its next turn with the live screen in front of it.

Extracted from ``validator.py``; the public entry point remains
``ValidatorNode.__call__``, which delegates here. All patchable seams
(_exec_action, _validate_action_precondition*, _get_initial_screenshot, ...)
are dispatched through the node so existing test patches keep working.
"""

import asyncio
import base64
from dataclasses import dataclass
import time
import uuid
from uuid import UUID

from artemis.agents.validator.categories import ValidationErrorCategory
from artemis.agents.validator.incidents import (
    KIND_EXEC_ERROR,
    KIND_SAFETY_NET,
    open_incident,
)
from artemis.data_engine.trace import CURRENT_TRACE_ID
from artemis.graph.state import State
from artemis.utils.coordinates import normalize_action_dict
from artemis.utils.task_tree import format_action_clean
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

#: Hành động nào thì bị chặn khi lặp lại y hệt. Cuộn/vuốt lặp lại là bình thường
#: (cuộn xuống nhiều lần để tới cuối danh sách), còn tap cùng một chỗ mãi thì không.
REPEAT_GUARDED_ACTIONS = frozenset({"tap", "long_press"})

#: Số lần liên tiếp một hành động giống hệt nhau được phép chạy trước khi bị chặn.
#: Đo trên trace thật (``232d2382``): agent tap cùng toạ độ ``[135,631][586,670]``
#: **10 lần liên tiếp**, màn hình không đổi, mỗi lần tốn 4-10 lượt LLM — cả lần chạy
#: 47 lượt trong 2m35s. Chặn ở lần thứ 3 để agent buộc phải đọc lại màn hình.
REPEAT_GUARD_LIMIT = 3

#: Số hành động gần nhất được ghi nhớ để so trùng.
REPEAT_GUARD_HISTORY = 8


def _repeat_action_key(action_name: str, action_item: dict) -> str:
    """Khoá nhận dạng "cùng một hành động".

    Toạ độ được làm tròn về bội số 10: hai lần tap lệch nhau 1-2 pixel vẫn là cùng
    một chỗ, còn lệch sang hàng khác thì không.
    """
    coords = action_item.get("coordinates") or []
    if coords:
        try:
            rounded = ",".join(str(int(round(float(c) / 10.0)) * 10) for c in list(coords)[:4])
            return f"{action_name}@{rounded}"
        except (TypeError, ValueError):
            pass
    target = action_item.get("target_text") or action_item.get("text") or ""
    return f"{action_name}:{str(target).strip()[:80]}"


def repeat_loop_reason(node, action_name: str, action_item: dict) -> str | None:
    """Ghi nhận hành động này; trả về lý do nếu nó đang lặp vô ích.

    **Mọi** hành động đều được ghi vào lịch sử, kể cả loại không bị chặn — một lần
    cuộn xen giữa hai lần tap nghĩa là agent vừa làm việc khác, nên chuỗi lặp phải
    đứt. Chỉ *chặn* những hành động trong ``REPEAT_GUARDED_ACTIONS``.

    Lịch sử để trên ``ctx`` chứ không trên ``State``: ``State`` là ``extra="forbid"``
    và có manifest kiểm soát hiển thị, còn ``ArtemisContext`` thì mở.
    """
    ctx = getattr(node, "ctx", None)
    if ctx is None:
        return None

    key = _repeat_action_key(action_name, action_item)
    history = getattr(ctx, "repeat_action_history", None)
    history = list(history) if isinstance(history, list) else []
    history.append(key)
    del history[:-REPEAT_GUARD_HISTORY]
    try:
        ctx.repeat_action_history = history
    except (AttributeError, TypeError, ValueError):
        return None

    if action_name not in REPEAT_GUARDED_ACTIONS:
        return None
    if len(history) < REPEAT_GUARD_LIMIT:
        return None
    tail = history[-REPEAT_GUARD_LIMIT:]
    if len(set(tail)) != 1:
        return None
    return (
        f"Hành động '{action_name}' đã chạy y hệt {REPEAT_GUARD_LIMIT} lần liên tiếp "
        f"tại cùng một chỗ ({key}) mà màn hình không thay đổi. KHÔNG lặp lại nữa: "
        "đọc lại màn hình hiện tại rồi chọn phần tử khác, hoặc báo blocked kèm mô tả "
        "chính xác màn hình đang thấy."
    )


#: Số lần **thất bại thật** tương đương (cùng fingerprint, UI không đổi) trước khi hệ thống yêu cầu
#: Operator quan sát lại màn hình.
REPEAT_OBSERVE_AFTER = 2

#: Số lần thất bại thật tương đương trước khi **chặn** hành động đó. Chặn là sticky theo fingerprint:
#: UI, target hoặc sub-goal đổi ⇒ fingerprint đổi ⇒ được phép thử lại. Ngưỡng 3 nghĩa là **lần thử
#: thứ 3** bị chặn (2 thất bại thật ⇒ lần thử kế tiếp không được dispatch).
REPEAT_BLOCK_AFTER = 3

#: Số lượt liên tiếp bị chặn mà Operator **không** thay đổi gì trước khi hệ thống tự mở chặn để phục
#: hồi. Đây **không** phải "mở khoá theo thời gian": mở chặn chỉ để hành động đi tiếp vào **safety
#: gate không đổi** (gate vẫn có thể từ chối), và mục đích là chặn vòng lặp chỉ toàn `repeat_blocked`.
REPEAT_BLOCK_RECOVERY_TURNS = 2

#: Cùng ngữ nghĩa trên nhưng khi **không xác định được UI fingerprint** (thiếu/không hash được ảnh):
#: bằng chứng yếu hơn ⇒ phục hồi sớm hơn, không giữ chặn lâu mà không có căn cứ.
REPEAT_BLOCK_RECOVERY_TURNS_UNKNOWN_UI = 1


def _b64_bytes(data: str | None) -> bytes | None:
    if not data:
        return None
    try:
        import base64

        return base64.b64decode(data, validate=False)
    except Exception:  # noqa: BLE001
        return None


def action_ui_fingerprint(pre_screenshot_b64: str | None) -> str | None:
    """dHash của ảnh **trước** hành động — "UI fingerprint" của retry guard."""
    try:
        from artemis.utils.image_hash import dhash_hex

        return dhash_hex(_b64_bytes(pre_screenshot_b64))
    except Exception:  # noqa: BLE001 - thiếu hash chỉ làm guard kém nhạy, không được gãy run
        return None


def active_subgoal_text(state) -> str:
    """Văn bản sub-goal đang ``[/]`` trong ``task_plan`` (rỗng khi không đọc được)."""
    try:
        from artemis.utils.plan_grammar import parse_plan

        snapshot = parse_plan(getattr(state, "task_plan", "") or "")
        milestone = snapshot.active_milestone()
        if milestone is None:
            return ""
        leaf = snapshot.active_leaf(milestone)
        return " ".join((leaf or milestone).text.split())
    except Exception:  # noqa: BLE001
        return ""


def repeat_fingerprint(
    action_name: str,
    action_item: dict,
    *,
    ui_fingerprint: str | None = None,
    subgoal: str = "",
) -> str:
    """Khoá nhận dạng "cùng một hành động trong cùng một ngữ cảnh".

    Gồm: loại action · văn bản target / id phần tử · toạ độ (làm tròn 10 px) · **UI fingerprint** ·
    **sub-goal đang làm**. Nhờ hai thành phần sau, cùng một cú tap trên màn hình **đã đổi** hoặc ở
    sub-goal khác được coi là hành động **mới** — không chặn oan.
    """
    parts = [action_name]
    target = action_item.get("target_text") or action_item.get("target_resource_id")
    if target:
        parts.append(" ".join(str(target).split())[:60])
    coords = action_item.get("coordinates") or []
    if coords:
        try:
            parts.append(",".join(str(int(round(float(c) / 10.0)) * 10) for c in list(coords)[:2]))
        except (TypeError, ValueError):
            pass
    parts.append(f"ui={ui_fingerprint or '-'}")
    parts.append(f"sg={' '.join(subgoal.split())[:60]}")
    return "|".join(parts)


def _repeat_guard_state(ctx) -> dict:
    guard_state = getattr(ctx, "action_retry_state", None)
    if not isinstance(guard_state, dict):
        guard_state = {}
        try:
            ctx.action_retry_state = guard_state
        except (AttributeError, TypeError, ValueError):
            pass
    return guard_state


def repeat_guard_verdict(node, fingerprint: str, *, ui_fingerprint, subgoal) -> str | None:
    """Lý do **chặn** (sticky) khi fingerprint này đã thất bại thật đủ nhiều lần trên cùng UI/sub-goal."""
    ctx = getattr(node, "ctx", None)
    if ctx is None:
        return None
    entry = _repeat_guard_state(ctx).get(fingerprint)
    if not isinstance(entry, dict):
        return None
    if entry.get("ui") != ui_fingerprint or entry.get("subgoal") != subgoal:
        return None  # UI/sub-goal đã đổi ⇒ coi như hành động mới
    failures = int(entry.get("failures") or 0)
    if failures + 1 < REPEAT_BLOCK_AFTER:
        return None  # lần thử này chưa phải lần thứ ``REPEAT_BLOCK_AFTER``
    # Phục hồi an toàn: nếu Operator đã bị chặn liên tiếp mà **không đổi gì**, mở chặn để hành động
    # đi tiếp vào **safety gate không đổi** (gate vẫn có thể từ chối). Nhờ vậy không có vòng lặp chỉ
    # toàn `repeat_blocked`, và việc mở chặn dựa trên *số lượt bị chặn bỏ qua*, không dựa trên thời gian.
    limit = (
        REPEAT_BLOCK_RECOVERY_TURNS_UNKNOWN_UI
        if ui_fingerprint is None
        else REPEAT_BLOCK_RECOVERY_TURNS
    )
    if int(entry.get("blocked_turns") or 0) >= limit:
        _request_recovery(ctx, fingerprint, failures, ui_fingerprint is None)
        return None
    return (
        f"Hành động này đã **thất bại thật {failures} lần** ở cùng một chỗ mà màn hình không đổi."
        " KHÔNG chạy lại: đọc lại màn hình hiện tại, xác định phần tử/mục tiêu khác, cập nhật"
        " `task_plan` nếu UI khác kỳ vọng, rồi chọn hành động mới."
    )


def note_blocked_attempt(node, fingerprint: str, *, ui_fingerprint, subgoal) -> int:
    """Ghi một lượt **bị chặn** (không dispatch) cho fingerprint này; trả về số lượt đã bị chặn."""
    ctx = getattr(node, "ctx", None)
    if ctx is None:
        return 0
    entry = _repeat_guard_state(ctx).get(fingerprint)
    if not isinstance(entry, dict):
        return 0
    if entry.get("ui") != ui_fingerprint or entry.get("subgoal") != subgoal:
        return 0  # ngữ cảnh đã đổi ⇒ bộ đếm chặn không còn nghĩa
    blocked = int(entry.get("blocked_turns") or 0) + 1
    entry["blocked_turns"] = blocked
    return blocked


def _request_recovery(ctx, fingerprint: str, failures: int, unknown_ui: bool) -> None:
    """Yêu cầu phục hồi **cụ thể** khi chặn được mở (không phải mở khoá theo thời gian)."""
    try:
        ctx.repeat_observation_request = (
            f"Hành động `{fingerprint.split('|')[0]}` đã bị chặn nhiều lượt liên tiếp sau {failures}"
            " lần thất bại thật" + (" (không xác định được UI fingerprint)" if unknown_ui else "")
            + ". Chặn đã được MỞ để hành động đi tiếp qua safety gate — nhưng bắt buộc: (1) quan sát"
            " lại màn hình hiện tại; (2) chọn mục tiêu khác hoặc cách tiếp cận khác; (3) cập nhật"
            " `task_plan` nếu UI khác kỳ vọng. Lặp lại y hệt sẽ tiếp tục bị chặn."
        )
    except (AttributeError, TypeError, ValueError):
        pass


#: Các loại kết quả một lần thử có thể có, theo đúng cách retry counter phải xử lý.
OUTCOME_FAILED = "failed"  # dispatch lỗi hoặc safety net từ chối ⇒ **tăng** bộ đếm
OUTCOME_PROGRESS = "progress"  # bằng chứng tiến triển thật (UI đổi/màn hình đã chuyển) ⇒ **reset**
OUTCOME_NO_PROGRESS = "no_progress"  # dispatch xong nhưng UI không đổi ⇒ giữ nguyên bộ đếm
OUTCOME_NEUTRAL = "neutral"  # gate chặn / không dispatch / chặn lặp ⇒ giữ nguyên bộ đếm


def note_action_outcome(
    node, fingerprint: str, *, outcome: str, ui_fingerprint, subgoal
) -> int:
    """Ghi kết quả **thật** của một lần thử; trả về số thất bại thật tích luỹ của fingerprint.

    Bất biến (yêu cầu rà soát):
      * ``failed`` ⇒ tăng bộ đếm.
      * ``progress`` (đã có bằng chứng tiến triển thật) ⇒ **xoá** bộ đếm.
      * ``no_progress`` (dispatch nhưng UI không đổi) ⇒ **không tăng, không xoá**.
      * ``neutral`` (gate ledger chặn, không dispatch, chặn lặp, burst bỏ qua) ⇒ **không tăng, không
        xoá** — các trạng thái này không được tự reset bộ đếm.
    """
    ctx = getattr(node, "ctx", None)
    if ctx is None:
        return 0
    guard_state = _repeat_guard_state(ctx)
    if outcome == OUTCOME_PROGRESS:
        guard_state.pop(fingerprint, None)
        return 0
    if outcome in (OUTCOME_NO_PROGRESS, OUTCOME_NEUTRAL):
        entry = guard_state.get(fingerprint)
        return int(entry.get("failures") or 0) if isinstance(entry, dict) else 0
    entry = guard_state.get(fingerprint)
    same_context = (
        isinstance(entry, dict)
        and entry.get("ui") == ui_fingerprint
        and entry.get("subgoal") == subgoal
    )
    failures = (int(entry.get("failures") or 0) + 1) if same_context else 1
    guard_state[fingerprint] = {"failures": failures, "ui": ui_fingerprint, "subgoal": subgoal}
    if failures >= REPEAT_OBSERVE_AFTER:
        # Yêu cầu quan sát lại — Operator đọc ở lượt kế tiếp (kênh của progress watchdog).
        try:
            ctx.repeat_observation_request = (
                f"Hành động `{fingerprint.split('|')[0]}` đã **thất bại thật {failures} lần liên tiếp**"
                " trên cùng một màn hình. Quan sát lại UI trước khi chọn hành động tiếp theo: đọc các"
                " phần tử đang có, xác định mục tiêu mới, cập nhật `task_plan` nếu UI khác kỳ vọng."
                " KHÔNG tap lại cùng toạ độ."
            )
        except (AttributeError, TypeError, ValueError):
            pass
    return failures



#: ``attempts`` marker for actions a burst never reached.
BURST_SKIPPED_MARKER = "Skipped (burst aborted)"


@dataclass
class _ActionOutcome:
    """Result of processing a single action item (pre-check + execution)."""

    action_item: dict
    success: bool = False
    error_msg: str = ""
    error_category: ValidationErrorCategory = ValidationErrorCategory.GENERAL
    intercepted: bool = False
    #: Target biến mất vì màn hình **đã chuyển đúng hướng** hành động (dialog đóng, sang màn mới).
    #: Hành động không được thực thi nhưng **không** phải thất bại: không mở incident, không đếm vào
    #: chuỗi lỗi, và thông điệp cho Operator là hướng dẫn quan sát lại (xem session d110b1ef).
    already_applied: bool = False
    #: Bị **retry guard** chặn vì cùng hành động đã thất bại thật nhiều lần trên cùng màn hình.
    #: Không phải thất bại và không phải safety net: không mở incident, không tính vào chuỗi lỗi.
    repeat_blocked: bool = False
    post_screenshot_b64: str | None = None


async def _run_precondition_gate(
    node, session, action_item: dict, state: State, pre_screenshot_b64: str, original_coords: list
) -> tuple[bool, ValidationErrorCategory, str]:
    """Runs the pre-execution Safety Net: XML-first with pixel fallback."""
    is_explorer_candidate = action_item.get("target_class") == "ExplorerCandidate"
    is_ocr_element = "[OCR]" in str(action_item.get("target_class") or "")
    has_index_metadata = bool(
        (
            action_item.get("target_text")
            or action_item.get("target_bounds")
            or action_item.get("target_resource_id")
        )
        and not is_explorer_candidate
        and not is_ocr_element
    )

    async def _pixel_check() -> tuple[bool, ValidationErrorCategory, str]:
        started = time.time()
        verdict = await node._validate_action_precondition_pixel(
            session, action_item, pre_screenshot_b64, original_coords, state=state
        )
        # Số đo: thời gian pixel check (chỉ cộng số, không đổi luồng).
        try:
            from artemis.utils.step_metrics import add_validation

            add_validation(node.ctx, total_ms=0.0, pixel_ms=(time.time() - started) * 1000.0)
        except Exception:  # noqa: BLE001 - đo không được làm gãy lượt
            pass
        return verdict

    if not has_index_metadata:
        # Pure coordinate/non-index interactions: pixel-based validation is the only gate.
        return await _pixel_check()

    # Index-based validation is fastest and safest when metadata is present
    _gate_started = time.time()
    (
        validation_passed,
        validation_category,
        validation_error,
    ) = await node._validate_action_precondition(session, action_item, state=state)
    try:
        from artemis.utils.step_metrics import add_validation

        add_validation(node.ctx, total_ms=(time.time() - _gate_started) * 1000.0)
    except Exception:  # noqa: BLE001
        pass

    if validation_passed:
        return validation_passed, validation_category, validation_error

    if validation_category == ValidationErrorCategory.XML_BYPASSED:
        logger.info(
            f"Pre-execution XML-based validation bypassed ({validation_error})."
            " Falling back to Pixel-based validation..."
        )
        return await _pixel_check()

    logger.info(
        f"Pre-execution XML-based validation failed: {validation_error}."
        " Attempting Pixel-based validation fallback..."
    )
    pixel_passed, pixel_category, pixel_error = await _pixel_check()
    if pixel_passed and pixel_category == ValidationErrorCategory.NONE:
        logger.success("Pixel validation confirmed the target after XML validation failed.")
        return True, ValidationErrorCategory.NONE, ""

    # XML fail **không** được phép nuốt verdict của pixel fallback. Đo trên máy thật 30/10/2026
    # (session 93882dc3, bước đăng xuất): XML báo "bị 'Quên mật khẩu' chiếm chỗ", pixel chứng minh
    # ``target_state=already_applied`` (dialog đã đóng, màn Đăng nhập hiện ra) — nhưng vì hàm trả về
    # category của XML nên action bị tính **failed + mở incident** dù nó đã thành công, và Operator
    # có thể tap lại chính chỗ đó. Chỉ verdict ``PIXEL_ALREADY_APPLIED`` được truyền qua: mọi verdict
    # còn lại (blocked / target biến mất / confidence thấp / ảnh không đổi) vẫn giữ nguyên thất bại
    # của XML ⇒ **không nới lỏng** safety gate.
    if pixel_category == ValidationErrorCategory.PIXEL_ALREADY_APPLIED:
        logger.info(
            "Pixel fallback proved the target was already applied;"
            " propagating that verdict to execution."
        )
        return pixel_passed, pixel_category, pixel_error

    return validation_passed, validation_category, validation_error


async def _attempt_local_execution(
    node, session, action_item: dict, action_name, attempts_log: list, *, max_local_retries: int
) -> tuple[bool, str]:
    """Executes the action locally with a short retry loop."""
    success = False
    error_msg = ""
    for attempt in range(max_local_retries):
        try:
            exec_success, exec_error = await node._exec_action(session, action_item)
        except Exception as e:
            exec_success = False
            exec_error = str(e)

        # No effect polling: "dispatched" only says the device accepted the
        # command; whether it had the intended effect is the Operator's call.
        if exec_success:
            success = True
            error_msg = ""
            attempts_log.append("Dispatched")
            break
        else:
            success = False
            error_msg = exec_error
            attempts_log.append(error_msg)

        if attempt < max_local_retries - 1:
            await asyncio.sleep(0.5)

    return success, error_msg


async def _capture_live_screenshot(session, context_desc: str) -> str | None:
    try:
        return await session.screenshot_b64()
    except Exception as e:
        logger.error(f"Failed to capture {context_desc}: {e!r}")
        return None


async def _process_action(
    node,
    state: State,
    session,
    action_item: dict,
    action_name,
    pre_screenshot_b64: str,
    *,
    burst: bool,
) -> _ActionOutcome:
    """Runs precondition checks (vetted tier only) and execution for one action item."""
    action_item = dict(action_item)  # Make a copy to avoid mutating original state actions
    outcome = _ActionOutcome(action_item=action_item)
    attempts_log = []

    # 0. Chặn vòng lặp: cùng một hành động chạy y hệt ở cùng một chỗ, lặp mãi mà màn
    #    hình không đổi. Không áp cho burst — burst là chuỗi hành động liên tiếp có
    #    chủ đích, cắt giữa chuỗi sẽ phá timing của nó.
    ui_fp: str | None = None
    subgoal = ""
    fingerprint = ""
    if not burst:
        ui_fp = action_ui_fingerprint(pre_screenshot_b64)
        subgoal = active_subgoal_text(state)
        fingerprint = repeat_fingerprint(
            action_name, action_item, ui_fingerprint=ui_fp, subgoal=subgoal
        )
        repeat_reason = repeat_guard_verdict(
            node, fingerprint, ui_fingerprint=ui_fp, subgoal=subgoal
        )
        if repeat_reason:
            # **Chặn** (không dispatch) nhưng KHÔNG phải thất bại: không mở incident, không đếm vào
            # chuỗi lỗi. Guard chạy trước safety gate và chỉ chặn — không bao giờ cho action đi qua
            # gate, nên không nới lỏng an toàn.
            outcome.success = False
            outcome.intercepted = False
            outcome.repeat_blocked = True
            outcome.error_msg = repeat_reason
            outcome.error_category = ValidationErrorCategory.REPEAT_LOOP
            attempts_log.append(repeat_reason)
            action_item["attempts"] = list(attempts_log)
            note_blocked_attempt(
                node, fingerprint, ui_fingerprint=ui_fp, subgoal=subgoal
            )
            logger.warning(f"Chặn hành động lặp vô ích (sticky): {repeat_reason}")
            return outcome

    validation_passed = True
    if not burst:
        # 1. Pre-execution validation (Safety Net)
        original_coords = list(action_item.get("coordinates") or [])
        (
            validation_passed,
            validation_category,
            validation_error,
        ) = await _run_precondition_gate(
            node, session, action_item, state, pre_screenshot_b64, original_coords
        )
        if not validation_passed:
            if validation_category == ValidationErrorCategory.PIXEL_ALREADY_APPLIED:
                # Target biến mất vì màn hình đã chuyển đúng hướng hành động: **không thực thi**
                # (toạ độ cũ vô nghĩa) nhưng cũng **không** báo thất bại — nếu báo thất bại, agent sẽ
                # lặp lại chính cú tap đó (đo được: 3 lần trong session d110b1ef).
                outcome.success = False
                outcome.intercepted = False
                outcome.already_applied = True
                outcome.error_msg = validation_error
                outcome.error_category = validation_category
                attempts_log.append(validation_error)
                logger.info(f"Hành động không cần thiết (màn hình đã đổi): {validation_error}")
            else:
                outcome.success = False
                outcome.intercepted = True
                outcome.error_msg = f"Pre-execution validation failed: {validation_error}"
                outcome.error_category = validation_category
                attempts_log.append(outcome.error_msg)

    if validation_passed:
        # 2. Local execution: a vetted action gets a short retry loop, a burst
        # member fires exactly once (retries would break the burst's timing).
        if burst:
            max_local_retries = 1
        else:
            max_local_retries = 1 if action_name == "launch_app" else 2
        outcome.success, outcome.error_msg = await _attempt_local_execution(
            node,
            session,
            action_item,
            action_name,
            attempts_log,
            max_local_retries=max_local_retries,
        )

    if not burst and fingerprint:
        # Phân loại tường minh: chỉ ``failed`` mới tăng bộ đếm; chỉ **bằng chứng tiến triển thật**
        # (``already_applied``) mới reset; ``no_progress``/``neutral`` giữ nguyên bộ đếm. Một lần
        # dispatch "thành công" chưa phải bằng chứng tiến triển — UI có đổi hay không do dHash của
        # lượt sau quyết định (khi đó ngữ cảnh đổi và bộ đếm tự bắt đầu lại).
        if outcome.repeat_blocked:
            outcome_kind = OUTCOME_NEUTRAL
        elif outcome.already_applied:
            outcome_kind = OUTCOME_PROGRESS
        elif not outcome.success:
            outcome_kind = OUTCOME_FAILED
        else:
            outcome_kind = OUTCOME_NO_PROGRESS
        note_action_outcome(
            node, fingerprint, outcome=outcome_kind, ui_fingerprint=ui_fp, subgoal=subgoal
        )

    if not outcome.success and not outcome.already_applied and not outcome.repeat_blocked:
        logger.info("Action failed, capturing failure screenshot...")
        outcome.post_screenshot_b64 = await _capture_live_screenshot(session, "failure screenshot")

    # Enrich the executed action record
    if len(attempts_log) > 1 or not outcome.success:
        action_item["attempts"] = list(attempts_log)

    return outcome


def _absorb_post_screenshot(ctx, state: State, post_screenshot_b64: str) -> str | None:
    """Persists the post-action screenshot and points state at it.

    Returns the stored image name (None when no data engine is attached).
    """
    decoded_bytes = base64.b64decode(post_screenshot_b64)
    logger.info(f"Successfully decoded post_screenshot_b64 ({len(decoded_bytes)} bytes)")
    if not ctx.data_engine:
        return None

    post_image_name = ctx.data_engine.get_or_create_image(decoded_bytes)
    screenshot_path = str(ctx.data_engine.get_image_path(post_image_name))
    state.latest_screenshot = screenshot_path
    logger.info(f"Validator updated state.latest_screenshot to: {screenshot_path}")
    return post_image_name


def _record_action_start(ctx, step_id, action_name, action_item, parent_id, action_trace_id):
    if ctx.data_engine and step_id:
        ctx.data_engine.record_trace(
            type="action",
            name=action_name,
            payload={"action": action_item, "status": "running"},
            status="running",
            parent_trace_id=parent_id,
            step_id=step_id,
            trace_id=action_trace_id,
        )


def _record_action_end(
    ctx,
    step_id,
    action_name,
    outcome: _ActionOutcome,
    post_image_name,
    parent_id,
    action_trace_id,
    start_time: float,
    *,
    burst: bool,
):
    duration = time.time() - start_time
    if ctx.data_engine and step_id:
        relative_time = ctx.data_engine.get_relative_time(time.time())
        ctx.data_engine.record_trace(
            type="action",
            name=action_name,
            payload={
                "action": outcome.action_item,
                "success": outcome.success,
                "error_msg": outcome.error_msg,
                "post_screenshot": post_image_name,
                "timestamp": time.time(),
                "relative_time": relative_time,
                "burst": burst,
                "already_applied": outcome.already_applied,
            },
            status=(
                "success"
                if outcome.success
                else ("already_applied" if outcome.already_applied else "failed")
            ),
            duration=duration,
            parent_trace_id=parent_id,
            step_id=step_id,
            trace_id=action_trace_id,
        )


def _normalize_point(ctx, point) -> list[int] | None:
    """Pixel [x, y] -> normalized 0-1000 [x, y] using the device dims."""
    try:
        x, y = int(point[0]), int(point[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    width = getattr(getattr(ctx, "device", None), "device_width", None) or 1080
    height = getattr(getattr(ctx, "device", None), "device_height", None) or 2400
    return [
        max(0, min(1000, round(x * 1000 / width))),
        max(0, min(1000, round(y * 1000 / height))),
    ]


def _incident_evidence(ctx, outcome: _ActionOutcome) -> dict:
    """Structured facts the safety net attached to the action item, normalized."""
    raw = outcome.action_item.pop("safety_net_evidence", None) or {}
    evidence: dict = {}
    if raw.get("new_center"):
        normalized = _normalize_point(ctx, raw["new_center"])
        if normalized:
            evidence["new_location"] = normalized
        if raw.get("new_bounds") is not None:
            evidence["new_bounds"] = raw["new_bounds"]
    if raw.get("occupant"):
        evidence["occupant"] = raw["occupant"]
        if raw.get("occupant_bounds") is not None:
            evidence["occupant_bounds"] = raw["occupant_bounds"]
    return evidence


def _current_step_number(ctx, state: State) -> int | None:
    step_id = getattr(state, "current_step_id", None)
    if ctx.data_engine and step_id:
        try:
            return ctx.data_engine.get_step_number(UUID(step_id))
        except Exception:
            return None
    return None


def _model_facing_action(ctx, action_item: dict) -> dict:
    """The action item in the Operator's own 0–1000 coordinate space.

    The live item carries the controller's pixels; every phrase rendered for
    a model is normalized first, and the space marker makes that idempotent.
    """
    width = getattr(getattr(ctx, "device", None), "device_width", None) or 1080
    height = getattr(getattr(ctx, "device", None), "device_height", None) or 2400
    return normalize_action_dict(action_item, width, height)


def _build_incident(ctx, state: State, outcome: _ActionOutcome, *, index: int, total: int) -> dict:
    step_number = _current_step_number(ctx, state)
    return open_incident(
        previous=getattr(state, "open_incident", None),
        kind=KIND_SAFETY_NET if outcome.intercepted else KIND_EXEC_ERROR,
        category=outcome.error_category,
        reason=outcome.error_msg,
        action_item=outcome.action_item,
        action_description=format_action_clean(_model_facing_action(ctx, outcome.action_item)),
        action_index=index,
        burst_size=total,
        step_number=step_number,
        evidence=_incident_evidence(ctx, outcome),
    )


async def run_validation_loop(node, state: State) -> dict:
    """Executes all pending Operator decisions and returns the execution report.

    ``node`` is the ValidatorNode facade; every overridable seam is invoked
    through it.
    """
    ctx = node.ctx

    actions, error_msg = node._parse_decisions(state.structured_decisions)
    if error_msg:
        return {}

    execution: list[dict] = []
    actions_to_execute = list(actions)
    total_actions = len(actions_to_execute)
    burst = total_actions > 1
    if burst:
        logger.info(
            f"Fast-action burst: {total_actions} actions will fire back to back"
            " without the safety net."
        )

    session = await node._get_mcp_session()

    try:
        last_screenshot_b64, last_screenshot_name = await node._get_initial_screenshot(
            session, state
        )
        # Preserve the turn-initial screenshot (what the operator saw when making decisions)
        decision_screenshot_name = last_screenshot_name
    except Exception:
        return {}

    step_id = UUID(state.current_step_id) if state.current_step_id else None

    incident: dict | None = None
    success = False
    already_applied = False
    repeat_blocked = False
    index = 0

    while actions_to_execute:
        action_item = actions_to_execute.pop(0)
        action_name = action_item.get("action")

        logger.info(f"Executing action: {action_name}")

        action_trace_id = uuid.uuid4()
        parent_id = CURRENT_TRACE_ID.get()
        token = CURRENT_TRACE_ID.set(action_trace_id)
        start_time = time.time()

        _record_action_start(ctx, step_id, action_name, action_item, parent_id, action_trace_id)

        try:
            outcome = await _process_action(
                node, state, session, action_item, action_name, last_screenshot_b64, burst=burst
            )
            success = outcome.success
            execution.append(outcome.action_item)

            post_image_name = None
            if outcome.post_screenshot_b64:
                post_image_name = _absorb_post_screenshot(ctx, state, outcome.post_screenshot_b64)
                last_screenshot_b64 = outcome.post_screenshot_b64
                if post_image_name:
                    last_screenshot_name = post_image_name

            _record_action_end(
                ctx,
                step_id,
                action_name,
                outcome,
                post_image_name,
                parent_id,
                action_trace_id,
                start_time,
                burst=burst,
            )
        finally:
            CURRENT_TRACE_ID.reset(token)

        if outcome.already_applied:
            # Không phải thất bại: màn hình đã tự chuyển tới trạng thái mà hành động nhắm tới. Không
            # mở incident, không đếm vào chuỗi lỗi — chỉ ghi nhận để Operator quan sát lại.
            already_applied = True
            index += 1
            continue
        if outcome.repeat_blocked:
            # Retry guard chặn: không dispatch, KHÔNG mở incident và không tính là failed. Operator đã
            # nhận yêu cầu quan sát lại ở lượt kế tiếp.
            repeat_blocked = True
            index += 1
            continue
        if not success:
            incident = _build_incident(ctx, state, outcome, index=index, total=total_actions)
            logger.warning(
                f"Action failed: {outcome.action_item}. Opened execution incident"
                f" ({incident['kind']}/{incident['category']},"
                f" consecutive={incident['consecutive_failures']}) for the Operator."
            )
            break
        index += 1

    # Append any skipped actions to the execution log
    for skipped in actions_to_execute:
        skipped_copy = dict(skipped)
        skipped_copy["attempts"] = [BURST_SKIPPED_MARKER if burst else "Skipped"]
        execution.append(skipped_copy)

    report = {
        "execution": execution,
        "status": (
            "dispatched"
            if success
            else ("already_applied" if already_applied else ("repeat_blocked" if repeat_blocked else "failed"))
        ),
        "burst": burst,
        "incident": incident,
    }

    # Số đo của bước: phân loại rõ dispatched / already_applied / failed / blocked (không gọi LLM).
    try:
        from artemis.utils.step_metrics import finish_execution

        finish_execution(
            node.ctx,
            dispatched=sum(1 for item in execution if item.get("attempts") == ["Dispatched"])
            or (1 if success else 0),
            already_applied=1 if already_applied else 0,
            failed=0 if success or already_applied or repeat_blocked else 1,
            repeat_blocked=1 if repeat_blocked else 0,
        )
    except Exception as exc:  # noqa: BLE001 - đo hiệu năng không được làm gãy lượt
        logger.info(f"step-metrics: không chốt được số đo bước: {exc}")

    # Determine if a distinct post-action screenshot was captured
    distinct_post_image_name = (
        last_screenshot_name
        if (last_screenshot_name and last_screenshot_name != decision_screenshot_name)
        else None
    )

    if ctx.data_engine and step_id:
        ctx.data_engine.update_step_execution_result(
            step_id, report, post_image_name=distinct_post_image_name
        )

    # A successful turn closes whatever incident was open (handing the Operator
    # a one-turn CLOSED notice); a failed one keeps and escalates it.
    closed = None
    previous = getattr(state, "open_incident", None)
    if success and isinstance(previous, dict) and previous.get("kind"):
        closed = dict(previous)
        closed["closed_at_step"] = _current_step_number(ctx, state)
    return {
        "last_execution_result": report,
        "open_incident": incident,
        "last_closed_incident": closed,
    }
