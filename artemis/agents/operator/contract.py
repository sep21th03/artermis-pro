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

"""Hợp đồng thực thi: những luật **kiểm bằng code** trước khi action ra tới thiết bị.

**Vì sao phải là code.** Đo trên máy thật 29/10/2026 (task "đăng nhập 0999 → chuyển nhà Mesh Tạo 2
→ tạo ngữ cảnh chạy khi ấn"): prompt có đủ ràng buộc, nhưng agent vẫn tự nhập tài khoản, rồi tự
bấm Đăng xuất và bị safety net chặn 3 lần. Ràng buộc nằm trong văn bản thì *có thể* bị bỏ qua —
nó là lời khuyên. Cùng ràng buộc đó, nếu kiểm trước khi gửi action, là một cái cổng.

**Giai đoạn này chỉ GHI LOG.** ``ARTEMIS_CONTRACT_ENFORCE`` mặc định tắt: luật chạy, phán quyết
được ghi lại kèm lý do, nhưng action vẫn đi tiếp. Lý do không phải sự nhút nhát — một cổng chặn
nhầm sẽ làm hỏng những run đang chạy tốt, nên phải thu thập dương tính giả trên run thật trước
khi bật từng luật (đúng thứ tự đã chốt: log-only → thu thập → bật theo từng luật).

Module này là hàm thuần: không thiết bị, không LLM, không trạng thái ẩn — để luật test được bằng
đúng câu hỏi "đầu vào này có bị chặn không, và vì sao".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

#: Bật/tắt cưỡng chế. ``0``/rỗng ⇒ chỉ ghi log (mặc định).
ENFORCE_ENV = "ARTEMIS_CONTRACT_ENFORCE"

#: Danh sách luật được phép cưỡng chế, ngăn bằng dấu phẩy. Rỗng + ``ENFORCE_ENV=1`` ⇒ cưỡng chế tất.
RULES_ENV = "ARTEMIS_CONTRACT_RULES"

#: Thao tác nhạy cảm: đổi trạng thái không thể hoàn tác hoặc làm mất dữ liệu phiên. Chỉ được thực
#: hiện khi **chính công đoạn đang làm** nói tới nó (tester yêu cầu hoặc KB hướng dẫn).
SENSITIVE_ACTION_TERMS: tuple[str, ...] = (
    "đăng xuất",
    "logout",
    "log out",
    "sign out",
    "xoá tài khoản",
    "delete account",
    "xoá nhà",
    "xoá phòng",
    "xoá thiết bị",
    "remove device",
    "unpair",
    "huỷ ghép",
    "khôi phục cài đặt gốc",
    "factory reset",
    "reset",
)

#: Dấu hiệu một action là *nhập mật khẩu* (ô mật khẩu, không phải số điện thoại).
PASSWORD_TERMS: tuple[str, ...] = (
    "mật khẩu",
    "mat khau",
    "password",
    "passcode",
    "pin code",
    "mã pin",
)

#: Dấu hiệu tài khoản "đã lưu" (đăng nhập bằng tài khoản có sẵn, không gõ tay).
SAVED_ACCOUNT_TERMS: tuple[str, ...] = (
    "đã lưu",
    "da luu",
    "saved",
    "tài khoản đã từng đăng nhập",
    "tài khoản cũ",
    "danh sách tài khoản",
)

#: Dấu hiệu công đoạn **đóng** overlay (đóng modal/bottomsheet là một phần của workflow).
CLOSE_OVERLAY_TERMS: tuple[str, ...] = (
    "đóng",
    "close",
    "dismiss",
    "huỷ",
    "cancel",
    "quay lại",
    "back",
)


def enforcement_enabled() -> bool:
    """Có cưỡng chế hay không (mặc định: không)."""
    return (os.getenv(ENFORCE_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def enforced_rules() -> frozenset[str] | None:
    """Tập luật được cưỡng chế; ``None`` nghĩa là tất cả (khi bật mà không nêu danh sách)."""
    raw = (os.getenv(RULES_ENV) or "").strip()
    if not raw:
        return None
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def is_enforced(rule: str) -> bool:
    """Luật này có đang được cưỡng chế không (dùng cho cả ghi log lẫn chặn)."""
    if not enforcement_enabled():
        return False
    allowed = enforced_rules()
    return allowed is None or rule in allowed


@dataclass(frozen=True)
class Violation:
    """Một luật bị vi phạm: định danh luật, mức độ, và lý do đọc được."""

    rule: str
    reason: str
    severity: str = "block"
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "reason": self.reason,
            "severity": self.severity,
            "enforced": is_enforced(self.rule),
            **self.detail,
        }


@dataclass(frozen=True)
class ContractContext:
    """Những gì hợp đồng cần biết về lượt này.

    Mọi trường đều **do code** điền (plan, câu tester, KB), không phải do model tự khai — nếu model
    khai được "tôi được phép đăng xuất" thì hợp đồng vô nghĩa.
    """

    goal_text: str = ""
    subtask_text: str = ""
    allowed_actions: tuple[str, ...] = ()
    tester_provided_password: bool = False
    saved_account_required: bool = False
    open_overlay_expected: bool = False
    postcondition_verified: bool = False


def _contains(text: str, terms: tuple[str, ...]) -> str | None:
    """Trả về từ khoá đầu tiên xuất hiện trong ``text`` (đã chuẩn hoá thường), hoặc ``None``."""
    haystack = (text or "").lower()
    for term in terms:
        if term.lower() in haystack:
            return term
    return None


def _action_text(action: dict[str, Any]) -> str:
    """Toàn bộ chữ mô tả một action (target, mô tả, text nhập) để luật đọc."""
    parts: list[str] = []
    for key in ("target", "target_description", "text", "action", "type", "name"):
        value = action.get(key)
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


# ── Luật ────────────────────────────────────────────────────────────────
def rule_no_invented_password(action: dict[str, Any], ctx: ContractContext) -> Violation | None:
    """Không tự nhập mật khẩu khi tester không cấp mật khẩu.

    Đo trên máy thật: prompt có ``auth.switch_account`` ("đăng nhập bằng tài khoản đã lưu") mà agent
    vẫn xoá số cũ rồi gõ tay ``0999``. Luật này chặn đúng *hành vi*, không chặn việc đăng nhập.
    """
    if ctx.tester_provided_password:
        return None
    text = _action_text(action)
    term = _contains(text, PASSWORD_TERMS)
    if not term:
        return None
    # Ô tìm kiếm có chữ "password" trong nhãn (ví dụ "quên mật khẩu") không phải nhập mật khẩu.
    if not re.search(r"\b(input_text|set_text|type|nhập|enter)\b", text, re.IGNORECASE):
        return None
    return Violation(
        rule="no_invented_password",
        reason=(
            f"Nhập vào ô mật khẩu («{term}») khi tester không cấp mật khẩu"
            + (" và KB nói dùng tài khoản đã lưu" if ctx.saved_account_required else "")
            + ". Không được tự bịa mật khẩu."
        ),
        detail={"term": term},
    )


def rule_sensitive_action_needs_subtask(
    action: dict[str, Any], ctx: ContractContext
) -> Violation | None:
    """Thao tác nhạy cảm chỉ được làm khi **chính công đoạn** nói tới nó.

    Đăng xuất/xoá/ghép lại là những thao tác không hoàn tác được. Trong run đã đo, agent bấm Đăng
    xuất trong khi công đoạn là *chuyển nhà* — không có căn cứ nào từ plan hay KB cho nó.
    """
    text = _action_text(action)
    term = _contains(text, SENSITIVE_ACTION_TERMS)
    if not term:
        return None
    allowed_text = f"{ctx.subtask_text} {ctx.goal_text}".lower()
    if term.lower() in allowed_text:
        return None
    return Violation(
        rule="sensitive_action_out_of_scope",
        reason=(
            f"Thao tác nhạy cảm «{term}» không xuất hiện trong công đoạn đang làm"
            f" ({ctx.subtask_text[:80] or 'chưa xác định'})."
        ),
        detail={"term": term},
    )


def rule_close_overlay_only_when_intended(
    action: dict[str, Any], ctx: ContractContext
) -> Violation | None:
    """Không tự đóng overlay khi overlay là một phần của công đoạn (modal chuyển nhà)."""
    if not ctx.open_overlay_expected:
        return None
    text = _action_text(action)
    term = _contains(text, CLOSE_OVERLAY_TERMS)
    if not term:
        return None
    if term.lower() in ctx.subtask_text.lower():
        return None
    return Violation(
        rule="overlay_closed_unintended",
        reason=(
            f"Đóng overlay («{term}») trong khi công đoạn cần overlay mở ({ctx.subtask_text[:80]})."
        ),
        detail={"term": term},
    )


def rule_success_requires_evidence(
    action: dict[str, Any], ctx: ContractContext
) -> Violation | None:
    """Không tuyên bố hoàn thành công đoạn khi chưa có bằng chứng trên màn hình.

    Chỉ áp cho hành động *kết thúc lượt bằng tuyên bố đạt* (``report_task_status``/``finish``),
    không áp cho thao tác thường: nếu không, mọi lượt bấm đều bị coi là "tuyên bố thành công".
    """
    name = str(action.get("action") or action.get("name") or "").lower()
    if not any(marker in name for marker in ("report_task_status", "finish", "complete")):
        return None
    if ctx.postcondition_verified:
        return None
    return Violation(
        rule="success_requires_evidence",
        reason=(
            "Báo hoàn thành khi tiêu chí xác minh của công đoạn chưa có bằng chứng quan sát được"
            " (NOT_VERIFIED)."
        ),
    )


#: Thứ tự chạy luật. Luật nhạy cảm nhất trước để lý do hiển thị là lý do nặng nhất.
RULES = (
    rule_no_invented_password,
    rule_sensitive_action_needs_subtask,
    rule_close_overlay_only_when_intended,
    rule_success_requires_evidence,
)


def check_actions(actions: list[dict[str, Any]], ctx: ContractContext) -> list[Violation]:
    """Mọi vi phạm của một lượt action (rỗng nghĩa là hợp lệ)."""
    out: list[Violation] = []
    for action in actions or []:
        for rule in RULES:
            violation = rule(action, ctx)
            if violation is not None:
                out.append(violation)
    return out


def blocking_violations(actions: list[dict[str, Any]], ctx: ContractContext) -> list[Violation]:
    """Chỉ những vi phạm **đang được cưỡng chế** — dùng để quyết định chặn hay không."""
    return [item for item in check_actions(actions, ctx) if is_enforced(item.rule)]


def describe(violations: list[Violation]) -> str:
    """Câu mô tả để ghi log và (khi bật) trả về cho agent biết vì sao bị chặn."""
    return " | ".join(f"[{item.rule}] {item.reason}" for item in violations)


__all__ = [
    "CLOSE_OVERLAY_TERMS",
    "ENFORCE_ENV",
    "PASSWORD_TERMS",
    "RULES",
    "RULES_ENV",
    "SAVED_ACCOUNT_TERMS",
    "SENSITIVE_ACTION_TERMS",
    "ContractContext",
    "Violation",
    "blocking_violations",
    "check_actions",
    "describe",
    "enforced_rules",
    "enforcement_enabled",
    "is_enforced",
    "rule_close_overlay_only_when_intended",
    "rule_no_invented_password",
    "rule_sensitive_action_needs_subtask",
    "rule_success_requires_evidence",
    "tester_supplied_password",
]


#: Bắt "tester có đưa mật khẩu" bằng **giá trị đi kèm**, không bằng chữ "mật khẩu" xuất hiện đâu đó:
#: prompt đã ghép luôn chứa chữ này (tên workflow ``auth.login_password``, bước "nhập mật khẩu"),
#: nên nếu chỉ tìm từ khoá thì luật sẽ tự tắt trong mọi run — tức là không bảo vệ được gì.
_PASSWORD_VALUE = re.compile(
    r"(?:mật khẩu|mat khau|password|passcode|mã pin|pin code)\s*[:=]?\s*([A-Za-z0-9!@#$%^&*_.-]{4,})",
    re.IGNORECASE,
)


def tester_supplied_password(text: str) -> bool:
    """Tester có **đưa mật khẩu** trong câu của mình hay không (heuristic, ghi rõ để còn chỉnh).

    Đây là chỗ dễ sai nhất của luật "không tự nhập mật khẩu", nên nó tách riêng và có test: sai ở
    đây thì hoặc là luật tự tắt (không bảo vệ), hoặc là chặn oan tester đã cấp mật khẩu.
    """
    return bool(_PASSWORD_VALUE.search(text or ""))
