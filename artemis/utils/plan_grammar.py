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

"""Single source of truth for the task-plan grammar (the "machine channel").

``task_plan.md`` is a dual-channel document:

- **Machine channel**: the checkbox micro-grammar parsed here (status chars,
  indentation, ``[Loop]`` / ``[Loop:continuous]`` tags). Harness code may only
  ever read this channel.
- **Semantic channel**: all remaining free text. Only LLM agents interpret it;
  harness code must never branch on wording.

Every deterministic decision the harness makes about the plan (termination,
loop protection, validation/checker triggers, hand-slip detection) derives
from the :class:`PlanSnapshot` produced by :func:`parse_plan`. The same
constants render :data:`PLAN_GRAMMAR_SPEC`, the grammar description injected
into agent prompts via the ``{{ plan_grammar }}`` template variable — so the
contract the agents are taught and the contract the code enforces cannot
drift apart.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal

STATUS_PENDING = " "
STATUS_ACTIVE = "/"
STATUS_DONE = "x"
STATUS_BLOCKED = "!"
STATUS_CHARS = STATUS_PENDING + STATUS_ACTIVE + STATUS_DONE + STATUS_BLOCKED

LOOP_TAG = "[Loop]"
CONTINUOUS_LOOP_TAG = "[Loop:continuous]"

CHECKBOX_LINE_RE = re.compile(
    r"^(?P<indent>\s*)-\s*\[(?P<status>[" + re.escape(STATUS_CHARS) + r"])\]\s*(?P<text>.*)$"
)
_LOOP_TAG_RE = re.compile(r"\[Loop(?::continuous)?\]")
_CONTINUOUS_TAG_RE = re.compile(re.escape(CONTINUOUS_LOOP_TAG))

#: Check lines are indented attachments under a top-level subgoal
#: (``- verify: ...`` / ``- assert: ...``) or task-level free items
#: (``- assert@end: ...`` at zero indent). The optional ``@end`` suffix moves
#: the judgment moment from the anchored subgoal's completion to task exit.
CHECK_LINE_RE = re.compile(
    r"^(?P<indent>\s*)-\s*(?P<kind>verify|assert)(?P<at_end>@end)?\s*:\s*(?P<text>\S.*?)\s*$"
)

#: Finding lines are SYSTEM-authored standing headlines for unresolved verify
#: failures, indented under the affected subgoal. They are a single-direction
#: projection of the checkpoint repair state: harness code re-renders them on
#: every plan write and NEVER reads their text back. They match neither
#: :data:`CHECKBOX_LINE_RE` nor :data:`CHECK_LINE_RE`, so they can never
#: contribute to milestone hashing, ratchet drift detection, check-item
#: multisets, or completion triggers.
FINDING_LINE_RE = re.compile(r"^(?P<indent>\s*)-\s*finding\s*:\s*(?P<text>.*)$")


def subgoal_hash(text: str) -> str:
    """Stable identity hash for a subgoal text (used for per-subgoal artifacts)."""
    return hashlib.md5(text.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanItem:
    """One checkbox line of the plan's machine channel."""

    indent: int
    status: str
    text: str
    line_no: int

    @property
    def is_top_level(self) -> bool:
        return self.indent == 0

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def is_done(self) -> bool:
        return self.status == STATUS_DONE

    @property
    def is_loop(self) -> bool:
        return bool(_LOOP_TAG_RE.search(self.text))

    @property
    def is_continuous(self) -> bool:
        return bool(_CONTINUOUS_TAG_RE.search(self.text))

    @property
    def key(self) -> str:
        return subgoal_hash(self.text)


@dataclass(frozen=True)
class CheckItem:
    """One declared check line of the plan's machine channel.

    ``verify`` marks a completion acceptance criterion (a failure triggers
    repair); ``assert`` marks a test assertion (a failure is recorded as a
    legitimate test result, never repaired).
    """

    kind: Literal["verify", "assert"]
    when: Literal["on_complete", "at_end"]
    text: str
    parent_key: str | None

    @property
    def key(self) -> str:
        """Stable identity of the check line (kind + when + text + anchor)."""
        return subgoal_hash(f"{self.kind}|{self.when}|{self.parent_key}|{self.text}")


@dataclass(frozen=True)
class PlanSnapshot:
    """Read-only derived view of a plan. Never persisted, never written back."""

    items: tuple[PlanItem, ...]
    check_items: tuple[CheckItem, ...] = field(default=())

    @property
    def top_level(self) -> tuple[PlanItem, ...]:
        return tuple(i for i in self.items if i.is_top_level)

    @property
    def has_top_level(self) -> bool:
        return bool(self.top_level)

    @property
    def all_top_level_done(self) -> bool:
        top = self.top_level
        return bool(top) and all(i.is_done for i in top)

    @property
    def continuous_top_level(self) -> tuple[PlanItem, ...]:
        return tuple(i for i in self.top_level if i.is_continuous)

    @property
    def milestone_texts(self) -> tuple[str, ...]:
        return tuple(i.text.strip() for i in self.top_level)

    def first_active(self) -> PlanItem | None:
        for item in self.items:
            if item.is_active:
                return item
        return None

    def last_active(self) -> PlanItem | None:
        for item in reversed(self.items):
            if item.is_active:
                return item
        return None

    def parent_of(self, item: PlanItem) -> PlanItem | None:
        """Nearest top-level item above an indented item."""
        parent = None
        for candidate in self.items:
            if candidate.line_no >= item.line_no:
                break
            if candidate.is_top_level:
                parent = candidate
        return parent

    def children_of(self, item: PlanItem) -> tuple[PlanItem, ...]:
        """Nested items under a top-level item (up to the next top-level line)."""
        if not item.is_top_level:
            return ()
        children: list[PlanItem] = []
        for candidate in self.items:
            if candidate.line_no <= item.line_no:
                continue
            if candidate.is_top_level:
                break
            children.append(candidate)
        return tuple(children)

    def active_milestone(self) -> PlanItem | None:
        """The milestone currently being executed.

        An explicitly active top-level item wins; otherwise the parent of the
        bottom-most active nested item; otherwise, when every item is still
        untouched, the first pending milestone (the same fallback the step
        stamping uses). ``None`` when nothing is in progress.
        """
        for item in self.top_level:
            if item.is_active:
                return item
        nested_active = self.last_active()
        if nested_active is not None:
            return self.parent_of(nested_active)
        if self.top_level and all(item.is_pending for item in self.top_level):
            return self.top_level[0]
        return None

    def active_leaf(self, milestone: PlanItem) -> PlanItem | None:
        """Bottom-most ``[/]`` nested item under ``milestone`` (the current sub-goal)."""
        leaf = None
        for child in self.children_of(milestone):
            if child.is_active:
                leaf = child
        return leaf

    def check_items_of(self, item: PlanItem) -> tuple[CheckItem, ...]:
        """Check lines anchored to the given top-level subgoal."""
        return tuple(ci for ci in self.check_items if ci.parent_key == item.key)

    @property
    def task_level_check_items(self) -> tuple[CheckItem, ...]:
        return tuple(ci for ci in self.check_items if ci.parent_key is None)

    @property
    def all_check_items(self) -> tuple[CheckItem, ...]:
        return self.check_items


def _checkbox_base_indent(content: str) -> int:
    """Thụt lề chung của các dòng checkbox — dùng để chuẩn hoá trước khi xác định top-level.

    **Vì sao cần.** ``PlanItem.is_top_level`` là ``indent == 0``. Model hay viết **cả** kế hoạch
    trong một khối thụt lề (dòng đầu đã có 2 space), và khi đó *không dòng nào* là top-level:
    ``convergence_gate`` thấy ``has_top_level == False`` ⇒ kết thúc run sau bước đầu, còn
    ``ExecutionState``/``KbFocusPromptComponent`` không có công đoạn nào để bám vào.

    Đo trên máy thật 29/10/2026 (task ``task_2910_1428``): plan thụt 2 space ⇒ 0/3 milestone ⇒
    gate kết thúc run ngay sau khi agent mở bảng chọn thiết bị, final check chốt ``blocked`` dù
    ngữ cảnh còn chưa được lưu. 3/8 plan gần nhất ở ``traces/*/notes/task_plan.md`` ở tình trạng
    này, nên đây là lỗi hệ thống chứ không phải ca cá biệt.

    Chỉ tính trên **dòng checkbox**: file thật có thể có ``- assert@end:`` ở cột 0 trong khi mọi
    checkbox thụt lề; lấy min của cả hai loại sẽ ra 0 và không chuẩn hoá được gì.
    """
    indents = [
        len(match.group("indent"))
        for match in (CHECKBOX_LINE_RE.match(line) for line in (content or "").splitlines())
        if match
    ]
    return min(indents) if indents else 0


def parse_plan(content: str | None) -> PlanSnapshot:
    """Parses plan markdown into its machine channel. Non-checkbox lines are ignored.

    Check lines (``- verify:`` / ``- assert:``) are parsed into
    :class:`CheckItem` entries; they never contribute to the checkbox item list
    and therefore never influence milestone hashing, drift detection, or
    completion triggers.

    Thụt lề được **chuẩn hoá theo mức nhỏ nhất của các dòng checkbox** trước khi xác định
    top-level, nên một kế hoạch bị thụt lề đồng loạt vẫn giữ đúng quan hệ cha/con và độ sâu
    tương đối, chỉ khác là dòng ngoài cùng trở thành top-level đúng như ý định của người viết.
    Nội dung và trạng thái ``[ ]/[x]/[/]`` không bị đổi.
    """
    items: list[PlanItem] = []
    checks: list[CheckItem] = []
    current_top: PlanItem | None = None
    base_indent = _checkbox_base_indent(content or "")
    if content:
        for line_no, line in enumerate(content.splitlines()):
            match = CHECKBOX_LINE_RE.match(line)
            if match:
                item = PlanItem(
                    indent=max(0, len(match.group("indent")) - base_indent),
                    status=match.group("status"),
                    text=match.group("text").strip(),
                    line_no=line_no,
                )
                items.append(item)
                if item.is_top_level:
                    current_top = item
                continue
            check_match = CHECK_LINE_RE.match(line)
            if check_match:
                # Cùng phép chuẩn hoá cho dòng kiểm: một dòng nằm sâu hơn dòng checkbox ngoài cùng
                # là dòng con; dòng ngang hoặc trên mức đó là dòng kiểm chung của cả kế hoạch.
                indented = max(0, len(check_match.group("indent")) - base_indent) > 0
                at_end = bool(check_match.group("at_end"))
                parent_key = current_top.key if (indented and current_top) else None
                checks.append(
                    CheckItem(
                        kind=check_match.group("kind"),
                        when="at_end" if at_end else "on_complete",
                        text=check_match.group("text").strip(),
                        parent_key=parent_key,
                    )
                )
    return PlanSnapshot(items=tuple(items), check_items=tuple(checks))


def milestones_changed(before: PlanSnapshot, after: PlanSnapshot) -> bool:
    """True if the top-level milestone texts differ (status flips don't count)."""
    return before.milestone_texts != after.milestone_texts


#: Từ khoá thể hiện **ý định xác minh** ngay trong văn bản của công đoạn/việc con.
#: Đo trên máy thật 30/10/2026 (session 93882dc3): milestone
#: "Verify current house via Home tab and switch to `Dled`" có mọi dòng con `[x]` nên
#: ``settle_completed_milestones`` chốt nó **từ checkbox**, convergence gate kết thúc run, và không
#: có bằng chứng nào cho việc xác minh trên Trang chủ — nhưng run vẫn được coi là thành công.
_VERIFICATION_INTENT_RE = re.compile(
    r"(?i)\b(verify|check|confirm|ensure|validate|assert|"
    r"xác minh|kiểm tra|xác nhận|đối chiếu)\b"
)


def normalize_check_text(text: str | None) -> str:
    """Chuẩn hoá văn bản check item để so khớp với ledger (khoảng trắng + hoa/thường)."""
    return " ".join((text or "").split()).casefold()


#: Bề mặt màn hình mà một tiêu chí xác minh có thể nhắc tới, kèm token nhận diện.
#: Đo trên máy thật 30/10/2026 (session 93882dc3): bằng chứng "Dled house card is still clearly
#: visible" là **Quản lý nhà**, không chứng minh được yêu cầu "via Home tab" của milestone.
_SURFACE_TOKENS: dict[str, tuple[str, ...]] = {
    "home": ("home", "trang chủ"),
    "menu": ("menu",),
    "house_management": ("quản lý nhà", "house management"),
    "login": ("đăng nhập", "login"),
}


def verification_surfaces(*texts: str | None) -> frozenset[str]:
    """Các **màn hình** mà văn bản xác minh nhắc tới (rỗng ⇒ không ràng buộc màn hình)."""
    joined = normalize_check_text(" ".join(t or "" for t in texts))
    return frozenset(
        surface for surface, tokens in _SURFACE_TOKENS.items() if any(t in joined for t in tokens)
    )


def evidence_matches_surfaces(evidence_text: str | None, required: Collection[str]) -> bool:
    """Bằng chứng phải nhắc tới **đúng màn hình** mà tiêu chí yêu cầu.

    Không ràng buộc màn hình ⇒ luôn đạt. Có ràng buộc ⇒ bằng chứng phải nhắc tới ít nhất một màn
    hình trong đó; bằng chứng ở màn khác **không** tính (đây là lỗ đã đo: "Dled selected on Quản lý
    nhà" không phải bằng chứng cho "Home tab hiển thị Dled").
    """
    required_set = {r for r in required if r}
    if not required_set:
        return True
    return bool(verification_surfaces(evidence_text) & required_set)


def milestone_verification_surfaces(
    snapshot: PlanSnapshot, milestone: PlanItem
) -> frozenset[str]:
    """Màn hình mà công đoạn (và các việc xác minh con) yêu cầu phải được nhìn thấy."""
    texts: list[str | None] = [milestone.text]
    for item in snapshot.children_of(milestone):
        if _VERIFICATION_INTENT_RE.search(item.text or ""):
            texts.append(item.text)
    return verification_surfaces(*texts)


def verification_request_text(gap: str, milestone: PlanItem, surfaces: Collection[str]) -> str:
    """Yêu cầu xác minh **cụ thể** để gửi Operator qua kênh ``- finding:`` của hệ thống.

    Không phải thông báo chung: nó nêu đúng công đoạn, đúng lý do thiếu bằng chứng, và **đúng màn
    hình** cần nhìn để lấy bằng chứng (``Home``/``Menu``/``Quản lý nhà``…).
    """
    surface_names = {
        "home": "Trang chủ (Home tab)",
        "menu": "tab Menu",
        "house_management": "Quản lý nhà",
        "login": "màn Đăng nhập",
    }
    where = ", ".join(surface_names.get(s, s) for s in sorted(surfaces))
    requirement = (
        f" Đọc trực tiếp trên {where} rồi ghi lại bằng chứng quan sát được."
        if where
        else " Ghi lại bằng chứng quan sát được (màn hình/giá trị cụ thể)."
    )
    return (
        f"Chưa xác minh «{_one_line(milestone.text, 70)}»: {gap}.{requirement}"
        " Chưa có bằng chứng thì **không** được coi công đoạn này là xong."
    )


def ensure_finding_line(content: str, milestone_key: str, text: str) -> tuple[str, bool]:
    """Bảo đảm có **một** dòng ``- finding:`` dưới milestone; trả ``(nội dung, có thay đổi)``.

    Khác :func:`apply_finding_lines` (xoá rồi chiếu lại toàn bộ finding do checker sở hữu), hàm này
    chỉ **thêm khi chưa có** ⇒ không ghi đè kênh finding của checker.
    """
    body = (text or "").strip()
    source = content or ""
    snapshot = parse_plan(source)
    if not body or not snapshot.has_top_level:
        return source, False
    milestone = next((item for item in snapshot.top_level if item.key == milestone_key), None)
    if milestone is None:
        return source, False
    lines = source.splitlines()
    if milestone.line_no >= len(lines):
        return source, False
    for candidate in snapshot.items:
        if candidate.line_no <= milestone.line_no:
            continue
        if candidate.is_top_level:
            break
        if candidate.line_no >= len(lines):
            continue
        match = FINDING_LINE_RE.match(lines[candidate.line_no])
        if match and normalize_check_text(match.group("text")) == normalize_check_text(body):
            return source, False
    lines.insert(milestone.line_no + 1, render_finding_line(body, indent=2))
    rewritten = "\n".join(lines) + ("\n" if source.endswith("\n") else "")
    return rewritten, True



def verification_intent_items(snapshot: PlanSnapshot) -> tuple[PlanItem, ...]:
    """Các dòng checkbox **tự khai là xác minh** — cần bằng chứng thật, không chỉ ``[x]``."""
    return tuple(
        item
        for item in snapshot.items
        if not item.is_loop and _VERIFICATION_INTENT_RE.search(item.text or "")
    )


def milestone_evidence_gap(
    snapshot: PlanSnapshot,
    milestone: PlanItem,
    *,
    passed_check_items: Collection[str] = (),
    judged_check_items: Collection[str] = (),
    evidenced_milestones: Collection[str] = (),
) -> str | None:
    """Lý do một milestone **chưa đủ bằng chứng** để chốt tự động; ``None`` khi đã đủ.

    Hai điều kiện, đúng hai cách "nói dối" đã đo được trên máy thật:

    1. Nhánh còn **check item chưa có verdict passed** ⇒ ``[x]`` của các dòng con không phải bằng
       chứng cho ``verify``/``assert``.
    2. Nhánh **tự khai xác minh** (``verify|check|confirm|ensure|xác minh|kiểm tra…``) mà **không có
       bằng chứng nào** (không check item passed, không ledger record passed gắn với công đoạn) ⇒
       chốt nó là kết thúc run bằng một lời tự khai.

    Milestone không khai xác minh và không có check item ⇒ trả ``None`` (giữ nguyên hành vi cũ).
    """
    passed = {normalize_check_text(t) for t in passed_check_items}
    judged = {normalize_check_text(t) for t in judged_check_items} | passed
    evidenced = {normalize_check_text(t) for t in evidenced_milestones}
    milestone_checks = snapshot.check_items_of(milestone)
    for check in milestone_checks:
        key = normalize_check_text(check.text)
        # **Cả** ``verify`` **và** ``assert`` đều phải **passed**: một assert fail/inconclusive không
        # phải bằng chứng rằng tiêu chí đã đạt, và `[x]` của dòng con cũng không. ``judged`` chỉ dùng
        # để nói rõ trong thông điệp là "đã phán nhưng không đạt" hay "chưa hề được phán".
        if key in passed:
            continue
        label = "verify" if check.kind == "verify" else "assert"
        if key in judged:
            return f"{label} «{_one_line(check.text, 70)}» đã được phán nhưng KHÔNG passed"
        return f"{label} «{_one_line(check.text, 70)}» chưa có verdict passed"
    if milestone_checks:
        return None
    if normalize_check_text(milestone.text) in evidenced:
        return None
    for item in (milestone, *snapshot.children_of(milestone)):
        if _VERIFICATION_INTENT_RE.search(item.text or ""):
            return f"việc xác minh «{_one_line(item.text, 70)}» chưa có bằng chứng nào"
    return None



def collapse_duplicate_check_lines(content: str | None) -> tuple[str, int]:
    """Bỏ các dòng ``- verify:`` / ``- assert:`` **giống hệt nhau trong cùng một phạm vi**.

    Phạm vi = milestone cha (dòng kiểm tra thụt lề) hoặc cấp task (dòng không thụt lề) — đúng cách
    ``parse_plan`` gán ``parent_key``. Giữ **lần xuất hiện đầu tiên** và giữ nguyên thứ tự; chỉ bỏ
    bản sao **trùng cả loại, mốc phán, phạm vi và văn bản**; mọi tiêu chí khác nhau giữ nguyên.

    **Vì sao cần** (máy thật 30/10/2026, session ``4806881d``): plan có **hai** dòng
    ``- verify@end: Current logged-in account is 0972842548 and current house is Dled`` giống hệt
    nhau ⇒ checker phán cùng một tiêu chí hai lần và bảng tiêu chí bị phồng.
    """
    text = content or ""
    if not text:
        return text, 0
    lines = text.splitlines()
    seen: set[tuple[str, str, str | None, str]] = set()
    kept: list[str] = []
    removed = 0
    current_top: str | None = None
    for line in lines:
        checkbox = CHECKBOX_LINE_RE.match(line)
        if checkbox:
            if not checkbox.group("indent"):
                current_top = normalize_check_text(checkbox.group("text"))
            kept.append(line)
            continue
        check = CHECK_LINE_RE.match(line)
        if check:
            # Dòng thụt lề thuộc milestone đang mở; dòng ở cột 0 là tiêu chí cấp task (parent None).
            scope = current_top if check.group("indent") else None
            key = (
                check.group("kind"),
                check.group("at_end") or "",
                scope,
                normalize_check_text(check.group("text")),
            )
            if key in seen:
                removed += 1
                continue
            seen.add(key)
        kept.append(line)
    if not removed:
        return text, 0
    rewritten = "\n".join(kept) + ("\n" if text.endswith("\n") else "")
    return rewritten, removed


def settle_completed_milestones(
    content: str | None,
    *,
    passed_check_items: Collection[str] = (),
    judged_check_items: Collection[str] = (),
    evidenced_milestones: Collection[str] = (),
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Chốt các công đoạn **đã xong** bằng code, và trả về tên những công đoạn vừa chốt.

    **Vì sao cần.**  ``convergence_gate`` chỉ đọc ``all_top_level_done`` — nghĩa là nó *hoàn
    toàn* tin vào việc model tự đánh dấu milestone.  Không có chỗ nào *tính* công đoạn, nên
    không ai phát hiện "công đoạn đã xong mà chưa được chốt".  Đo trên máy thật 28/10/2026
    (câu *"đăng nhập tài khoản 0999 chuyển nhà sang nhà 'Mesh Tạo 2' rồi vào màn ngữ cảnh…"*):
    plan là ``[/] Switch active house to "Mesh Tạo 2"`` với **cả hai** sub-goal đã ``[x]``, nhưng
    model không chốt milestone cũng không mở công đoạn kế tiếp — nó lặp tap/vuốt trên màn Quản
    lý nhà (step 25–30, cùng một toạ độ) cho tới hết ngân sách.

    **Quy tắc** (tất định, không suy diễn): một milestone **có** sub-goal và mọi sub-goal đã
    ``[x]`` ⇒ milestone ``[x]``.  Milestone không có sub-goal thì để nguyên: không có bằng chứng
    nào để chốt.  Milestone vòng lặp (``[Loop]``/``[Loop:continuous]``) không bao giờ bị chốt
    tự động — việc giám sát chỉ kết thúc khi có tín hiệu dừng của người dùng.

    Chỉ đổi đúng ký tự trạng thái trên dòng của milestone; mọi dòng khác giữ nguyên từng ký tự.
    """
    text = content or ""
    snapshot = parse_plan(text)
    if not snapshot.has_top_level:
        return text, (), ()
    lines = text.splitlines()
    settled: list[str] = []
    withheld: list[str] = []
    for milestone in snapshot.top_level:
        if milestone.is_done or milestone.is_loop:
            continue
        children = snapshot.children_of(milestone)
        if not children or not all(child.is_done for child in children):
            continue
        gap = milestone_evidence_gap(
            snapshot,
            milestone,
            passed_check_items=passed_check_items,
            judged_check_items=judged_check_items,
            evidenced_milestones=evidenced_milestones,
        )
        if gap:
            # Mọi dòng con đã `[x]` nhưng **thiếu bằng chứng**: KHÔNG chốt. Giữ nguyên trạng thái để
            # công đoạn vẫn là việc đang làm ⇒ dòng tiến độ plan đưa Operator trở lại đúng việc xác
            # minh đó; hết ngân sách thì exit settlement chốt `blocked` (không success).
            withheld.append(f"{_one_line(milestone.text, 70)} — {gap}")
            continue
        if milestone.line_no >= len(lines):
            continue
        match = CHECKBOX_LINE_RE.match(lines[milestone.line_no])
        if not match:
            continue
        start, end = match.span("status")
        lines[milestone.line_no] = (
            lines[milestone.line_no][:start] + STATUS_DONE + lines[milestone.line_no][end:]
        )
        settled.append(milestone.text)
    if not settled:
        return text, (), tuple(withheld)
    rewritten = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return rewritten, tuple(settled), tuple(withheld)


def render_plan_progress(content: str | None) -> str:
    """Một dòng tiến độ công đoạn — **chỗ duy nhất** đếm công đoạn của cả lượt chạy.

    In ra cho agent thấy nó đang ở công đoạn nào, còn mấy công đoạn, và công đoạn kế tiếp là gì.
    Không có dòng này thì agent phải tự suy ra tiến độ từ chính plan nó vừa viết — và đo trên
    máy thật, nó đọc sai: milestone đã xong (mọi sub-goal ``[x]``) vẫn bị coi là đang làm, nên
    agent quay lại thao tác trên màn hình cũ thay vì đi tiếp.

    Trả chuỗi rỗng khi plan chưa có milestone nào (không in dòng vô nghĩa).
    """
    snapshot = parse_plan(content)
    if not snapshot.has_top_level:
        return ""
    milestones = snapshot.top_level
    done = sum(1 for item in milestones if item.is_done)
    current = snapshot.active_milestone()
    parts = [f"Progress: {done}/{len(milestones)} công đoạn đã xong"]
    if current is not None:
        parts.append(f"đang làm: «{_one_line(current.text)}»")
        children = snapshot.children_of(current)
        if current.is_active and children and all(child.is_done for child in children):
            parts.append("mọi việc con của nó đã [x] ⇒ chốt nó và đi tiếp, KHÔNG làm lại")
    upcoming = next(
        (
            item
            for item in milestones
            if not item.is_done and item.key != (current.key if current else None)
        ),
        None,
    )
    if upcoming is not None:
        parts.append(f"kế tiếp: «{_one_line(upcoming.text)}»")
    return " · ".join(parts)


def _one_line(text: str, limit: int = 90) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def plan_with_progress(content: str | None) -> str:
    """Plan kèm dòng tiến độ công đoạn ở đầu — dùng ở **mọi chỗ agent đọc plan**.

    Gom vào một hàm để hai đường prompt (legacy ``build_plan_and_history`` và transcript
    ``PlanRecitationPromptComponent``) không thể lệch nhau về cách đếm công đoạn.
    """
    text = content or ""
    progress = render_plan_progress(text)
    return f"{progress}\n{text}" if progress else text


def _check_multiset(snapshot: PlanSnapshot) -> Counter:
    return Counter((ci.kind, ci.when, ci.text, ci.parent_key) for ci in snapshot.check_items)


def check_items_changed(before: PlanSnapshot, after: PlanSnapshot) -> bool:
    """True if the multiset of check lines differs between the two snapshots."""
    return _check_multiset(before) != _check_multiset(after)


def missing_check_items(before: PlanSnapshot, after: PlanSnapshot) -> list[CheckItem]:
    """Check items present in ``before`` but absent from ``after`` (multiset diff).

    Additions in ``after`` are ignored — adding check lines is allowed; only
    deletions/rewrites of existing ones are guarded.
    """
    deficit = _check_multiset(before) - _check_multiset(after)
    missing: list[CheckItem] = []
    for ci in before.check_items:
        sig = (ci.kind, ci.when, ci.text, ci.parent_key)
        if deficit.get(sig, 0) > 0:
            deficit[sig] -= 1
            missing.append(ci)
    return missing


def render_check_line(item: CheckItem, indent: int = 2) -> str:
    suffix = "@end" if item.when == "at_end" else ""
    prefix = " " * indent if item.parent_key is not None else ""
    return f"{prefix}- {item.kind}{suffix}: {item.text}"


def restore_missing_check_items(
    content_before: str,
    content_after: str,
    items: list[CheckItem] | None = None,
) -> str | None:
    """Deterministic guard: merge check lines deleted/rewritten by a plan write
    back into the new content.

    Returns the merged plan text, or ``None`` when nothing is missing. Lines
    whose parent subgoal still exists are re-anchored directly under it; lines
    whose parent subgoal was removed are converted to task-level ``@end`` items
    appended at the end of the plan. ``items`` restricts the merge to the given
    missing items (the caller may waive some of them).
    """
    before = parse_plan(content_before)
    after = parse_plan(content_after)
    missing = missing_check_items(before, after) if items is None else list(items)
    if not missing:
        return None

    lines = (content_after or "").splitlines()
    top_line_by_key: dict[str, int] = {}
    for item in after.items:
        if item.is_top_level:
            top_line_by_key.setdefault(item.key, item.line_no)

    inserts: dict[int, list[str]] = {}
    tail: list[str] = []
    for ci in missing:
        if ci.parent_key is not None and ci.parent_key in top_line_by_key:
            inserts.setdefault(top_line_by_key[ci.parent_key], []).append(render_check_line(ci))
        else:
            orphan = CheckItem(kind=ci.kind, when="at_end", text=ci.text, parent_key=None)
            tail.append(render_check_line(orphan))

    merged: list[str] = []
    for line_no, line in enumerate(lines):
        merged.append(line)
        if line_no in inserts:
            merged.extend(inserts[line_no])
    merged.extend(tail)
    return "\n".join(merged) + ("\n" if (content_after or "").endswith("\n") else "")


def render_finding_line(text: str, indent: int = 2) -> str:
    return f"{' ' * indent}- finding: {text}"


def strip_finding_lines(content: str) -> str:
    """Removes every finding line (system channel hygiene before re-projection)."""
    if not content:
        return content
    lines = [line for line in content.splitlines() if not FINDING_LINE_RE.match(line)]
    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")


def apply_finding_lines(content: str, findings_by_parent_key: dict[str, str]) -> str:
    """Deterministic single-direction projection of unresolved verify findings.

    Removes ALL existing ``- finding:`` lines, then re-inserts one rendered
    line per entry directly under its parent top-level subgoal (matched by
    content hash). Entries whose parent subgoal no longer exists in the plan
    are dropped silently — a rewritten subgoal supersedes its stale finding.
    The projection is content-driven and idempotent: the model deleting a
    finding line simply makes it grow back on the next write, and harness code
    never reads the rendered text back.
    """
    stripped = strip_finding_lines(content or "")
    if not findings_by_parent_key:
        return stripped

    snapshot = parse_plan(stripped)
    line_by_key: dict[str, int] = {}
    for item in snapshot.items:
        if item.is_top_level:
            line_by_key.setdefault(item.key, item.line_no)

    inserts: dict[int, list[str]] = {}
    for parent_key, text in findings_by_parent_key.items():
        line_no = line_by_key.get(parent_key)
        if line_no is None:
            continue
        inserts.setdefault(line_no, []).append(render_finding_line(text))

    if not inserts:
        return stripped

    lines = stripped.splitlines()
    merged: list[str] = []
    for line_no, line in enumerate(lines):
        merged.append(line)
        if line_no in inserts:
            merged.extend(inserts[line_no])
    return "\n".join(merged) + ("\n" if stripped.endswith("\n") else "")


def new_top_level_completions(before: PlanSnapshot, after: PlanSnapshot) -> list[str]:
    """Top-level texts newly marked done, in plan order."""
    before_done = {i.text for i in before.top_level if i.is_done}
    return [i.text for i in after.top_level if i.is_done and i.text not in before_done]


def unintended_milestone_edits(before: PlanSnapshot, after: PlanSnapshot) -> list[tuple[str, str]]:
    """Hand-slip signature of a full-file rewrite: positional top-level pairs
    whose status char is unchanged but whose text was reworded.

    Only defined when the milestone count is unchanged; structural changes
    (add/remove) are a declared replan and are judged by planner validation
    instead.
    """
    top_before = before.top_level
    top_after = after.top_level
    if len(top_before) != len(top_after):
        return []
    return [
        (b.text, a.text)
        for b, a in zip(top_before, top_after)
        if b.status == a.status and b.text != a.text
    ]


_PLAN_GRAMMAR_BASE = f"""### Task Plan Grammar (machine-enforced contract)
Every checklist line MUST match `<indent>- [<status>] <text>`:
- Status characters: `[{STATUS_PENDING}]` pending, `[{STATUS_ACTIVE}]` in progress, `[{STATUS_DONE}]` completed, `[{STATUS_BLOCKED}]` blocked.
- Top-level lines (no indentation) are strategic milestones. Nested lines are their sub-goals: 2 spaces for a sub-goal, 4 spaces for a sub-sub-goal beneath it (deeper nesting is allowed). Only zero-indent lines count as milestones for termination and validation; nested lines are the executor's live ledger. All other text in the note is free-form context.
- `{LOOP_TAG}` tags a BOUNDED iterative milestone: declare its exit boundary (e.g., `(Exit: <condition>; Interval: <cadence>)`) and mark it `[{STATUS_DONE}]` only once that exit condition is verifiably met.
- `{CONTINUOUS_LOOP_TAG}` tags an UNBOUNDED continuous-monitoring milestone: it must stay `[{STATUS_ACTIVE}]` and can never be marked `[{STATUS_DONE}]`, deleted, or untagged by you — the system mechanically rejects such edits. Only an explicit external stop signal injected by the user unlocks its completion; that signal is an external interruption delivered by the system, never something inferred from the screen or the plan.
- The task terminates only when every top-level milestone is `[{STATUS_DONE}]`."""

_CHECK_GRAMMAR_HEADER = """### Check Line Grammar (declared verification standards)
A top-level milestone may carry indented check lines, and the plan may end with task-level check lines at zero indentation:"""

#: Judgment-moment wording of the check lines, keyed by the midway gate.
_VERIFY_JUDGED_MIDWAY = (
    "It is judged by an independent Checker at the moment the milestone is marked"
    " completed; a failed verify reopens the milestone for repair."
)
_VERIFY_JUDGED_AT_EXIT = (
    "It is judged once at task exit from the recorded evidence around the"
    " milestone's completion; there is no midway repair loop."
)
_ASSERT_JUDGED_MIDWAY = (
    "It is judged by the independent Checker when its milestone completes (at task exit for `@end`)"
)
_ASSERT_JUDGED_AT_EXIT = "It is judged once at task exit from the recorded evidence"

_CHECK_GRAMMAR_COMMON = """- `- verify@end: ...` / `- assert@end: ...` — the `@end` suffix defers judgment to task exit, using the final device state. Items without `@end` are judged from the evidence recorded around their anchored milestone's completion.
- Capability boundary (declare honestly, never promise more): all checks are POST-HOC audits over recorded evidence. They detect and record violations after the fact; they CANNOT block an action from happening, so "B must not run before A is confirmed" ordering rules are verified retroactively, not enforced. Transient prompts that were never captured in the execution history cannot be recovered — prefer expressing expectations as persistently probeable state."""

_FINDING_GRAMMAR = """- `  - finding: ...` — a SYSTEM-authored standing headline pinned under a milestone whose verify criterion failed. You never author these lines: the system re-renders them on every plan write (deleting one only makes it reappear) and removes them automatically once the verify passes again or its repair budget is exhausted. Each finding names a `checker-...` note holding the full details and repair log — read it with `read_note`; note keys with the `checker-` prefix are reserved for the system and are read-only for you."""


def _render_check_grammar(*, midway: bool, final: bool) -> str:
    """The check-line grammar worded for the active gates.

    Midway on: verify lines are judged at milestone completion and can reopen
    the milestone; finding lines exist. Midway off (final only): every line is
    judged once at task exit, and no repair loop or finding line exists, so
    neither is taught.
    """
    verify_when = _VERIFY_JUDGED_MIDWAY if midway else _VERIFY_JUDGED_AT_EXIT
    assert_when = _ASSERT_JUDGED_MIDWAY if midway else _ASSERT_JUDGED_AT_EXIT
    lines = [
        _CHECK_GRAMMAR_HEADER,
        "- `  - verify: <expected state>` — an acceptance criterion for its parent"
        f" milestone. {verify_when}",
        f"- `  - assert: <expected observation>` — a test assertion. {assert_when} and a"
        " failure is recorded verbatim as a legitimate test result; assertions are"
        " NEVER repaired, worked around, or satisfied by constructing state.",
        _CHECK_GRAMMAR_COMMON,
    ]
    if midway:
        lines.append(_FINDING_GRAMMAR)
    return "\n".join(lines)


def render_plan_grammar_spec(
    include_checks: bool | None = None, *, midway: bool = False, final: bool = False
) -> str:
    """Renders the grammar spec taught to agents.

    ``midway`` / ``final`` are the two check gates
    (``ExecutionSetup.midway_checks_enabled`` / ``final_check_enabled``); the
    check-line grammar is worded for whichever is active. Both off returns
    exactly the base grammar — zero context pollution from the checking
    feature. ``include_checks`` is a compatibility shim meaning "both gates".
    """
    if include_checks is not None:
        midway = final = bool(include_checks)
    if midway or final:
        return _PLAN_GRAMMAR_BASE + "\n\n" + _render_check_grammar(midway=midway, final=final)
    return _PLAN_GRAMMAR_BASE


#: Backward-compatible module constant: the base grammar without the check
#: extension. Prompt injection sites use :func:`render_plan_grammar_spec`.
PLAN_GRAMMAR_SPEC = _PLAN_GRAMMAR_BASE


def _with_status(line: str, status: str) -> str:
    """Đổi **duy nhất** ký tự trạng thái checkbox của một dòng, giữ nguyên phần còn lại."""
    match = CHECKBOX_LINE_RE.match(line)
    if not match:
        return line
    return f"{line[: match.start('status')]}{status}{line[match.end('status') :]}"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def plan_self_heal_candidate(task_plan: str, *, prefer: str = "") -> tuple[str, str] | None:
    """Kế hoạch **tự mở** ``[/]`` khi chỉ có **một** ứng viên hợp lệ; ``None`` khi mơ hồ.

    Vì sao cần (session ``d110b1ef``, task ``1371db04``): khi kế hoạch không còn dòng ``[/]`` nào,
    gate ledger **bounce** hành động và bắt agent tự sửa — đo được **10 lần bounce**, mỗi lần tốn một
    lượt LLM. Phần lớn các lần đó việc phải làm là **xác định duy nhất**: chỉ một milestone chưa xong,
    hoặc một dòng con chưa xong dưới milestone đang làm. Code tự mở ``[/]`` trong đúng trường hợp đó,
    và **giữ nguyên gate** (kèm lý do) khi có nhiều hơn một ứng viên — không đoán.

    Trả ``(kế hoạch đã sửa, lý do)``. Chỉ **ký tự trạng thái** của (các) dòng được mở bị đổi; mọi
    dòng khác — kể cả ``verify``/``assert`` — giữ nguyên từng ký tự. ``prefer`` là văn bản công đoạn
    đang làm (từ ``ExecutionState``) dùng để phá thế mơ hồ khi nó khớp **đúng một** ứng viên.
    """
    snapshot = parse_plan(task_plan or "")
    if not snapshot.has_top_level:
        return None

    lines = task_plan.split("\n")
    reasons: list[str] = []

    milestone = snapshot.active_milestone()
    if milestone is None:
        candidates = [item for item in snapshot.top_level if not item.is_done]
        if len(candidates) != 1:
            return None
        milestone = candidates[0]
    else:
        other_open = [
            item for item in snapshot.top_level if not item.is_done and item.key != milestone.key
        ]
        if other_open:
            return None  # còn milestone khác chưa xong ⇒ mơ hồ, để agent tự quyết

    if not milestone.is_active:
        lines[milestone.line_no] = _with_status(lines[milestone.line_no], STATUS_ACTIVE)
        reasons.append(f'mở milestone «{milestone.text[:60]}»')

    children = [item for item in snapshot.children_of(milestone) if not item.is_done]
    if snapshot.active_leaf(milestone) is None and children:
        hint = _normalize_text(prefer)
        hinted = [item for item in children if hint and hint == _normalize_text(item.text)]
        if len(hinted) == 1:
            children = hinted
        if len(children) != 1:
            return None
        leaf = children[0]
        lines[leaf.line_no] = _with_status(lines[leaf.line_no], STATUS_ACTIVE)
        reasons.append(f'mở sub-goal «{leaf.text[:60]}»')

    if not reasons:
        return None
    return "\n".join(lines), "; ".join(reasons)


def _plan_digest(plan_text: str) -> str:
    import hashlib

    return hashlib.sha256((plan_text or "").encode("utf-8")).hexdigest()


def plan_digest(plan_text: str) -> str:
    """Vân tay nội dung kế hoạch — dùng để biết "trạng thái có đổi không"."""
    return _plan_digest(plan_text)


def plan_ledger_candidates(task_plan: str) -> list[str]:
    """Các ứng viên còn mơ hồ của ledger (để gate nói **rõ** agent phải chọn cái nào).

    Dùng khi tự chữa không được: thay vì lặp lại cùng một câu "hãy mở `[/]`", gate liệt kê đúng các
    milestone/dòng con đang mở ⇒ lượt sau là thông tin **mới**, không phải bản lặp.
    """
    snapshot = parse_plan(task_plan or "")
    if not snapshot.has_top_level:
        return []
    milestone = snapshot.active_milestone()
    if milestone is None:
        return [item.text for item in snapshot.top_level if not item.is_done]
    children = [item.text for item in snapshot.children_of(milestone) if not item.is_done]
    return children or [milestone.text]


def repeat_bounce_message(plan_text: str) -> str:
    """Thông điệp cho lần bounce **lặp** (kế hoạch không đổi): nêu **ứng viên cụ thể**, không lặp câu cũ.

    Bất biến quan trọng: hàm này luôn trả một câu **chặn** action (``Not executed: …``) — không bao
    giờ trả chuỗi rỗng để nơi gọi vô tình cho action vượt gate khi kế hoạch vẫn không hợp lệ.
    """
    candidates = plan_ledger_candidates(plan_text)
    listed = "; ".join(f"«{text[:70]}»" for text in candidates[:4]) or "«(không đọc được mục nào)»"
    return (
        "Not executed: the plan ledger is still invalid (no milestone or sub-goal marked `[/]`)."
        " Pick exactly one of the open items and open it with update_note (key task_plan):"
        f" {listed}."
    )


def repeat_bounce_suppressed(previous: tuple[str, str] | None, plan_text: str) -> bool:
    """Đã từng bounce vì **đúng kế hoạch này** chưa? (⇒ lần này không bounce lại).

    Yêu cầu "không bounce lặp cùng một lý do nếu không có thay đổi trạng thái": lý do bounce sinh ra
    từ chính nội dung kế hoạch, nên **kế hoạch không đổi** nghĩa là cùng một lý do. Trả ``False`` khi
    chưa từng bounce, hoặc khi kế hoạch đã đổi (có tiến triển ⇒ được phép nhắc lại).
    """
    if previous is None:
        return False
    _reason, digest = previous
    return digest == _plan_digest(plan_text)
