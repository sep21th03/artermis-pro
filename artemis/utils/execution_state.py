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

"""Trạng thái thực thi **có cấu trúc** cho từng công đoạn (subtask).

**Vì sao cần.** Trước đây "đã làm bước nào" chỉ tồn tại ở hai nơi đều không đáng tin: trong plan
Markdown (agent tự sửa dấu ``[x]``) và trong context của LLM (agent phải tự nhớ). Đo trên máy thật
29/10/2026: prompt mỗi lượt 12,7k→18,3k token, và agent quên mất ràng buộc "phải quan sát đúng tài
khoản 0999" dù nó nằm trong prompt — vì ràng buộc là *văn bản giữa hàng nghìn token khác*, không
phải một trường mà code kiểm được.

Ở đây mỗi công đoạn là một bản ghi có kiểu: mục tiêu, giá trị đích phải quan sát, ràng buộc, tiêu
chí hoàn thành, và **trạng thái xác minh do code quyết định**. Nhờ vậy:

* không phụ thuộc việc LLM nhớ bước nào đã xong (A.3);
* mỗi lượt chỉ cần cấp công đoạn hiện tại + ràng buộc của nó (A.4, E);
* không quan sát được tiêu chí ⇒ ``NOT_VERIFIED``, **không** tự coi là xong (A.6).

Module này là hàm thuần: plan + câu gốc + định danh đã thấy vào, trạng thái có cấu trúc ra. Không
thiết bị, không LLM, không ghi file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from artemis.agents.operator.prompts import (
    observed_identifiers,
    required_state_values,
    value_was_observed,
)
from artemis.utils.plan_grammar import PlanSnapshot, parse_plan

#: Trạng thái của một công đoạn. ``not_verified`` là câu trả lời **hợp lệ**, không phải lỗi: nó
#: nghĩa là "chưa quan sát được tiêu chí", khác hẳn "đã xong".
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_VERIFIED = "verified"
STATUS_NOT_VERIFIED = "not_verified"


@dataclass
class SubtaskState:
    """Một công đoạn: làm gì, phải thấy gì, và đã xác minh chưa."""

    id: str
    text: str
    #: Văn bản các công đoạn con. Cần cho người đọc công đoạn (ví dụ truy xuất KB theo công đoạn):
    #: "Thêm hành động cho ngữ cảnh" nằm ở dòng con, không nằm ở dòng milestone.
    children: list[str] = field(default_factory=list)
    status: str = STATUS_PENDING
    target_values: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    postcondition: str = ""
    evidence: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def verified(self) -> bool:
        return self.status == STATUS_VERIFIED

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionState:
    """Toàn cảnh: công đoạn đang làm, đã xong, và những cái **chưa xác minh được**."""

    subtasks: list[SubtaskState] = field(default_factory=list)
    required_values: list[tuple[str, str]] = field(default_factory=list)
    observed_values: list[str] = field(default_factory=list)

    def active(self) -> SubtaskState | None:
        return next((item for item in self.subtasks if item.is_active), None)

    def verified(self) -> list[SubtaskState]:
        return [item for item in self.subtasks if item.verified]

    def not_verified(self) -> list[SubtaskState]:
        return [item for item in self.subtasks if item.status == STATUS_NOT_VERIFIED]

    @property
    def missing_values(self) -> list[str]:
        """Giá trị mà yêu cầu bắt buộc quan sát nhưng **chưa** thấy trên màn hình."""
        return [
            value
            for _label, value in self.required_values
            if not value_was_observed(value, self.observed_values)
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "subtasks": [item.as_dict() for item in self.subtasks],
            "required_values": [list(pair) for pair in self.required_values],
            "observed_values": list(self.observed_values),
            "missing_values": self.missing_values,
        }


def _postcondition_of(text: str) -> str:
    """Tiêu chí hoàn thành ghi ngay trong dòng công đoạn (``⇒``/``->``/``thì``), nếu có."""
    for marker in ("⇒", "->", "→"):
        if marker in text:
            return text.split(marker, 1)[1].strip()
    return ""


def _constraints_of(plan: PlanSnapshot, milestone_text: str, goal_text: str) -> list[str]:
    """Ràng buộc áp cho công đoạn: câu guard/điều kiện trong kế hoạch nhắc tới nó.

    Chỉ lấy **câu có căn cứ** (guard của composer hoặc dòng điều kiện của agent) — không suy diễn
    thêm ràng buộc mới, vì ràng buộc sai sẽ chặn đúng việc cần làm.
    """
    out: list[str] = []
    for line in (goal_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if any(marker in low for marker in ("chỉ khi", "chỉ được coi là", "điều kiện", "bắt buộc")):
            if not milestone_text or any(
                word in stripped.lower()
                for word in milestone_text.lower().split()[:3]
                if len(word) > 3
            ):
                out.append(stripped)
        if len(out) >= 3:
            break
    for item in plan.items:
        if "chỉ khi" in item.text.lower() and item.text not in out:
            out.append(item.text)
        if len(out) >= 4:
            break
    return out


def build_execution_state(
    task_plan: str,
    goal_text: str,
    observed_text: str = "",
) -> ExecutionState:
    """Dựng trạng thái thực thi từ plan + câu gốc + màn hình đã quan sát.

    Quy tắc xác minh (A.5/A.6) — **code quyết định, không phải agent**:

    * công đoạn chưa làm ⇒ ``pending``;
    * công đoạn đang làm ⇒ ``active``;
    * công đoạn agent đánh dấu xong **và** mọi giá trị bắt buộc đã thấy ⇒ ``verified``;
    * công đoạn agent đánh dấu xong nhưng còn giá trị bắt buộc chưa thấy ⇒ ``not_verified`` kèm lý do.

    Dấu ``[x]`` trong plan vẫn được tôn trọng như *ý định* của agent, nhưng không đủ để coi là xong:
    đó chính là chỗ lần chạy thật đã đi sai (agent coi "đã đăng nhập" là xong mà chưa từng thấy tài
    khoản 0999 trên màn hình).
    """
    snapshot = parse_plan(task_plan or "")
    observed = sorted(observed_identifiers(observed_text or ""))
    requirements = list(required_state_values(goal_text or ""))
    missing = [value for _label, value in requirements if not value_was_observed(value, observed)]

    milestone = snapshot.active_milestone()
    children = snapshot.children_of(milestone) if milestone is not None else ()
    active_text = (
        " ".join([milestone.text, *[child.text for child in children]]) if milestone else ""
    )

    subtasks: list[SubtaskState] = []
    for item in snapshot.top_level:
        text = item.text
        if milestone is not None and item.key == milestone.key:
            status = STATUS_ACTIVE
            reason = ""
        elif item.is_done:
            if missing:
                status = STATUS_NOT_VERIFIED
                reason = "agent đánh dấu xong nhưng chưa quan sát thấy: " + ", ".join(missing[:3])
            else:
                status = STATUS_VERIFIED
                reason = ""
        else:
            status = STATUS_PENDING
            reason = ""
        subtasks.append(
            SubtaskState(
                id=item.key,
                text=text,
                children=[child.text for child in snapshot.children_of(item)],
                status=status,
                target_values=[value for _label, value in requirements],
                constraints=_constraints_of(snapshot, active_text, goal_text),
                postcondition=_postcondition_of(text),
                evidence=observed if status == STATUS_VERIFIED else [],
                reason=reason,
            )
        )
    return ExecutionState(
        subtasks=subtasks,
        required_values=requirements,
        observed_values=observed,
    )


def advanceable(exec_state: ExecutionState) -> tuple[bool, str]:
    """Có được chuyển sang công đoạn kế tiếp hay không, kèm lý do khi chưa được.

    Dùng ở chỗ *chốt* chuyển công đoạn: nếu công đoạn đang làm còn giá trị bắt buộc chưa quan sát
    thì câu trả lời là ``NOT_VERIFIED`` — agent phải quan sát tiếp, không được coi là xong.
    """
    active = exec_state.active()
    if active is None:
        return True, ""
    if not exec_state.missing_values:
        return True, ""
    return False, (
        "NOT_VERIFIED: chưa quan sát thấy "
        + ", ".join(exec_state.missing_values[:3])
        + " — không được coi công đoạn này là xong."
    )


def render_execution_state(exec_state: ExecutionState, *, max_chars: int = 1200) -> str:
    """Khối chữ ngắn cho prompt: **chỉ** công đoạn đang làm + ràng buộc + điều chưa xác minh.

    Cố ý không in toàn bộ plan: lý do tồn tại của khối này là để lượt hiện tại không phải đọc lại
    hàng nghìn token của cả kế hoạch (E — ngân sách ngữ cảnh theo công đoạn).
    """
    active = exec_state.active()
    if active is None and not exec_state.not_verified():
        return ""
    lines: list[str] = ["TRẠNG THÁI CÔNG ĐOẠN (do code xác định, không phải tự suy):"]
    if active is not None:
        lines.append(f"- Đang làm: {active.text}")
        if active.postcondition:
            lines.append(f"  tiêu chí xong: {active.postcondition}")
        if exec_state.missing_values:
            lines.append("  CHƯA xác minh: chưa thấy " + ", ".join(exec_state.missing_values[:3]))
        elif exec_state.required_values:
            lines.append("  đã xác minh: thấy đủ giá trị bắt buộc")
        for constraint in active.constraints[:2]:
            lines.append(f"  ràng buộc: {constraint}")
    for item in exec_state.not_verified()[:2]:
        lines.append(f"- NOT_VERIFIED (không được coi là xong): {item.text} — {item.reason}")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "\n…"


__all__ = [
    "STATUS_ACTIVE",
    "STATUS_NOT_VERIFIED",
    "STATUS_PENDING",
    "STATUS_VERIFIED",
    "ExecutionState",
    "SubtaskState",
    "advanceable",
    "build_execution_state",
    "render_execution_state",
]
