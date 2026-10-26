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

"""AI Prompt Preparation: retrieval + one LLM call -> the prompt the agent runs.

**Why this exists.**  Nối nguyên khối workflow vào prompt của tester làm prompt
dài, đầy hướng dẫn UI cứng, và agent đọc một kịch bản thay vì một mục tiêu.  Bước
chuẩn bị này giữ nguyên *phân công*:

* RAG cung cấp **kiến thức** (workflow đã duyệt, do tester nhập).
* LLM **tổng hợp** kiến thức đó thành một Enhanced Prompt ngắn, tự nhiên.
* Tester **duyệt** (xem, sửa) trước khi chạy.
* Agent chỉ **thao tác và xác minh trên UI thực tế**.

**Không có RAG thứ hai.**  Truy xuất đi qua đúng
:meth:`artemis.knowledge.service.KnowledgeService.retrieve` mà Planner vẫn dùng,
nên ``kb_retrieval_log``, trust policy (kênh từ vựng quyết định ``found``) và chi
phí embedding không đổi.  Module này chỉ *đọc* kết quả đó.

**Prompt gốc bất khả xâm phạm.**  Enhanced Prompt luôn bắt đầu bằng prompt gốc
nguyên văn; LLM chỉ được viết phần kiến thức phía sau.  Nhờ vậy "không mất mục
tiêu" và "không đổi định danh" là tính chất kiểm chứng được bằng máy
(:func:`validate_enhanced`), không phải lời hứa.

**Không có gì ở đây được phép làm hỏng một lần chạy.**  LLM lỗi, quá hạn, vượt
hạn mức hay viết ra thứ không đạt kiểm tra chất lượng ⇒ trả về prompt gốc kèm
phần kiến thức rút gọn bằng logic xác định (:func:`condense_knowledge`), và ghi rõ
``status="fallback"`` để tester biết.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
import unicodedata

from artemis.knowledge.intent import (
    CONTEXT_DISCLAIMER,
    ROLE_PRIMARY,
    ROLE_SUPPORTING,
    SECTION_CONTEXT,
    SECTION_CRITERIA,
    SECTION_GAP,
    SECTION_KNOWLEDGE,
    SECTION_PLAN,
    SECTIONS,
    IntentPlan,
    TaskKnowledge,
    analyze_intent,
    best_role,
    build_deterministic_prompt,
    build_knowledge_brief,
    build_task_knowledge,
    deterministic_plan,
    has_hard_ui,
    one_line,
    plan_queries,
    sanitize_ui_instructions,
    workflow_warnings,
)
from artemis.knowledge.models import RetrievalResult, Workflow, WorkflowHit
from artemis.knowledge.service import KnowledgeService
from artemis.knowledge.text import (
    content_tokens,
    coverage,
    identity_tokens,
    nfc,
    normalize,
)

logger = logging.getLogger(__name__)

# ── Trạng thái chuẩn bị ──────────────────────────────────────────────────
#: LLM tổng hợp thành công, đã qua kiểm tra chất lượng.
STATUS_LLM = "llm"
#: LLM lỗi/quá hạn/không đạt kiểm tra ⇒ prompt gốc + kiến thức dựng bằng luật.
STATUS_FALLBACK = "fallback"
#: Knowledge base không có workflow nào đáng tin cho chức năng này.
STATUS_NO_KNOWLEDGE = "no_knowledge"
#: Không truy cập được knowledge base.
STATUS_DEGRADED = "degraded"

#: Nhãn nguồn cho UI/log: bản này do LLM tổng hợp hay do luật dựng ra.
SOURCE_LLM = "llm"
SOURCE_DETERMINISTIC = "deterministic"

_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")
#: Token trông như id workflow (``auth.logout``) — dùng để phát hiện kiến thức bịa.
_ID_LIKE = re.compile(r"\b[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+\b")


def kb_version(workflows: Sequence[Workflow]) -> str:
    """Fingerprint phiên bản kiến thức đã dùng — khoá cache và dấu vết kiểm toán.

    Đổi bất kỳ workflow nào đã khớp (nội dung, revision, thời điểm sửa) là đổi
    fingerprint, nên cache không bao giờ trả một Enhanced Prompt được tổng hợp từ
    kiến thức cũ.  Nhận ``Workflow`` (không phải hit) để lúc kiểm tra cache có thể
    tính lại fingerprint từ chính database mà không cần truy xuất lại.
    """
    parts = [
        f"{workflow.id}:{workflow.revision_no}:{workflow.updated_at}:"
        f"{hashlib.sha256(workflow.search_text().encode('utf-8')).hexdigest()[:12]}"
        for workflow in sorted(workflows, key=lambda item: item.id)
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _current_kb_version(service: KnowledgeService, workflow_ids: Sequence[str]) -> str:
    """Fingerprint của các workflow đó *ngay bây giờ*, đọc thẳng từ store.

    Đây là phép kiểm tra cache rẻ tiền: vài lượt đọc theo khoá chính, không embedding,
    không LLM, không truy xuất.  Trả ``""`` khi một workflow đã bị xoá — coi như cache
    cũ và tính lại.
    """
    try:
        workflows = []
        for workflow_id in workflow_ids:
            workflow = service._store.get_workflow(workflow_id)
            if workflow is None:
                return ""
            workflows.append(workflow)
    except Exception as exc:  # noqa: BLE001 - không đọc được thì bỏ cache
        logger.info("Không kiểm tra được phiên bản KB cho cache: %s", exc)
        return ""
    return kb_version(workflows)


@dataclass
class Check:
    """Một tiêu chí chất lượng trên Enhanced Prompt, kèm lý do khi trượt."""

    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def _coverage_check(
    name: str,
    items: list[str],
    enhanced: str,
    floor: float,
    label: str,
) -> Check:
    """``items`` phải xuất hiện lại trong prompt mới ở mức ``floor`` độ phủ token.

    Dùng lại :func:`artemis.knowledge.text.coverage` của RAG thay vì một phép so
    khớp riêng: cùng một định nghĩa "thế nào là khớp" cho cả hai phía hệ thống.
    """
    if not items:
        return Check(name, True, f"Không có {label}")
    haystack = normalize(enhanced)
    dropped: list[str] = []
    for item in items:
        tokens = content_tokens(item)
        if tokens and coverage(tokens, haystack) < floor:
            dropped.append(one_line(item)[:80])
    return Check(
        name,
        not dropped,
        "" if not dropped else f"Bỏ sót {label}: " + " | ".join(dropped[:3]),
    )


def _mentions_id(text: str, ids: set[str]) -> list[str]:
    """Id trông như workflow trong ``text`` mà không nằm trong ``ids`` (bịa)."""
    return sorted({token for token in _ID_LIKE.findall(text or "") if token not in ids})


# ── LLM tổng hợp ─────────────────────────────────────────────────────────
class SynthesisError(RuntimeError):
    """LLM không tổng hợp được (lỗi mạng, 4xx/5xx, quá hạn, trả rỗng)."""


#: Chữ ký của hàm gọi LLM.  Console truyền vào một hàm đi qua AI proxy hiện có;
#: test truyền vào một hàm giả.  Trả về nội dung assistant, hoặc một object có
#: ``.text`` (khi đó ``.usage`` được ghi vào metrics).
Synthesizer = Callable[..., str]


SYSTEM_PROMPT = (
    "Bạn viết phần HƯỚNG DẪN THỰC THI cho một agent tự động hoá Android, dựa trên "
    "KẾ HOẠCH và KIẾN THỨC đã được phân loại sẵn.\n"
    "Bạn KHÔNG viết lại mục tiêu của tester và KHÔNG được thêm việc mới.\n"
    "Bắt buộc giữ đúng các mục sau, theo thứ tự, mỗi mục có dòng tiêu đề riêng:\n"
    f"{SECTION_PLAN} các việc theo thứ tự; mỗi dòng nêu việc + điều kiện thực hiện "
    "(ví dụ 'chỉ khi chưa đăng nhập'); giữ nguyên mọi định danh (tên nhà, thiết bị, "
    "số điện thoại, mã).\n"
    f"{SECTION_KNOWLEDGE} chỉ kiến thức của workflow THỰC HIỆN việc đó: cách làm, điều "
    "kiện phải kiểm tra trên UI, cảnh báo, tiêu chí. Viết ngắn, gộp câu trùng nhau.\n"
    f"{SECTION_CONTEXT} chỉ các mục bối cảnh/luật, mỗi mục MỘT dòng, nêu rõ đây là bối "
    "cảnh để nhận diện trạng thái, KHÔNG phải việc phải làm. Không có thì bỏ mục này.\n"
    f"{SECTION_GAP} các việc knowledge base chưa có hướng dẫn: nói thẳng là chưa có, "
    "dặn tự khám phá UI hiện tại và không tự suy diễn quy trình. Không có thì bỏ mục này.\n"
    f"{SECTION_CRITERIA} tiêu chí để kết luận ĐẠT.\n"
    "Quy tắc không có ngoại lệ:\n"
    "1. Không đổi số điện thoại, tên nhà, tên thiết bị, mã, số lượng, URL.\n"
    "2. Không thêm mục tiêu, không bỏ mục tiêu, không biến điều kiện thành bước bắt buộc.\n"
    "3. Không biến bối cảnh/luật thành việc phải làm. Nếu kiến thức chỉ có luật xử lý một "
    "modal thì tuyệt đối không viết thao tác đóng modal như một bước của mục tiêu.\n"
    "4. Không bịa chức năng, phần tử UI hay quy trình không có trong kiến thức.\n"
    "5. Không đưa toạ độ, bounds, pixel, tỷ lệ màn hình hay vị trí cố định.\n"
    "6. Không lặp lại các quy tắc chung của agent (không dùng toạ độ, chỉ thao tác trong "
    "app, giới hạn số bước...).\n"
    "7. Không mặc định điều kiện trước là đúng (tài khoản đã lưu, đã đăng nhập, đã ở đúng "
    "nhà...): luôn viết ở dạng phải kiểm tra trên UI.\n"
    "8. Mọi điều kiện phải kiểm tra, cảnh báo và tiêu chí của workflow THỰC HIỆN phải còn "
    "đủ ý trong bản viết lại. Được phép diễn đạt ngắn hơn, KHÔNG được bỏ mục nào — kể cả "
    "chi tiết trong ngoặc.\n"
    "9. Không thêm bước nhập liệu (số điện thoại, mật khẩu, OTP, mã xác minh) nếu kiến "
    "thức không nói tới. Nhiều workflow dùng tài khoản đã lưu nên KHÔNG cần nhập gì; viết "
    "thừa một bước như vậy là sai.\n"
    "10. Ngắn: tối đa {max_chars} ký tự cho toàn bộ phần này, tiếng Việt, không markdown "
    "cầu kỳ, không mở bài, không kết luận."
)


def build_user_message(goal: str, brief: str, profile: str) -> str:
    """Nội dung gửi cho LLM: mục tiêu gốc + kế hoạch/kiến thức đã phân loại."""
    return (
        "[YÊU CẦU GỐC CỦA TESTER — không được viết lại]\n"
        f"{goal.strip()}\n\n"
        "[KẾ HOẠCH + KIẾN THỨC ĐÃ PHÂN LOẠI — nguồn duy nhất được dùng]\n"
        f"{brief.strip()}\n\n"
        f"[PROFILE CHẠY] {profile}\n"
        "Hãy viết lại thành các mục theo đúng yêu cầu, giữ nguyên định danh và điều kiện."
    )


def _clean_llm_output(text: str, goal: str) -> str:
    """Bỏ code fence, bỏ phần LLM lỡ lặp lại prompt gốc, chuẩn hoá khoảng trắng."""
    cleaned = nfc(_FENCE.sub("", (text or "").strip()))
    cleaned = sanitize_ui_instructions(cleaned)
    stripped_goal = goal.strip()
    if stripped_goal and normalize(cleaned).startswith(normalize(stripped_goal)):
        cleaned = cleaned[len(stripped_goal) :].lstrip(" \n:—-")
    return cleaned.strip()


def enhance_with_llm(
    goal: str,
    brief: str,
    profile: str,
    synthesizer: Synthesizer,
    *,
    max_chars: int,
    max_tokens: int,
    timeout: float,
) -> tuple[str, dict]:
    """Một lượt LLM, tối đa ``max_tokens``; trả ``(nội dung, usage)``."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(max_chars=max_chars)},
        {"role": "user", "content": build_user_message(goal, brief, profile)},
    ]
    raw = synthesizer(messages, max_tokens=max_tokens, timeout=timeout)
    usage = getattr(raw, "usage", None)
    text = raw if isinstance(raw, str) else getattr(raw, "text", "")
    if not isinstance(text, str) or not text.strip():
        raise SynthesisError("LLM trả về nội dung rỗng")
    return _clean_llm_output(text, goal), dict(usage) if isinstance(usage, dict) else {}


def assemble_prompt(original_prompt: str, knowledge: str) -> str:
    """Enhanced Prompt = prompt gốc nguyên văn + phần hướng dẫn.

    Thứ tự này là hợp đồng: phần đầu là của tester, phần sau do hệ thống thêm.  Nó
    cũng khiến "prompt gốc được bảo toàn" trở thành bất biến cấu trúc.
    """
    goal = (original_prompt or "").strip()
    block = nfc((knowledge or "").strip())
    if not block:
        return goal
    return f"{goal}\n\n{block}"


def failed_checks(checks: list[Check]) -> list[Check]:
    return [check for check in checks if not check.ok]


# ── Kiểm tra chất lượng đầu ra ───────────────────────────────────────────
def _supporting_only(entries: Sequence[TaskKnowledge]) -> list[WorkflowHit]:
    """Workflow chỉ có vai trò SUPPORTING ở mọi task (không bao giờ là mục tiêu)."""
    primary_ids = {hit.workflow.id for entry in entries for hit in entry.primary}
    out: list[WorkflowHit] = []
    seen: set[str] = set()
    for entry in entries:
        for hit in entry.supporting:
            if hit.workflow.id in primary_ids or hit.workflow.id in seen:
                continue
            seen.add(hit.workflow.id)
            out.append(hit)
    return out


def _sections_required(entries: Sequence[TaskKnowledge], brief: str) -> list[str]:
    """Các mục mà bản LLM phải giữ, suy từ chính phần kiến thức đã gửi cho nó.

    Một mục chỉ được *đòi* khi nó thật sự có trong đầu vào: nếu hạn mức ký tự đã cắt
    mục bối cảnh thì bản LLM không thể tái tạo nó, và bắt lỗi vì thế là bắt sai người.
    """
    required = [SECTION_PLAN, SECTION_CRITERIA]
    if SECTION_KNOWLEDGE in brief:
        required.append(SECTION_KNOWLEDGE)
    if SECTION_CONTEXT in brief:
        required.append(SECTION_CONTEXT)
    if SECTION_GAP in brief:
        required.append(SECTION_GAP)
    return required


#: Độ phủ token tối thiểu của *hành động* trong phần chỉ dẫn.  Cao hơn ``coverage_floor``
#: vì hành động ngắn (2 token) rất dễ bị phủ một nửa do trùng từ tình cờ — "chuyển nhà"
#: khớp "Chuyển tài khoản" trong tên một workflow khác — và khi đó phép kiểm "không mất
#: việc" sẽ im lặng cho qua.
MIN_TASK_INSTRUCTION_COVERAGE = 0.8


def _covers(task: Any, enhanced: str, floor: float) -> bool:
    """Việc của task còn xuất hiện trong *phần chỉ dẫn* của prompt mới hay không.

    Phải soi phần chỉ dẫn (KẾ HOẠCH + KIẾN THỨC), không soi cả prompt: prompt gốc luôn
    nằm ở đầu và luôn chứa đủ từ của mọi việc, nên nếu soi cả prompt thì phép kiểm này
    không bao giờ phát hiện được một việc bị bỏ sót.
    """
    body = normalize(_instruction_sections(enhanced))
    action = content_tokens(task.action)
    if action and coverage(action, body) < max(floor, MIN_TASK_INSTRUCTION_COVERAGE):
        return False
    # Hành động có thể bị diễn đạt lại, nhưng đối tượng cụ thể thì không được mất.
    target = content_tokens(task.target)
    return not target or coverage(target, body) >= floor


#: Nhãn UI trong ngoặc kép — đơn vị *hành động được* của một workflow
#: (``nút 'Huỷ'``, ``chọn 'Xác Nhận'``).  Đây là thứ nguy hiểm khi lọt vào prompt.
_QUOTED_LABEL = re.compile(r"[\"'“”‘’]([^\"'“”‘’\n]{2,40})[\"'“”‘’]")


def _quoted_labels(workflow: Workflow) -> list[str]:
    """Nhãn UI được trích dẫn trong các bước và tiêu chí của một workflow."""
    texts = [str(step.target or "") for step in workflow.steps]
    texts.extend(str(text or "") for text in workflow.success_criteria)
    out: list[str] = []
    for text in texts:
        for label in _QUOTED_LABEL.findall(text):
            cleaned = one_line(label)
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return out


def _contains_word(haystack: str, needle: str) -> bool:
    """True khi ``needle`` xuất hiện trong ``haystack`` như một **từ**, không phải chuỗi con.

    Cần thiết vì tiếng Việt không dấu có nhiều chuỗi con trùng nhau: "huy" nằm trong
    "chuyen", nên so khớp chuỗi con sẽ bỏ qua đúng nhãn nguy hiểm cần bắt.
    """
    if not needle:
        return False
    return re.search(rf"(?<![0-9a-z]){re.escape(needle)}(?![0-9a-z])", haystack) is not None


def _split_sections(text: str) -> dict[str, list[str]]:
    """Tách phần có mục thành ``{tiêu đề: [dòng nội dung]}`` theo thứ tự xuất hiện."""
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in (text or "").splitlines():
        header = line.strip()
        if header in SECTIONS:
            current = header
            blocks.setdefault(current, [])
            continue
        if current is not None and line.strip():
            blocks[current].append(line)
    return blocks


def _instruction_sections(enhanced: str) -> str:
    """Phần prompt chứa *chỉ dẫn thực thi*: KẾ HOẠCH và KIẾN THỨC ỨNG DỤNG.

    Bối cảnh nằm ở mục riêng, nên giới hạn phạm vi kiểm tra vào hai mục này khiến
    phép kiểm không báo nhầm khi tên/mô tả của một luật xuất hiện hợp lệ ở mục bối cảnh.
    """
    blocks = _split_sections(enhanced)
    lines: list[str] = []
    for header in (SECTION_PLAN, SECTION_KNOWLEDGE):
        lines.extend(blocks.get(header, []))
    return "\n".join(lines)


def _ensure_sections(knowledge: str, brief: str, entries: Sequence[TaskKnowledge]) -> str:
    """Chèn lại nguyên văn những mục mà LLM bỏ sót.

    Mục là *cấu trúc*, không phải văn phong: thiếu một mục thì bản LLM mất thông tin đã
    được phân loại sẵn mà nội dung phần còn lại vẫn đúng, nên chèn lại rẻ hơn — và an
    toàn hơn cho tester — so với từ chối cả bản.  Nội dung chèn vào chính là phần đã
    được dựng theo vai trò, không phải văn bản do LLM sinh thêm.
    """
    blocks = _split_sections(brief)
    missing = [header for header in _sections_required(entries, brief) if header not in knowledge]
    if not missing:
        return knowledge
    parts = [knowledge.strip()]
    for header in SECTIONS:
        if header in missing and blocks.get(header):
            parts.append(f"{header}\n" + "\n".join(blocks[header]))
    return "\n\n".join(part for part in parts if part)


def validate_enhanced(
    original_prompt: str,
    enhanced_prompt: str,
    entries: Sequence[TaskKnowledge],
    *,
    digest: str = "",
    max_enhanced_chars: int = 6000,
    coverage_floor: float = 0.5,
    conflict_floor: float = 0.8,
) -> list[Check]:
    """Kiểm tra chất lượng đầu ra — điều kiện để dùng bản LLM.

    Mỗi tính chất là một câu hỏi trả lời được bằng máy:

    1. ``original_preserved`` — prompt gốc còn nguyên vẹn trong prompt mới.
    2. ``identifiers_preserved`` — điện thoại/email/mã/URL của tester không bị đổi.
    3. ``no_invented_data`` — không có định danh *mới* không nằm trong prompt gốc
       hoặc trong kiến thức của KB.
    4. ``no_hard_ui`` — không toạ độ, không bounds, không pixel.
    5. ``knowledge_grounded`` — không nhắc workflow nào ngoài danh sách đã truy xuất.
    6. ``sections_ok`` — đủ các mục mà kế hoạch đòi (kế hoạch / kiến thức / bối cảnh /
       khoảng trống / tiêu chí).
    7. ``tasks_kept`` — mọi việc trong kế hoạch còn xuất hiện: không mất mục tiêu.
    8. ``primary_knowledge_kept`` — điều kiện, tiêu chí và cảnh báo của workflow
       THỰC HIỆN không bị bỏ quên.
    9. ``supporting_not_a_step`` — workflow phụ không được biến thành việc phải làm.
    10. ``size_ok`` — không phình to hơn hạn mức.
    """
    checks: list[Check] = []
    original = (original_prompt or "").strip()
    enhanced = (enhanced_prompt or "").strip()

    if not enhanced:
        return [Check("original_preserved", False, "Prompt mới rỗng")]

    preserved = normalize(original) in normalize(enhanced) if original else True
    checks.append(
        Check(
            "original_preserved",
            preserved,
            "" if preserved else "Prompt gốc không còn nguyên vẹn trong prompt mới",
        )
    )

    missing = [token for token in identity_tokens(original) if nfc(token) not in nfc(enhanced)]
    checks.append(
        Check(
            "identifiers_preserved",
            not missing,
            "" if not missing else "Định danh bị thay đổi/mất: " + ", ".join(missing[:5]),
        )
    )

    allowed = {nfc(token) for token in identity_tokens(original)}
    allowed |= {nfc(token) for token in identity_tokens(digest)}
    invented_data = [token for token in identity_tokens(enhanced) if nfc(token) not in allowed]
    checks.append(
        Check(
            "no_invented_data",
            not invented_data,
            "" if not invented_data else "Dữ liệu không có nguồn: " + ", ".join(invented_data[:5]),
        )
    )

    hard_ui = has_hard_ui(enhanced)
    checks.append(
        Check("no_hard_ui", not hard_ui, "" if not hard_ui else "Có toạ độ/pixel trong prompt mới")
    )

    known_ids = {
        hit.workflow.id
        for entry in entries
        for hit in [*entry.primary, *entry.supporting, *entry.irrelevant]
    }
    invented = _mentions_id(enhanced, known_ids)
    checks.append(
        Check(
            "knowledge_grounded",
            not invented,
            "" if not invented else "Nhắc workflow không có trong KB: " + ", ".join(invented[:5]),
        )
    )

    missing_sections = [
        header for header in _sections_required(entries, digest) if header not in enhanced
    ]
    checks.append(
        Check(
            "sections_ok",
            not missing_sections,
            "" if not missing_sections else "Thiếu mục: " + ", ".join(missing_sections),
        )
    )

    dropped_tasks = [
        one_line(entry.task.query() or entry.task.action)
        for entry in entries
        if not _covers(entry.task, enhanced, coverage_floor)
    ]
    checks.append(
        Check(
            "tasks_kept",
            not dropped_tasks,
            ""
            if not dropped_tasks
            else "Mất việc trong kế hoạch: " + " | ".join(dropped_tasks[:3]),
        )
    )

    primary_hits = [hit for entry in entries for hit in entry.primary]
    checks.append(
        _coverage_check(
            "primary_knowledge_kept",
            [
                text
                for hit in primary_hits
                for text in [
                    *hit.workflow.preconditions,
                    *hit.workflow.success_criteria,
                    *workflow_warnings(hit.workflow),
                ]
            ],
            enhanced,
            coverage_floor,
            "điều kiện/tiêu chí/cảnh báo của workflow thực hiện",
        )
    )

    contradicted: list[str] = []
    instruction_body = normalize(_instruction_sections(enhanced))
    allowed_labels = normalize(original)
    # Chỉ coi là rò rỉ khi nhãn được trích dẫn lại — đó là cách một nhãn UI thật sự được
    # chép vào prompt ("nhấn 'Huỷ'").  Một từ thường gặp như "xác nhận" trong câu văn
    # không phải bằng chứng rằng luật của modal đã trở thành bước.
    quoted_in_prompt = {normalize(label) for label in _QUOTED_LABEL.findall(enhanced)}
    for hit in _supporting_only(entries):
        # Nhãn UI mà *chỉ* workflow phụ nhắc tới (không có trong prompt gốc, không nằm
        # trong tên/mô tả của chính nó) không được xuất hiện trong phần chỉ dẫn: đó
        # chính là lỗi đã gặp — prompt yêu cầu chuyển nhà mà lại hướng dẫn nhấn 'Huỷ'.
        context_text = normalize(f"{hit.workflow.name} {hit.workflow.description}")
        for label in _quoted_labels(hit.workflow):
            key = normalize(label)
            if not key or _contains_word(allowed_labels, key) or _contains_word(context_text, key):
                continue
            if key in quoted_in_prompt and _contains_word(instruction_body, key):
                contradicted.append(f"{hit.workflow.id}: '{label}'")
    checks.append(
        Check(
            "supporting_not_a_step",
            not contradicted,
            ""
            if not contradicted
            else "Nhãn của workflow phụ lọt vào phần chỉ dẫn: " + " | ".join(contradicted[:3]),
        )
    )

    size_ok = len(enhanced) <= max_enhanced_chars
    checks.append(
        Check(
            "size_ok",
            size_ok,
            "" if size_ok else f"Prompt mới {len(enhanced)} ký tự, vượt {max_enhanced_chars}",
        )
    )
    return checks


@dataclass
class PrepareOptions:
    """Tham số vận hành của một lần chuẩn bị prompt.

    Đọc từ ``settings.ARTEMIS_PREPARE_*`` khi có, nên VPS chỉnh được bằng ``.env``
    mà không phải sửa code; test truyền thẳng giá trị nhỏ để chạy nhanh.
    """

    max_knowledge_chars: int = 1200
    max_enhanced_chars: int = 6000
    max_output_tokens: int = 700
    timeout: float = 20.0
    coverage_floor: float = 0.5
    cache_ttl: float = 600.0
    cache_size: int = 256
    rate_limit: int = 30
    rate_window: float = 60.0
    top_k: int | None = None
    #: Phân tích mục tiêu bằng LLM trước khi truy xuất.  Tắt = một task cho cả câu.
    intent_enabled: bool = True
    max_tasks: int = 4
    intent_max_tokens: int = 400
    intent_timeout: float = 20.0
    #: Độ phủ token tối thiểu để một workflow được coi là *bàn đúng việc* (PRIMARY):
    #: đo trên TÊN workflow, vì tên là phát biểu chính thức về mục đích của nó.
    name_coverage_floor: float = 0.6
    #: Độ phủ tối thiểu trên mô tả/alias để coi là *bối cảnh* (SUPPORTING).
    intent_coverage_floor: float = 0.6
    #: Độ phủ mà tiêu chí/bước của workflow phụ không được vượt qua (nếu vượt nghĩa là
    #: nó đã bị biến thành việc phải làm).
    conflict_floor: float = 0.8

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> PrepareOptions:
        if settings is None:
            try:
                from artemis.config.settings import settings as global_settings

                settings = global_settings
            except Exception:  # noqa: BLE001 - thiếu config thì dùng mặc định
                settings = None

        def value(name: str, fallback: Any) -> Any:
            if settings is None:
                return fallback
            return getattr(settings, name, fallback)

        return cls(
            max_knowledge_chars=int(value("ARTEMIS_PREPARE_MAX_KNOWLEDGE_CHARS", 1200)),
            max_enhanced_chars=int(value("ARTEMIS_PREPARE_MAX_PROMPT_CHARS", 6000)),
            max_output_tokens=int(value("ARTEMIS_PREPARE_MAX_OUTPUT_TOKENS", 700)),
            timeout=float(value("ARTEMIS_PREPARE_TIMEOUT", 20.0)),
            coverage_floor=float(value("ARTEMIS_PREPARE_COVERAGE_FLOOR", 0.5)),
            cache_ttl=float(value("ARTEMIS_PREPARE_CACHE_TTL", 600.0)),
            cache_size=int(value("ARTEMIS_PREPARE_CACHE_SIZE", 256)),
            rate_limit=int(value("ARTEMIS_PREPARE_RATE_LIMIT", 30)),
            rate_window=float(value("ARTEMIS_PREPARE_RATE_WINDOW", 60.0)),
            top_k=value("ARTEMIS_PREPARE_TOP_K", None),
            intent_enabled=bool(value("ARTEMIS_PREPARE_INTENT_ENABLED", True)),
            max_tasks=int(value("ARTEMIS_PREPARE_MAX_TASKS", 4)),
            intent_max_tokens=int(value("ARTEMIS_PREPARE_INTENT_MAX_TOKENS", 400)),
            intent_timeout=float(value("ARTEMIS_PREPARE_INTENT_TIMEOUT", 20.0)),
            name_coverage_floor=float(value("ARTEMIS_PREPARE_NAME_COVERAGE_FLOOR", 0.6)),
            intent_coverage_floor=float(value("ARTEMIS_PREPARE_INTENT_COVERAGE_FLOOR", 0.6)),
            conflict_floor=float(value("ARTEMIS_PREPARE_CONFLICT_FLOOR", 0.8)),
        )


class PrepareCache:
    """Cache theo **phiên bản knowledge base**, không chỉ theo câu hỏi.

    Khoá gồm fingerprint của đúng những workflow đã dùng, nên sửa một workflow là
    mọi bản cache liên quan tự vô hiệu — tester không bao giờ nhận một prompt được
    tổng hợp từ kiến thức đã cũ.
    """

    def __init__(self, ttl: float = 600.0, max_entries: int = 256) -> None:
        self.ttl = max(0.0, float(ttl))
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, PrepareOutcome]] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(*, app_id: str | None, profile: str, prompt: str) -> str:
        """Khoá cache: câu hỏi + app + profile.  Phiên bản KB kiểm tra riêng.

        Không đưa phiên bản KB vào khoá vì lúc tra cứu chưa biết workflow nào sẽ khớp
        (biết được thì đã phải truy xuất rồi).  Thay vào đó bản cache ghi lại các
        workflow đã dùng, và lúc tra cứu fingerprint của chúng được tính lại từ
        database — nhờ vậy một lần lặp lại **không** tốn embedding và **không** tốn LLM.
        """
        raw = f"{app_id or ''}|{profile}|{normalize(prompt)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> PrepareOutcome | None:
        now = time.time()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self.misses += 1
                return None
            stored_at, outcome = item
            if self.ttl and now - stored_at > self.ttl:
                self._items.pop(key, None)
                self.misses += 1
                return None
            self.hits += 1
            return outcome

    def put(self, key: str, outcome: PrepareOutcome) -> None:
        now = time.time()
        with self._lock:
            if self.ttl:
                for stale, (stored_at, _) in list(self._items.items()):
                    if now - stored_at > self.ttl:
                        self._items.pop(stale, None)
            if len(self._items) >= self.max_entries:
                oldest = min(self._items.items(), key=lambda item: item[1][0])[0]
                self._items.pop(oldest, None)
            self._items[key] = (now, outcome)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._items),
                "hits": self.hits,
                "misses": self.misses,
                "ttl_seconds": self.ttl,
                "max_entries": self.max_entries,
            }


class RateLimiter:
    """Cửa sổ trượt theo khoá (khoá truy cập hoặc IP) cho endpoint tốn tiền.

    Chuẩn bị prompt là một lượt gọi LLM: không có giới hạn thì một vòng lặp vô ý
    trong UI cũng đốt hết ngân sách.  Trả ``retry_after`` để API đặt đúng header
    ``Retry-After``.
    """

    def __init__(self, max_events: int = 30, window_seconds: float = 60.0) -> None:
        self.max_events = max(1, int(max_events))
        self.window_seconds = max(1.0, float(window_seconds))
        self._lock = threading.Lock()
        self._events: dict[str, list[float]] = {}

    def allow(self, key: str) -> tuple[bool, float]:
        """``(được phép, giây cần chờ)`` cho một request của ``key``."""
        now = time.time()
        with self._lock:
            events = [
                stamp for stamp in self._events.get(key, []) if now - stamp < self.window_seconds
            ]
            if len(events) >= self.max_events:
                retry_after = self.window_seconds - (now - events[0])
                self._events[key] = events
                return False, max(0.0, retry_after)
            events.append(now)
            self._events[key] = events
            # Dọn khoá đã nguội để bộ nhớ không phình theo số client.
            if len(self._events) > 512:
                for stale, stamps in list(self._events.items()):
                    if not stamps or now - stamps[-1] > self.window_seconds:
                        self._events.pop(stale, None)
            return True, 0.0

    def stats(self) -> dict:
        with self._lock:
            return {
                "keys": len(self._events),
                "max_events": self.max_events,
                "window_seconds": self.window_seconds,
            }


# ── Kết quả ──────────────────────────────────────────────────────────────
@dataclass
class PrepareOutcome:
    """Kết quả một lần chuẩn bị: cái API trả về, cái UI hiển thị, cái tester duyệt."""

    original_prompt: str
    enhanced_prompt: str
    status: str
    tasks: list[dict] = field(default_factory=list)
    plan: dict = field(default_factory=dict)
    matched_workflows: list[dict] = field(default_factory=list)
    excluded_workflows: list[dict] = field(default_factory=list)
    knowledge_gaps: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    knowledge: str = ""
    source: dict = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    fallback_reason: str = ""
    cached: bool = False
    prepared_at: float = 0.0
    elapsed_ms: float = 0.0

    @property
    def used_llm(self) -> bool:
        return self.status == STATUS_LLM

    @property
    def ready(self) -> bool:
        """True khi tester có thể bấm Chạy ngay với bản này."""
        return self.status in {STATUS_LLM, STATUS_FALLBACK}

    def as_dict(self) -> dict:
        return {
            "original_prompt": self.original_prompt,
            "enhanced_prompt": self.enhanced_prompt,
            "status": self.status,
            "ready": self.ready,
            "tasks": self.tasks,
            "plan": self.plan,
            "matched_workflows": self.matched_workflows,
            "excluded_workflows": self.excluded_workflows,
            "knowledge_gaps": self.knowledge_gaps,
            "warnings": self.warnings,
            "knowledge": self.knowledge,
            "source": self.source,
            "checks": [check.as_dict() for check in self.checks],
            "metrics": self.metrics,
            "fallback_reason": self.fallback_reason,
            "cached": self.cached,
            "prepared_at": self.prepared_at,
            "elapsed_ms": self.elapsed_ms,
        }


def _retrieval_source_many(
    queries: Sequence[str], hits_by_query: dict[str, list[WorkflowHit]], app_id: str | None
) -> dict:
    """Nguồn gốc của kiến thức khi truy xuất theo từng task.

    Giữ đúng hình dạng mà UI và log đã dùng (``workflows``/``trusted``/``found``) để
    không phải sửa chỗ đọc, và thêm ``per_query`` để thấy task nào tìm ra gì.
    """
    workflows: list[str] = []
    trusted: list[str] = []
    per_query: list[dict] = []
    for query in queries:
        hits = hits_by_query.get(query, [])
        per_query.append(
            {
                "query": query,
                "found": any(hit.trusted for hit in hits),
                "hits": [hit.workflow.id for hit in hits],
            }
        )
        for hit in hits:
            if hit.workflow.id not in workflows:
                workflows.append(hit.workflow.id)
            if hit.trusted and hit.workflow.id not in trusted:
                trusted.append(hit.workflow.id)
    return {
        "app_id": app_id,
        "retrieval": "knowledge-service",
        "found": bool(trusted),
        "degraded": False,
        "reason": "",
        "queries": list(queries),
        "per_query": per_query,
        "workflows": workflows,
        "trusted": trusted,
        "kb_version": "",
    }


def prepare_prompt(
    prompt: str,
    *,
    app_id: str | None = None,
    app_package: str | None = None,
    profile: str = "pro",
    version_id: str | None = None,
    run_id: str | None = None,
    service: KnowledgeService | None = None,
    synthesizer: Synthesizer | None = None,
    options: PrepareOptions | None = None,
    cache: PrepareCache | None = None,
    use_cache: bool = True,
    model: str = "",
) -> PrepareOutcome:
    """Chuẩn bị prompt thực thi: hiểu mục tiêu → truy xuất → phân loại → tổng hợp.

    Sáu bước, mỗi bước trả lời một câu hỏi khác nhau:

    1. **Hiểu mục tiêu** — LLM tách câu của tester thành các task có điều kiện
       (:func:`artemis.knowledge.intent.analyze_intent`).
    2. **Truy xuất theo từng task** — mỗi task một truy vấn, qua đúng
       ``KnowledgeService.retrieve`` của RAG (RRF + trust policy + log như cũ).
    3. **Phân loại vai trò** — mỗi hit là PRIMARY (bàn đúng việc), SUPPORTING (bối
       cảnh/luật) hay IRRELEVANT (chỉ trùng từ).  Vai trò quyết định *nội dung nào
       được phép vào prompt*: tiêu chí và bước của một workflow phụ không bao giờ
       vào, nên chúng không thể trở thành việc phải làm.
    4. **Tổng hợp** — một lượt LLM viết lại kế hoạch + kiến thức đã phân loại thành
       các mục cố định (kế hoạch, kiến thức, bối cảnh, khoảng trống, tiêu chí).
    5. **Kiểm tra chất lượng** — 10 tiêu chí bằng máy; trượt một tiêu chí ⇒ bước 6.
    6. **Dự phòng** — prompt gốc + kiến thức dựng bằng luật, cùng cấu trúc mục, kèm
       ``status="fallback"`` và lý do.

    Không gọi LLM khi KB không có gì để nói (không tốn tiền cho một lần tổng hợp rỗng).
    """
    from artemis.knowledge.integration import resolve_app_id

    started = time.perf_counter()
    options = options or PrepareOptions.from_settings()
    original = (prompt or "").strip()
    profile = (profile or "pro").strip().lower() or "pro"
    metrics: dict = {
        "llm_calls": 0,
        "cache": "miss" if use_cache else "off",
        "plan_source": "",
        "queries": [],
    }

    def finish(outcome: PrepareOutcome) -> PrepareOutcome:
        outcome.original_prompt = original
        outcome.prepared_at = time.time()
        outcome.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        # Metrics của lượt hiện tại thắng metrics lấy từ cache: ``llm_calls`` và
        # ``cache`` phải nói về lần gọi này, không phải lần đã sinh ra bản cache.
        outcome.metrics = {
            **(outcome.metrics or {}),
            **metrics,
            "total_ms": outcome.elapsed_ms,
        }
        return outcome

    def plain(
        status: str,
        source: dict,
        reason: str,
        plan: IntentPlan | None = None,
        matched: list[dict] | None = None,
        excluded: list[dict] | None = None,
        gaps: list[dict] | None = None,
        warnings: list[str] | None = None,
    ) -> PrepareOutcome:
        """Kết quả không có gì để tổng hợp: giữ nguyên prompt gốc, không gọi LLM."""
        return finish(
            PrepareOutcome(
                original_prompt=original,
                enhanced_prompt=original,
                status=status,
                tasks=[task.as_dict() for task in plan.tasks] if plan else [],
                plan=plan.as_dict() if plan else {},
                matched_workflows=matched or [],
                excluded_workflows=excluded or [],
                knowledge_gaps=gaps or [],
                warnings=warnings or [],
                source={**source, "synthesis": None},
                fallback_reason=reason,
            )
        )

    if not original:
        return plain(STATUS_NO_KNOWLEDGE, _retrieval_source(None, app_id), "Prompt gốc rỗng")

    # 0. Knowledge base — chỉ đọc, và mọi lỗi ở đây nghĩa là "không có kiến thức".
    try:
        if service is None:
            from artemis.knowledge.service import get_service

            service = get_service()
    except Exception as exc:  # noqa: BLE001
        logger.info("Không khởi tạo được knowledge base: %s", exc)
        return plain(STATUS_DEGRADED, _retrieval_source(None, app_id), f"KB lỗi: {exc}")
    if service is None or not service.available:
        reason = getattr(service, "degraded_reason", "") or "Knowledge base chưa sẵn sàng"
        return plain(STATUS_DEGRADED, _retrieval_source(None, app_id), reason)

    resolved_app = app_id or resolve_app_id(app_package, service)
    if not resolved_app:
        return plain(
            STATUS_NO_KNOWLEDGE,
            _retrieval_source(None, None),
            f"App {app_package!r} chưa có trong knowledge base" if app_package else "Thiếu app_id",
        )

    # 1. Cache: khoá theo (câu hỏi, app, profile), xác nhận bằng phiên bản KB hiện tại.
    #
    # Tra cứu TRƯỚC khi phân tích mục tiêu: một lần lặp lại đúng yêu cầu cũ không được
    # tốn embedding, không tốn LLM, và cũng không tốn lượt phân tích mục tiêu.  Tính
    # đúng đắn nằm ở chỗ fingerprint của các workflow đã dùng được tính lại từ database:
    # sửa/xoá một workflow trong số đó là cache tự vô hiệu.
    store = (cache if cache is not None else get_cache()) if use_cache else None
    cache_key = PrepareCache.key(app_id=resolved_app, profile=profile, prompt=original)
    if store is not None:
        hit = store.get(cache_key)
        if hit is not None:
            used_ids = [
                row["workflow_id"]
                for row in [*hit.matched_workflows, *hit.excluded_workflows]
                if row.get("workflow_id")
            ]
            stored_version = str(hit.source.get("kb_version") or "")
            if stored_version and _current_kb_version(service, used_ids) == stored_version:
                logger.info("Prepare: dùng lại cache %s cho app %s", cache_key[:8], resolved_app)
                metrics["cache"] = "hit"
                metrics["llm_calls"] = 0
                return finish(
                    PrepareOutcome(
                        original_prompt=hit.original_prompt,
                        enhanced_prompt=hit.enhanced_prompt,
                        status=hit.status,
                        tasks=hit.tasks,
                        plan=hit.plan,
                        matched_workflows=hit.matched_workflows,
                        excluded_workflows=hit.excluded_workflows,
                        knowledge_gaps=hit.knowledge_gaps,
                        warnings=hit.warnings,
                        knowledge=hit.knowledge,
                        source={**hit.source, "cached": True},
                        checks=hit.checks,
                        metrics=hit.metrics,
                        fallback_reason=hit.fallback_reason,
                        cached=True,
                    )
                )

    # 2. Hiểu mục tiêu (một lượt LLM; tắt được bằng cấu hình).
    mark = time.perf_counter()
    if options.intent_enabled:
        plan = analyze_intent(
            original,
            synthesizer=synthesizer,
            app_label=resolved_app,
            max_tasks=options.max_tasks,
            max_tokens=options.intent_max_tokens,
            timeout=options.intent_timeout,
        )
        if synthesizer is not None:
            metrics["llm_calls"] += 1
    else:
        plan = deterministic_plan(original, "Phân tích mục tiêu đang tắt")
    metrics["intent_ms"] = round((time.perf_counter() - mark) * 1000, 2)
    metrics["plan_source"] = plan.source
    metrics["tasks"] = len(plan.tasks)
    if plan.source != "llm" and plan.reason:
        metrics["plan_reason"] = plan.reason

    # 2. Truy xuất theo từng task — một truy vấn mỗi task, qua service của RAG.
    queries = plan_queries(plan)
    metrics["queries"] = queries
    mark = time.perf_counter()
    hits_by_query: dict[str, list[WorkflowHit]] = {}
    degraded_reason = ""
    for query in queries:
        try:
            result = service.retrieve(
                query, resolved_app, version_id=version_id, top_k=options.top_k, run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001 - retrieval là cố vấn
            logger.warning("Truy xuất knowledge base lỗi cho %r: %s", query, exc)
            return plain(
                STATUS_DEGRADED, _retrieval_source(None, resolved_app), f"Truy xuất lỗi: {exc}"
            )
        if result.degraded:
            degraded_reason = result.reason or "Knowledge base suy giảm"
        hits_by_query[query] = list(result.hits)
    metrics["retrieval_ms"] = round((time.perf_counter() - mark) * 1000, 2)

    source = _retrieval_source_many(queries, hits_by_query, resolved_app)
    if degraded_reason:
        return plain(STATUS_DEGRADED, source, degraded_reason)

    # 3. Phân loại vai trò theo từng task.
    #
    # Khi KHÔNG có kế hoạch thật (LLM phân tích hỏng, hoặc bị tắt), task duy nhất là cả
    # câu — so độ phủ của cả câu với tên workflow là vô nghĩa, và mọi hit sẽ bị loại.
    # Lúc đó giao lại cho chính sách tin cậy của RAG (``hit.trusted``) quyết định, đúng
    # như hành vi trước đây; tầng vai trò vẫn giữ luật/modal ở mức bối cảnh.
    decomposed = plan.source == "llm"
    retrieved = [(task, hits_by_query.get(task.query() or task.action, [])) for task in plan.tasks]
    entries = build_task_knowledge(
        plan,
        retrieved,
        min_intent_coverage=options.intent_coverage_floor if decomposed else 0.0,
        min_name_coverage=options.name_coverage_floor if decomposed else 0.0,
    )
    matched, excluded, hits_by_id = _matched_workflows(entries)
    gaps = _knowledge_gaps(entries)
    warnings = _collect_warnings(entries)
    metrics["primary"] = sum(1 for row in matched if row["role"] == ROLE_PRIMARY)
    metrics["supporting"] = sum(1 for row in matched if row["role"] == ROLE_SUPPORTING)
    metrics["excluded"] = len(excluded)
    metrics["gaps"] = len(gaps)

    if not matched:
        # KB không có gì dùng được cho bất kỳ task nào: giữ nguyên prompt gốc và nói rõ
        # khoảng trống — KHÔNG gọi LLM để tổng hợp một khoảng trống.
        return plain(
            STATUS_NO_KNOWLEDGE,
            source,
            "Knowledge base không có workflow nào phục vụ các việc này",
            plan=plan,
            matched=matched,
            excluded=excluded,
            gaps=gaps,
            warnings=warnings,
        )

    version = kb_version([hit.workflow for hit in hits_by_id.values()])
    source = {**source, "kb_version": version}

    brief = build_knowledge_brief(entries, plan.final_goal, max_chars=options.max_knowledge_chars)
    knowledge = brief

    def fallback(reason: str, checks: list[Check] | None = None) -> PrepareOutcome:
        """Prompt gốc + kiến thức dựng bằng luật, cùng cấu trúc mục.

        Không cache kết quả dự phòng: một lần LLM hỏng thoáng qua không được phép ghim
        tester vào bản suy giảm suốt TTL của cache.
        """
        enhanced = build_deterministic_prompt(
            original, entries, plan.final_goal, max_chars=options.max_knowledge_chars
        )
        if len(enhanced) > options.max_enhanced_chars:
            enhanced = enhanced[: options.max_enhanced_chars].rstrip()
        return finish(
            PrepareOutcome(
                original_prompt=original,
                enhanced_prompt=enhanced,
                status=STATUS_FALLBACK,
                tasks=[task.as_dict() for task in plan.tasks],
                plan=plan.as_dict(),
                matched_workflows=matched,
                excluded_workflows=excluded,
                knowledge_gaps=gaps,
                warnings=warnings,
                knowledge=knowledge,
                source={**source, "synthesis": SOURCE_DETERMINISTIC},
                checks=checks or [],
                fallback_reason=reason,
            )
        )

    # 5. Tổng hợp + kiểm tra chất lượng.  Không đạt ⇒ bước 6.
    if synthesizer is None:
        return fallback("Chưa cấu hình LLM để tổng hợp prompt")

    mark = time.perf_counter()
    try:
        knowledge, usage = enhance_with_llm(
            original,
            brief,
            profile,
            synthesizer,
            max_chars=options.max_knowledge_chars,
            max_tokens=options.max_output_tokens,
            timeout=options.timeout,
        )
        metrics["llm_calls"] += 1
        if usage:
            metrics["usage"] = usage
    except Exception as exc:  # noqa: BLE001 - LLM hỏng không được làm gãy tester
        logger.warning("LLM tổng hợp prompt thất bại: %s", exc)
        return fallback(f"LLM tổng hợp lỗi: {exc}")
    metrics["synthesis_ms"] = round((time.perf_counter() - mark) * 1000, 2)

    # 6. Chèn lại mục bị bỏ sót trước khi kiểm tra: mục là cấu trúc, không phải văn phong.
    knowledge = _ensure_sections(knowledge, brief, entries)
    enhanced = assemble_prompt(original, knowledge)
    checks = validate_enhanced(
        original,
        enhanced,
        entries,
        digest=brief,
        max_enhanced_chars=options.max_enhanced_chars,
        coverage_floor=options.coverage_floor,
        conflict_floor=options.conflict_floor,
    )
    failures = failed_checks(checks)
    if failures:
        reason = "Không đạt kiểm tra chất lượng: " + "; ".join(
            f"{check.name} ({check.detail})" for check in failures
        )
        logger.warning("Prepare: %s", reason)
        return fallback(reason, checks=checks)

    outcome = PrepareOutcome(
        original_prompt=original,
        enhanced_prompt=enhanced,
        status=STATUS_LLM,
        tasks=[task.as_dict() for task in plan.tasks],
        plan=plan.as_dict(),
        matched_workflows=matched,
        excluded_workflows=excluded,
        knowledge_gaps=gaps,
        warnings=warnings,
        knowledge=knowledge,
        source={**source, "synthesis": SOURCE_LLM, "model": model},
        checks=checks,
    )
    if store is not None:
        store.put(cache_key, outcome)
    return finish(outcome)


def _matched_workflows(
    entries: Sequence[TaskKnowledge],
) -> tuple[list[dict], list[dict], dict[str, WorkflowHit]]:
    """Workflow đã dùng (kèm vai trò) và workflow bị loại, theo thứ tự task.

    ``matched_workflows`` chỉ chứa PRIMARY/SUPPORTING — tức những gì được phép ảnh
    hưởng tới prompt.  ``excluded_workflows`` là phần bị loại: tester cần thấy chúng
    để biết hệ thống *đã cân nhắc rồi bỏ*, chứ không phải bỏ sót.
    """
    best = best_role(entries)
    hits_by_id: dict[str, WorkflowHit] = {}
    for entry in entries:
        for hit in [*entry.primary, *entry.supporting, *entry.irrelevant]:
            hits_by_id.setdefault(hit.workflow.id, hit)

    order: list[str] = []
    for entry in entries:
        for hit in [*entry.primary, *entry.supporting, *entry.irrelevant]:
            if hit.workflow.id not in order:
                order.append(hit.workflow.id)

    matched: list[dict] = []
    excluded: list[dict] = []
    for workflow_id in order:
        hit = hits_by_id[workflow_id]
        assignment = best.get(workflow_id)
        role = assignment.role if assignment else ROLE_SUPPORTING
        row = {
            "workflow_id": hit.workflow.id,
            "name": hit.workflow.name,
            "status": hit.workflow.status,
            "score": hit.score,
            "channels": hit.channels,
            "trusted": hit.trusted,
            "revision_no": hit.workflow.revision_no,
            "updated_at": hit.workflow.updated_at,
            "role": role,
            "role_reason": assignment.reason if assignment else "",
            "evidence": assignment.evidence if assignment else {},
        }
        if role == ROLE_PRIMARY:
            row["tasks"] = [
                entry.task.query()
                for entry in entries
                if hit.workflow.id in {item.workflow.id for item in entry.primary}
            ]
            matched.insert(0, row)
        elif role == ROLE_SUPPORTING:
            row["tasks"] = [
                entry.task.query()
                for entry in entries
                if hit.workflow.id in {item.workflow.id for item in entry.supporting}
            ]
            matched.append(row)
        else:
            excluded.append(row)
    return matched, excluded, hits_by_id


def _knowledge_gaps(entries: Sequence[TaskKnowledge]) -> list[dict]:
    return [
        {
            "task": entry.task.as_dict(),
            "reason": entry.gap_reason,
            "has_supporting": bool(entry.supporting),
        }
        for entry in entries
        if entry.gap
    ]


def _collect_warnings(entries: Sequence[TaskKnowledge]) -> list[str]:
    """Cảnh báo của workflow PRIMARY + cảnh báo về khoảng trống kiến thức."""
    out: list[str] = []
    for entry in entries:
        for hit in entry.primary:
            for text in workflow_warnings(hit.workflow):
                if text not in out:
                    out.append(text)
    for entry in entries:
        if entry.gap:
            text = f"KB chưa có hướng dẫn cho: {one_line(entry.task.query() or entry.task.action)}"
            if text not in out:
                out.append(text)
    return out


_CACHE: PrepareCache | None = None
_LIMITER: RateLimiter | None = None
_STATE_LOCK = threading.Lock()


def get_cache() -> PrepareCache:
    """Cache dùng chung của tiến trình, dựng theo ``ARTEMIS_PREPARE_CACHE_*``."""
    global _CACHE
    with _STATE_LOCK:
        if _CACHE is None:
            options = PrepareOptions.from_settings()
            _CACHE = PrepareCache(ttl=options.cache_ttl, max_entries=options.cache_size)
        return _CACHE


def get_rate_limiter() -> RateLimiter:
    """Rate limiter dùng chung của tiến trình, theo ``ARTEMIS_PREPARE_RATE_*``."""
    global _LIMITER
    with _STATE_LOCK:
        if _LIMITER is None:
            options = PrepareOptions.from_settings()
            _LIMITER = RateLimiter(
                max_events=options.rate_limit, window_seconds=options.rate_window
            )
        return _LIMITER


def reset_runtime_state() -> None:
    """Xoá cache và limiter (test, hoặc sau khi đổi cấu hình)."""
    global _CACHE, _LIMITER
    with _STATE_LOCK:
        _CACHE = None
        _LIMITER = None


def _retrieval_source(result: RetrievalResult | None, app_id: str | None) -> dict:
    """Nguồn gốc của kiến thức — để UI nói được "cái này từ đâu ra"."""
    if result is None:
        return {
            "app_id": app_id,
            "retrieval": "knowledge-service",
            "found": False,
            "degraded": False,
            "reason": "",
            "timings_ms": {},
            "workflows": [],
            "trusted": [],
            "kb_version": "",
        }
    return {
        "app_id": app_id or result.app_id,
        "version_id": result.version_id,
        "retrieval": "knowledge-service",
        "found": result.found,
        "degraded": result.degraded,
        "reason": result.reason,
        "timings_ms": result.timings_ms,
        "workflows": [hit.workflow.id for hit in result.hits],
        "trusted": [hit.workflow.id for hit in result.hits if hit.trusted],
        "kb_version": "",
    }
