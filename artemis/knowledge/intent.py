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

"""Understand the request *before* touching the knowledge base.

**The failure this exists to fix.**  Retrieval used to run one query — the whole
sentence — and the top-K workflows were stitched into the prompt.  With
``mở app và đăng nhập rồi chuyển sang nhà dled`` that produced:

* ``home.switch_house_modal`` (a *rule* that says "close this modal") presented as
  the way to switch house,
* ``auth.switch_account`` (the saved-account flow) assumed to be the way to log in,
* ``auth.logout`` riding along because a KB alias literally reads
  "đăng nhập tài khoản khác".

None of that is a retrieval bug in the RRF sense: every hit shared words with the
sentence.  The missing step was **understanding that the sentence is three tasks
with conditions**, and that a workflow can be *relevant* without being the *goal*.

**What this module does**

1. :func:`analyze_intent` — one LLM call turns the sentence into ordered tasks
   (action / target / condition) plus a final goal.  Every task must be grounded
   in the tester's own words, so an action nobody asked for ("đăng xuất") cannot
   appear.
2. :func:`plan_queries` — one retrieval query per task, so "chuyển sang nhà dled"
   is searched as its own goal instead of drowning in the login words.
3. :func:`classify_task` — every retrieved workflow is labelled PRIMARY (it *is*
   this action), SUPPORTING (context, state, a rule) or IRRELEVANT (shares words
   only).  The label decides **what content may enter the prompt**: a SUPPORTING
   workflow contributes one line of context and can never become a step.

Nothing here is domain-specific: no list of house names, verbs or workflow ids.
The only vocabulary this module knows is the KB's own schema (a workflow tagged
``rule`` is a rule) and the tester's words from the request itself.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from artemis.knowledge.models import Workflow, WorkflowHit
from artemis.knowledge.text import (
    content_tokens,
    coverage,
    identity_tokens,
    nfc,
    normalize,
)

logger = logging.getLogger(__name__)

# ── Vai trò của một workflow đối với một task ────────────────────────────
#: Workflow *là* hành động này: nó định nghĩa mục tiêu và tiêu chí hoàn thành.
ROLE_PRIMARY = "primary"
#: Workflow là bối cảnh: luật của app, modal, trạng thái, một bước phụ.  Nó không
#: bao giờ được trình bày như mục tiêu hay như các bước phải làm.
ROLE_SUPPORTING = "supporting"
#: Workflow chỉ trùng từ ngữ, không phục vụ mục tiêu này.
ROLE_IRRELEVANT = "irrelevant"

ROLE_ORDER = {ROLE_IRRELEVANT: 0, ROLE_SUPPORTING: 1, ROLE_PRIMARY: 2}

#: Tag đánh dấu một workflow là **luật** chứ không phải chức năng.  Đây là từ vựng
#: của chính knowledge base (xem ``artemis/knowledge/README.md``), không phải danh
#: sách từ khoá theo miền nghiệp vụ: nó nói về *loại* workflow, không nói về app nào.
RULE_TAGS = frozenset({"rule", "modal", "warning", "policy", "note"})

#: Số task tối đa nhận từ LLM.  Một câu dài hơn thế là dấu hiệu model đang bịa thêm
#: bước, và mỗi task là một truy vấn embedding nên trần này cũng là trần chi phí.
MAX_TASKS = 4
#: Trần ký tự cho mỗi trường của một task, để một câu trả lời dài không phình prompt.
MAX_FIELD_CHARS = 90
#: Một task phải được câu của tester bao phủ bao nhiêu phần trăm token nội dung.  Đây
#: là chốt chặn "không tự thêm thao tác": một hành động không có trong câu của tester
#: không thể vào kế hoạch.  Ngưỡng cao (0.6) vì chia sẻ *một* token là chưa đủ —
#: "đăng xuất" và "đăng nhập" chia sẻ token "đăng", và đó chính là cặp đã gây lỗi.
MIN_TASK_COVERAGE = 0.6
#: Tỉ lệ token nội dung của prompt mà kế hoạch phải bao phủ.
MIN_PLAN_COVERAGE = 0.5

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


# ── Kế hoạch ─────────────────────────────────────────────────────────────
@dataclass
class TaskSpec:
    """Một mục tiêu đơn lẻ trong câu của tester.

    ``condition`` là *điều kiện để làm*, không phải một bước: "chỉ khi chưa đăng
    nhập", "chỉ khi nhà hiện tại khác X".  Nó tồn tại để agent không đăng nhập lại
    khi đã đăng nhập, không chuyển nhà khi đã ở đúng nhà.
    """

    action: str
    target: str = ""
    condition: str = ""
    required: bool = True
    source: str = "llm"

    def query(self) -> str:
        """Truy vấn truy xuất cho task này: hành động + đối tượng của nó."""
        return " ".join(part for part in (self.action, self.target) if part).strip()

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "target": self.target,
            "condition": self.condition,
            "required": self.required,
            "query": self.query(),
            "source": self.source,
        }


@dataclass
class IntentPlan:
    """Câu của tester sau khi được hiểu: các task theo thứ tự + mục tiêu cuối."""

    tasks: list[TaskSpec]
    final_goal: dict = field(default_factory=dict)
    source: str = "deterministic"
    reason: str = ""

    @property
    def primary_task(self) -> TaskSpec | None:
        """Task cuối cùng — mục tiêu mà tester thực sự muốn đạt."""
        return self.tasks[-1] if self.tasks else None

    def as_dict(self) -> dict:
        return {
            "tasks": [task.as_dict() for task in self.tasks],
            "final_goal": dict(self.final_goal),
            "source": self.source,
            "reason": self.reason,
        }


@dataclass
class RoleAssignment:
    """Vai trò của một workflow đối với một task, kèm bằng chứng đã dùng."""

    workflow_id: str
    role: str
    reason: str = ""
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "role": self.role,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


def deterministic_plan(prompt: str, reason: str = "") -> IntentPlan:
    """Kế hoạch một task = cả câu, dùng khi không có LLM hoặc LLM trả về rác.

    Đây là hành vi trước đây (một truy vấn cho cả câu), nhưng vẫn đi qua tầng phân
    loại vai trò, nên một workflow luật không thể trở thành mục tiêu và một khoảng
    trống vẫn được báo ra.
    """
    text = nfc(" ".join((prompt or "").split()))
    return IntentPlan(
        tasks=[TaskSpec(action=text, source="deterministic")],
        final_goal={},
        source="deterministic",
        reason=reason or "Không phân tích được mục tiêu — coi cả câu là một mục tiêu",
    )


def plan_queries(plan: IntentPlan) -> list[str]:
    """Truy vấn cho từng task, giữ thứ tự và bỏ trùng (không truy vấn hai lần)."""
    seen: set[str] = set()
    out: list[str] = []
    for task in plan.tasks:
        query = task.query() or task.action
        key = normalize(query)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(query)
    return out


def is_rule(workflow: Workflow) -> bool:
    """True khi workflow là *luật của app* (modal, cảnh báo), không phải chức năng."""
    tags = {str(tag).strip().lower() for tag in (workflow.tags or [])}
    if tags & RULE_TAGS:
        return True
    kind = str((workflow.metadata or {}).get("kind", "")).strip().lower()
    return kind in RULE_TAGS


# ── Phân tích mục tiêu bằng LLM ──────────────────────────────────────────
ANALYSIS_SYSTEM = (
    "Bạn tách một yêu cầu kiểm thử app Android thành các MỤC TIÊU có cấu trúc.\n"
    "Chỉ trả về JSON, không giải thích, không markdown.\n"
    "Định dạng:\n"
    '{"tasks":[{"action":"<động từ + việc, tiếng Việt, ngắn>","target":"<đối tượng cụ thể '
    'nếu có, ví dụ tên thiết bị/nhà/tài khoản>","condition":"<khi nào mới cần làm, ví dụ '
    '"chỉ khi chưa đăng nhập">","required":true}],"final_goal":{"type":"<đối tượng được '
    'kiểm chứng>","expected_value":"<giá trị mong đợi>"}}\n'
    "Quy tắc bắt buộc:\n"
    "1. Giữ nguyên mọi định danh: tên nhà, tên thiết bị, số điện thoại, email, mã. "
    "Chép y nguyên từng ký tự, không đổi thành tên khác.\n"
    "2. Mỗi task phải là một việc CÓ TRONG câu của tester. Không thêm việc không được "
    "yêu cầu (đăng xuất, xoá, đổi cài đặt...).\n"
    "3. Giữ đúng thứ tự thời gian trong câu.\n"
    "4. Phân biệt MỤC TIÊU với ĐIỀU KIỆN: 'đăng nhập rồi chuyển nhà X' nghĩa là mục tiêu "
    "chính là chuyển nhà X, còn đăng nhập chỉ là điều kiện cần nếu chưa đăng nhập.\n"
    "5. Mục tiêu cuối cùng (việc tester thực sự muốn đạt) đặt ở task CUỐI và required=true.\n"
    "6. Việc chỉ cần làm khi trạng thái chưa đúng thì ghi vào condition, ví dụ "
    "'chỉ khi chưa đăng nhập', 'chỉ khi nhà hiện tại khác X'. Không biến điều kiện "
    "thành một bước bắt buộc.\n"
    "7. Nếu câu có nhiều việc thì tách thành nhiều task, tối đa 4. Nếu không rõ thì để "
    "một task với cả câu.\n"
    "8. Không suy diễn thêm chi tiết không có trong câu."
)


def build_analysis_message(prompt: str, app_label: str = "") -> str:
    app = f"[APP ĐANG TEST] {app_label}\n\n" if app_label else ""
    return f"{app}[YÊU CẦU CỦA TESTER]\n{prompt.strip()}\n\nTrả về JSON."


def _text_of(value: object) -> str:
    """Lấy nội dung text từ synthesizer (chấp nhận str hoặc object có ``.text``)."""
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    return text if isinstance(text, str) else ""


def extract_json(text: str) -> dict | None:
    """Đọc object JSON đầu tiên trong câu trả lời (bỏ code fence nếu có)."""
    raw = (text or "").strip()
    fenced = _JSON_FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean_field(value: object) -> str:
    text = nfc(" ".join(str(value or "").split()))
    return text[:MAX_FIELD_CHARS].strip()


def _plan_from_payload(payload: dict, prompt: str, *, max_tasks: int) -> IntentPlan | None:
    """Validate JSON của LLM; trả None khi kế hoạch không dùng được.

    Ba cửa kiểm tra, mỗi cửa chặn một kiểu hỏng cụ thể:

    * **Có ít nhất một task** — không có kế hoạch thì không có gì để truy xuất.
    * **Mỗi task bám vào câu của tester** — hành động phải chia sẻ ít nhất một token
      nội dung với prompt, nên "đăng xuất" không thể xuất hiện nếu tester không nói.
    * **Định danh được giữ** — mọi số điện thoại / tên nhà / mã trong prompt phải còn
      trong kế hoạch, nếu không thì model đã "chuẩn hoá" mất dữ liệu của tester.
    """
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        return None

    prompt_tokens = set(content_tokens(prompt))
    tasks: list[TaskSpec] = []
    for item in raw_tasks[:max_tasks]:
        if not isinstance(item, dict):
            continue
        action = _clean_field(item.get("action"))
        if not action:
            continue
        target = _clean_field(item.get("target"))
        condition = _clean_field(item.get("condition"))
        action_tokens = content_tokens(f"{action} {target}")
        grounded = coverage(action_tokens, " ".join(sorted(prompt_tokens)))
        if grounded < MIN_TASK_COVERAGE:
            logger.info("Bỏ task không có căn cứ trong prompt: %r (%.2f)", action, grounded)
            continue
        tasks.append(
            TaskSpec(
                action=action,
                target=target,
                condition=condition,
                required=bool(item.get("required", True)),
            )
        )
    if not tasks:
        return None

    final_goal = payload.get("final_goal")
    return IntentPlan(
        tasks=tasks,
        final_goal=dict(final_goal) if isinstance(final_goal, dict) else {},
        source="llm",
    )


def _plan_covers_prompt(plan: IntentPlan, prompt: str) -> bool:
    """Kế hoạch có bao phủ đủ các từ của câu gốc hay không."""
    prompt_tokens = set(content_tokens(prompt))
    if not prompt_tokens:
        return True
    covered: set[str] = set()
    for task in plan.tasks:
        covered |= set(content_tokens(f"{task.action} {task.target} {task.condition}"))
    return coverage(sorted(prompt_tokens), " ".join(sorted(covered))) >= MIN_PLAN_COVERAGE


def analyze_intent(
    prompt: str,
    *,
    synthesizer: Any | None = None,
    app_label: str = "",
    max_tasks: int = MAX_TASKS,
    max_tokens: int = 400,
    timeout: float = 20.0,
) -> IntentPlan:
    """Hiểu yêu cầu thành các task.  Không bao giờ raise.

    Không có LLM (hoặc LLM trả về rác) thì lùi về kế hoạch một task: hành vi cũ,
    nhưng vẫn qua tầng phân loại vai trò nên vẫn an toàn.
    """
    text = nfc(" ".join((prompt or "").split()))
    if not text:
        return deterministic_plan(prompt, "Prompt rỗng")
    if synthesizer is None:
        return deterministic_plan(prompt, "Chưa cấu hình LLM để phân tích mục tiêu")

    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM},
        {"role": "user", "content": build_analysis_message(text, app_label)},
    ]
    try:
        raw = _text_of(synthesizer(messages, max_tokens=max_tokens, timeout=timeout))
    except Exception as exc:  # noqa: BLE001 - phân tích hỏng thì lùi về một task
        logger.warning("Phân tích mục tiêu thất bại: %s", exc)
        return deterministic_plan(prompt, f"LLM phân tích lỗi: {exc}")

    payload = extract_json(raw)
    if payload is None:
        return deterministic_plan(prompt, "LLM không trả về JSON hợp lệ cho mục tiêu")

    plan = _plan_from_payload(payload, text, max_tasks=max_tasks)
    if plan is None:
        return deterministic_plan(prompt, "Kế hoạch của LLM không dùng được (thiếu căn cứ)")

    # Định danh kiểm trước: mất một tên nhà/số điện thoại là lỗi nặng hơn bỏ sót một từ.
    dumped = nfc(json.dumps(plan.as_dict(), ensure_ascii=False))
    missing = [token for token in identity_tokens(text) if nfc(token) not in dumped]
    if missing:
        return deterministic_plan(prompt, "Kế hoạch làm mất định danh: " + ", ".join(missing[:3]))
    if not _plan_covers_prompt(plan, text):
        return deterministic_plan(prompt, "Kế hoạch bỏ sót quá nhiều từ của câu gốc")
    return plan


# ── Phân loại workflow theo từng task ────────────────────────────────────
def classify_task(
    task: TaskSpec,
    hits: Sequence[WorkflowHit],
    *,
    min_intent_coverage: float,
    min_name_coverage: float,
) -> list[RoleAssignment]:
    """Gán vai trò cho từng workflow đã truy xuất cho ``task``.

    Cách quyết định — không có danh sách từ khoá miền nghiệp vụ nào:

    * Workflow được KB đánh dấu là **luật** (tag ``rule``/``modal``…) → SUPPORTING.
      Một luật nói *phải làm gì khi gặp trạng thái X*; nó không bao giờ là chức năng.
    * Tên workflow phủ được hành động của task **và** hit đó có bằng chứng từ vựng
      (``trusted``) → PRIMARY.  Tên là phát biểu chính thức về mục đích của workflow.
    * Chỉ *mô tả/alias* phủ hành động → SUPPORTING: trùng từ, nhưng mục đích không
      phải việc này (ví dụ workflow đăng xuất có alias "đăng nhập tài khoản khác").
    * Không phủ gì → IRRELEVANT.
    """
    action_tokens = content_tokens(task.action)
    assignments: list[RoleAssignment] = []
    for hit in hits:
        workflow = hit.workflow
        name_cov = coverage(action_tokens, normalize(workflow.name))
        intent_cov = coverage(action_tokens, normalize(workflow.embedding_text()))
        evidence = {
            "name_coverage": round(name_cov, 3),
            "intent_coverage": round(intent_cov, 3),
            "trusted": hit.trusted,
            "channels": dict(hit.channels),
            "is_rule": is_rule(workflow),
        }
        if is_rule(workflow):
            if intent_cov >= min_intent_coverage:
                role, reason = (
                    ROLE_SUPPORTING,
                    "workflow luật/modal — xử lý trạng thái, không phải chức năng",
                )
            else:
                role, reason = (
                    ROLE_IRRELEVANT,
                    "workflow luật nhưng không liên quan tới việc này",
                )
        elif name_cov >= min_name_coverage and hit.trusted:
            role, reason = ROLE_PRIMARY, "tên workflow đúng là hành động này"
        elif intent_cov >= min_intent_coverage:
            role, reason = ROLE_SUPPORTING, "trùng từ với hành động nhưng mục đích khác"
        else:
            role, reason = ROLE_IRRELEVANT, "không phục vụ hành động này"
        assignments.append(
            RoleAssignment(workflow_id=workflow.id, role=role, reason=reason, evidence=evidence)
        )
    return assignments


@dataclass
class TaskKnowledge:
    """Kết quả truy xuất + phân loại cho một task."""

    task: TaskSpec
    index: int
    primary: list[WorkflowHit] = field(default_factory=list)
    supporting: list[WorkflowHit] = field(default_factory=list)
    irrelevant: list[WorkflowHit] = field(default_factory=list)
    roles: list[RoleAssignment] = field(default_factory=list)
    gap_reason: str = ""

    @property
    def gap(self) -> bool:
        """True khi KB không có workflow nào *thực hiện* task này."""
        return not self.primary

    def as_dict(self) -> dict:
        return {
            "task": self.task.as_dict(),
            "primary": [hit.workflow.id for hit in self.primary],
            "supporting": [hit.workflow.id for hit in self.supporting],
            "irrelevant": [hit.workflow.id for hit in self.irrelevant],
            "roles": [role.as_dict() for role in self.roles],
            "gap": self.gap,
            "gap_reason": self.gap_reason,
        }


def build_task_knowledge(
    plan: IntentPlan,
    retrieved: Sequence[tuple[TaskSpec, Sequence[WorkflowHit]]],
    *,
    min_intent_coverage: float,
    min_name_coverage: float,
) -> list[TaskKnowledge]:
    """Phân loại hit của từng task; hit không có bằng chứng nào bị loại khỏi kết quả.

    ``retrieved`` là cặp ``(task, hits)`` theo đúng thứ tự task trong kế hoạch.
    """
    entries: list[TaskKnowledge] = []
    for index, (task, hits) in enumerate(retrieved):
        assignments = classify_task(
            task,
            hits,
            min_intent_coverage=min_intent_coverage,
            min_name_coverage=min_name_coverage,
        )
        by_id = {hit.workflow.id: hit for hit in hits}
        entry = TaskKnowledge(task=task, index=index, roles=assignments)
        for assignment in assignments:
            hit = by_id.get(assignment.workflow_id)
            if hit is None:
                continue
            if assignment.role == ROLE_PRIMARY:
                entry.primary.append(hit)
            elif assignment.role == ROLE_SUPPORTING:
                entry.supporting.append(hit)
            else:
                entry.irrelevant.append(hit)
        if entry.gap:
            entry.gap_reason = (
                "KB chưa có workflow thực hiện việc này"
                if not entry.supporting
                else "KB chỉ có workflow bối cảnh/luật cho việc này"
            )
        entries.append(entry)
    return entries


def best_role(entries: Iterable[TaskKnowledge]) -> dict[str, RoleAssignment]:
    """Vai trò mạnh nhất của mỗi workflow qua mọi task (PRIMARY > SUPPORTING)."""
    out: dict[str, RoleAssignment] = {}
    for entry in entries:
        for assignment in entry.roles:
            current = out.get(assignment.workflow_id)
            if current is None or ROLE_ORDER[assignment.role] > ROLE_ORDER[current.role]:
                out[assignment.workflow_id] = assignment
    return out


# ── Chuẩn hoá nội dung workflow ──────────────────────────────────────────
#: Từ khoá của một *cảnh báo* trong ``Workflow.metadata``.  Kiến thức đặc thù app
#: hay nằm ở đây ("modal này là bình thường, không phải lỗi"), và bỏ sót nó là
#: nguyên nhân agent báo FAILED oan.
WARNING_KEYS = ("warnings", "warning", "notes", "note", "rules", "rule", "caveats", "caveat")

_COORDINATE_PATTERNS = (
    # bounds của Android hierarchy: [28,1093,692,1198]
    re.compile(r"\[\s*\d{1,5}\s*,\s*\d{1,5}\s*,\s*\d{1,5}\s*,\s*\d{1,5}\s*\]"),
    # cặp toạ độ: (648, 1514)
    re.compile(r"\(\s*\d{1,5}\s*[,;]\s*\d{1,5}\s*\)"),
    re.compile(r"\b\d{2,5}\s*[,;]\s*\d{2,5}\s*(?:px|dp|dip)\b", re.IGNORECASE),
    re.compile(r"\b\d+\s*(?:px|dp|dip)\b", re.IGNORECASE),
)
_COORDINATE_WORDS = re.compile(nfc(r"(?i)\b(to[ạa]\s*đ[ộo]|coordinates?|bounds|pixel)\b"))


def one_line(value: object) -> str:
    """Gộp khoảng trắng và chuẩn hoá NFC, để một trường không thể giả làm mục mới."""
    return nfc(" ".join(str(value or "").split()))


def has_hard_ui(text: str) -> bool:
    """Có toạ độ/bounds/pixel trong ``text`` hay không — **chỉ phát hiện**, không sửa.

    Tách khỏi :func:`sanitize_ui_instructions` vì phép sửa còn chuẩn hoá cả khoảng trắng:
    dùng nó làm phép kiểm sẽ báo nhầm mọi văn bản có thụt lề (ví dụ một dòng trong khối
    kiến thức), và bản LLM bị từ chối vì lý do không liên quan tới toạ độ.
    """
    candidate = nfc(text or "")
    return any(pattern.search(candidate) for pattern in _COORDINATE_PATTERNS) or bool(
        _COORDINATE_WORDS.search(candidate)
    )


def sanitize_ui_instructions(text: str) -> str:
    """Xoá toạ độ và số pixel khỏi ``text`` (yêu cầu: không chỉ dẫn UI cứng).

    Agent phải quyết định dựa trên màn hình hiện tại, nên một toạ độ trong prompt
    vừa vô dụng (UI đổi là sai) vừa nguy hiểm (chạm đúng chỗ nhưng sai màn hình).
    Áp cho cả kiến thức lấy từ KB lẫn văn bản LLM viết ra.
    """
    if not text:
        return ""
    cleaned = nfc(text)
    for pattern in _COORDINATE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _COORDINATE_WORDS.sub(" ", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def workflow_warnings(workflow: Workflow) -> list[str]:
    """Cảnh báo/ghi chú của workflow, đọc từ ``metadata`` theo vài tên quen dùng."""
    out: list[str] = []
    for key in WARNING_KEYS:
        value = workflow.metadata.get(key) if isinstance(workflow.metadata, dict) else None
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            text = one_line(item)
            if text and text not in out:
                out.append(text)
    return out


# ── Dựng phần kiến thức: vai trò quyết định nội dung được phép vào prompt ──
#: Năm mục của phần kiến thức.  Bản LLM và bản dự phòng dùng đúng năm tiêu đề này,
#: nên kiểm tra chất lượng kiểm được cấu trúc chứ không chỉ kiểm nội dung.
SECTION_PLAN = "KẾ HOẠCH:"
SECTION_KNOWLEDGE = "KIẾN THỨC ỨNG DỤNG:"
SECTION_CONTEXT = "BỐI CẢNH / CẢNH BÁO:"
SECTION_GAP = "KB CHƯA CÓ HƯỚNG DẪN:"
SECTION_CRITERIA = "TIÊU CHÍ XÁC MINH:"
SECTIONS = (SECTION_PLAN, SECTION_KNOWLEDGE, SECTION_CONTEXT, SECTION_GAP, SECTION_CRITERIA)

#: Câu bắt buộc phải có khi KB thiếu workflow cho một task.
GAP_ADVICE = (
    "tự khám phá UI hiện tại; nếu không chắc thì báo blocked kèm màn hình đang thấy, "
    "KHÔNG tự suy diễn ra quy trình"
)
#: Nhắc rằng mục bối cảnh không phải việc phải làm — chống việc biến một luật/modal
#: thành bước chính (lỗi đã gặp: prompt yêu cầu chuyển nhà mà lại hướng dẫn nhấn Huỷ).
CONTEXT_DISCLAIMER = (
    "Chỉ là bối cảnh/điều kiện để nhận diện trạng thái — KHÔNG phải mục tiêu và "
    "KHÔNG phải các bước phải thực hiện."
)


def task_line(entry: TaskKnowledge) -> str:
    """Một dòng cho một task: việc gì, khi nào cần làm, và KB có gì để làm nó."""
    task = entry.task
    head = " ".join(part for part in (task.action, task.target) if part)
    if task.condition:
        head += f" — {task.condition}"
    if entry.primary:
        head += " — kiến thức: " + ", ".join(hit.workflow.id for hit in entry.primary)
    else:
        head += " — kiến thức: KB chưa có hướng dẫn"
    return head


def render_plan(entries: Sequence[TaskKnowledge]) -> list[str]:
    return [f"{index}. {task_line(entry)}" for index, entry in enumerate(entries, 1)]


def render_primary_knowledge(hits: Sequence[WorkflowHit]) -> list[str]:
    """Kiến thức của workflow PRIMARY: cách làm + điều kiện phải kiểm tra + tiêu chí.

    **Bước của workflow không được in ra** (agent tự tìm phần tử trên UI hiện tại),
    và điều kiện trước được trình bày như *điều phải kiểm tra*, không phải sự thật
    mặc định — nếu không, agent sẽ coi "tài khoản đã lưu" hay "đã đăng nhập" là đúng.
    """
    lines: list[str] = []
    for hit in hits:
        workflow = hit.workflow
        lines.append(f"- {workflow.id} — {one_line(workflow.name)} [{workflow.status}]")
        if workflow.description:
            lines.append(f"  Mô tả: {sanitize_ui_instructions(one_line(workflow.description))}")
        if workflow.preconditions:
            lines.append(
                "  Điều kiện phải kiểm tra trên UI (không được mặc định là đúng): "
                + "; ".join(
                    sanitize_ui_instructions(one_line(text)) for text in workflow.preconditions
                )
            )
        warnings = workflow_warnings(workflow)
        if warnings:
            lines.append(
                "  Cảnh báo: " + "; ".join(sanitize_ui_instructions(text) for text in warnings)
            )
        if workflow.success_criteria:
            lines.append(
                "  Tiêu chí hoàn thành: "
                + "; ".join(
                    sanitize_ui_instructions(one_line(text)) for text in workflow.success_criteria
                )
            )
    return lines


def render_supporting_context(hits: Sequence[WorkflowHit]) -> list[str]:
    """Bối cảnh: tên + mô tả một dòng.  Không điều kiện, không tiêu chí, không bước.

    Đây là chốt chặn cơ học cho "workflow phụ không được làm lệch mục tiêu": tiêu chí
    và các bước của một workflow SUPPORTING không bao giờ vào prompt, nên chúng không
    thể trở thành việc phải làm (ví dụ: "đóng modal" không thể thành mục tiêu).
    """
    lines: list[str] = []
    for hit in hits:
        workflow = hit.workflow
        description = sanitize_ui_instructions(one_line(workflow.description))
        suffix = f": {description}" if description else ""
        lines.append(f"- {workflow.id} — {one_line(workflow.name)}{suffix}")
    return lines


def render_gaps(entries: Sequence[TaskKnowledge]) -> list[str]:
    """Task không có workflow PRIMARY — nói thẳng là thiếu, không bịa quy trình."""
    return [
        f"- {task_line(entry).split(' — kiến thức')[0]}: {entry.gap_reason} — {GAP_ADVICE}."
        for entry in entries
        if entry.gap
    ]


def render_criteria(entries: Sequence[TaskKnowledge], final_goal: dict) -> list[str]:
    """Tiêu chí xác minh: mục tiêu cuối của tester + tiêu chí của workflow PRIMARY."""
    lines: list[str] = []
    goal_type = one_line(final_goal.get("type"))
    goal_value = one_line(final_goal.get("expected_value"))
    if goal_type:
        text = f"- Mục tiêu cuối: {goal_type}"
        if goal_value:
            text += f" = {goal_value}"
        lines.append(f"{text}.")
    for entry in entries:
        for hit in entry.primary:
            for criterion in hit.workflow.success_criteria:
                text = sanitize_ui_instructions(one_line(criterion))
                if text and f"- {text}" not in lines:
                    lines.append(f"- {text}")
    if not lines:
        lines.append("- Xác minh trực tiếp trên màn hình rằng mục tiêu của tester đã đạt.")
    return lines


def build_knowledge_brief(
    entries: Sequence[TaskKnowledge],
    final_goal: dict,
    *,
    max_chars: int,
) -> str:
    """Phần kiến thức đã phân loại, dạng có mục — đầu vào cho LLM tổng hợp.

    **Thứ tự ưu tiên khi hết chỗ.**  Kế hoạch, khoảng trống và tiêu chí là *cấu trúc*:
    mất chúng thì prompt mất mục tiêu hoặc mất cảnh báo thiếu kiến thức, nên chúng
    luôn được giữ trước.  Kiến thức chi tiết và bối cảnh là phần bị cắt khi hạn mức
    chật — thà thiếu một dòng mô tả còn hơn mất tiêu chí xác minh.
    """
    primary: list[WorkflowHit] = []
    seen: set[str] = set()
    for entry in entries:
        for hit in entry.primary:
            if hit.workflow.id not in seen:
                seen.add(hit.workflow.id)
                primary.append(hit)

    supporting: list[WorkflowHit] = []
    for entry in entries:
        for hit in entry.supporting:
            if hit.workflow.id not in seen:
                seen.add(hit.workflow.id)
                supporting.append(hit)

    plan_lines = render_plan(entries)
    gap_lines = render_gaps(entries)
    criteria_lines = render_criteria(entries, final_goal)
    knowledge_lines = render_primary_knowledge(primary) if primary else []
    context_lines = (
        [CONTEXT_DISCLAIMER, *render_supporting_context(supporting)] if supporting else []
    )

    # Ưu tiên: kế hoạch → khoảng trống → tiêu chí → kiến thức → bối cảnh.
    # Khoảng trống đứng trước tiêu chí vì "KB không có hướng dẫn" là thứ ngăn agent tự
    # bịa quy trình — thiếu nó nguy hiểm hơn thiếu một dòng tiêu chí.
    ordered: list[tuple[str, list[str]]] = [(SECTION_PLAN, plan_lines)]
    if gap_lines:
        ordered.append((SECTION_GAP, gap_lines))
    ordered.append((SECTION_CRITERIA, criteria_lines))
    if knowledge_lines:
        ordered.append((SECTION_KNOWLEDGE, knowledge_lines))
    if context_lines:
        ordered.append((SECTION_CONTEXT, context_lines))

    lines: list[str] = []
    length = 0
    for header, body in ordered:
        chunk = [header, *body]
        size = sum(len(line) + 1 for line in chunk)
        if length and length + size > max_chars:
            # Cắt theo mục, không cắt giữa dòng: một tiêu chí cụt sẽ khiến kiểm tra
            # chất lượng trượt và tester nhận bản dự phòng.
            break
        lines.extend(chunk)
        length += size
    return "\n".join(lines).strip()


def build_deterministic_prompt(
    original_prompt: str,
    entries: Sequence[TaskKnowledge],
    final_goal: dict,
    *,
    max_chars: int,
) -> str:
    """Enhanced Prompt dựng bằng luật, không cần LLM — dùng cho đường dự phòng.

    Cùng năm mục như bản LLM, cùng tầng phân loại vai trò, nên khi LLM hỏng tester
    vẫn nhận được một prompt có mục tiêu, có điều kiện, có khoảng trống được báo và
    có tiêu chí xác minh.
    """
    brief = build_knowledge_brief(entries, final_goal, max_chars=max_chars)
    goal = (original_prompt or "").strip()
    if not brief:
        return goal
    return f"{goal}\n\n{brief}"
