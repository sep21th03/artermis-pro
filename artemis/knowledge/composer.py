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

"""Prompt Composer: ghép KB đã phân loại thành prompt thực thi, **bằng luật**.

**Vì sao tất định, không gọi LLM viết lại.**  Bản trước đưa "brief" cho một lượt LLM viết
lại thành prompt.  Đo trên máy thật, lượt đó *nuốt mất* đúng phần cốt lõi: bước "mũi tên
caret-down ở CUỐI ô nhập số điện thoại" biến thành "chọn tài khoản trong danh sách đã lưu",
nên agent gõ số điện thoại vào ô nhập.  Viết lại văn bản là chỗ mất thông tin, và nó tốn
một lượt gọi LLM cho mỗi lần chuẩn bị.  Ở đây mọi thứ được *sắp xếp, lọc trùng, định dạng*
bằng code — ba việc mà code làm chính xác hơn LLM.

**Thứ tự mục** (bỏ mục nào không có nội dung):

1. ``NHIỆM VỤ`` — mục tiêu của tester, giữ nguyên ý nghĩa, kèm điều kiện áp dụng.
2. ``KIỂM TRA TRẠNG THÁI`` — điều kiện trước, viết như *điều phải xác minh*, không phải sự
   thật mặc định.
3. ``CÁC BƯỚC THỰC HIỆN`` — điều hướng tới màn hình trước, rồi các bước của workflow chính,
   theo đúng thứ tự trong KB.
4. ``LƯU Ý QUAN TRỌNG`` — cảnh báo và lỗi đã gặp, mỗi ý một lần.
5. ``XÁC MINH KẾT QUẢ`` — tách *kết quả trung gian* khỏi *mục tiêu cuối*, và nói rõ chưa đủ
   bằng chứng thì phải báo chưa xác minh.

**Không bịa thao tác.**  Composer chỉ phát ra chữ có trong KB.  Chỗ KB thiếu (không có bước,
không có cách mở màn hình) thì phát ra đúng một câu dạng "quan sát UI để xác định …" — không
suy ra vị trí nút, không suy ra đường điều hướng.  Câu này cũng là quy tắc chung đặt ở đầu
prompt, nên Planner nhận được cùng ràng buộc như Operator.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
import re

from artemis.knowledge.intent import (
    TaskKnowledge,
    is_navigation_workflow,
    is_rule,
    one_line,
    sanitize_ui_instructions,
    workflow_warnings,
)
from artemis.knowledge.models import Workflow, WorkflowHit
from artemis.knowledge.text import content_tokens, normalize

SECTION_TASK = "NHIỆM VỤ:"
SECTION_STATE = "KIỂM TRA TRẠNG THÁI (xác minh trên UI, KHÔNG mặc định là đúng):"
SECTION_STEPS = "CÁC BƯỚC THỰC HIỆN:"
SECTION_NOTES = "LƯU Ý QUAN TRỌNG:"
SECTION_VERIFY = "XÁC MINH KẾT QUẢ:"
SECTION_STOP = "KHI KHÔNG THỰC HIỆN ĐƯỢC (điều kiện dừng):"

SECTIONS = (
    SECTION_TASK,
    SECTION_STATE,
    SECTION_STEPS,
    SECTION_STOP,
    SECTION_NOTES,
    SECTION_VERIFY,
)

#: Từ *ngôn ngữ* chỉ một tập hợp nhiều mục.  Dùng để nhận ra bước **tìm/chọn trong danh
#: sách** — loại bước cần điều kiện dừng riêng (không thấy mục tiêu thì phải dừng, không chọn
#: mục khác).  Đây là lớp ngôn ngữ, không phải từ khoá của một ứng dụng.
_LIST_MARKERS = ("danh sách", "list")

#: Mã kết luận, dùng trong prompt để agent báo đúng loại kết quả.
CODE_NOT_FOUND = "NOT_FOUND"
CODE_NOT_VERIFIED = "NOT_VERIFIED"

#: Ghi chú gắn vào một bước tìm/chọn trong danh sách.
SEARCH_GUARD = (
    f"⟵ chỉ tiếp tục khi tìm thấy ĐÚNG mục tiêu; không thấy thì DỪNG và báo {CODE_NOT_FOUND}"
)

#: Quy tắc chung: chỉ làm điều có căn cứ.  Đặt ở đầu prompt vì Planner đọc cùng prompt này —
#: "tap profile icon ở góc trên bên phải" từng được Planner tự sinh từ một khoảng trống KB.
GROUND_RULE = (
    "Chỉ thực hiện thao tác có trong hướng dẫn dưới đây hoặc quan sát được trên UI hiện tại. "
    "Không tự suy ra vị trí nút, biểu tượng hay đường điều hướng khi hướng dẫn chưa nói."
)

#: Từ nối của một *điều kiện* trong tiếng Việt.  Đây là ngữ pháp, không phải từ khoá của một
#: ứng dụng: dùng để nhận ra câu nào đã nói rõ "chỉ khi nào", thay vì đoán theo tên workflow.
_CONDITION_MARKERS = ("chỉ khi", "chỉ cuộn", "nếu", "khi chưa", "trừ khi", "khi đã")

#: Dấu hiệu *mở đầu* một câu cảnh báo ("CẨN THẬN: …", "Đừng …", "Không …").
_WARNING_LEADS = ("cẩn thận", "đừng", "chớ", "tránh", "không", "tuyệt đối")

#: Dấu hiệu mạnh — xuất hiện ở đâu trong câu cũng đủ.
_WARNING_STRONG = ("cẩn thận", "đừng", "tuyệt đối", "tránh", "lỗi đã gặp")

#: Từ phủ định — dùng để nhận ra hai câu *xung đột* thật (một khẳng định, một phủ định).
_NEGATIONS = ("không", "đừng", "chớ", "tránh")

#: Nói rõ khi hai nguồn KB chưa thống nhất, thay vì tự chọn một cái làm sự thật.
CONFLICT_NOTE = (
    "Hai nguồn KB chưa thống nhất về điểm này — quan sát UI hiện tại để quyết định, "
    "và ghi lại điều đã quan sát được."
)


def _has_marker(text: str, markers: Sequence[str]) -> bool:
    """Có marker nào trong text không, so trên bản **đã chuẩn hoá hai phía**.

    ``normalize`` bỏ dấu, nên ``"không" in normalize(text)`` là **không bao giờ khớp** — lỗi
    này đã làm chết một guard trước đây (và làm luôn cả phép nhận diện câu có điều kiện).
    """
    lowered = normalize(text)
    return any(normalize(marker) in lowered for marker in markers)


def _negated(text: str) -> bool:
    return _has_marker(text, _NEGATIONS)


def _has_marker_exact(text: str, markers: Sequence[str]) -> bool:
    """Như :func:`_has_marker` nhưng **giữ dấu** — dùng khi bỏ dấu gây trùng sai.

    Ví dụ thật: ``"Đúng dòng số điện thoại…"`` bị coi là cảnh báo vì ``normalize`` biến cả
    "Đúng" và "đừng" thành ``dung``.  Văn bản KB là tiếng Việt có dấu, nên ở đây so nguyên
    dạng (casefold) là đúng và không mất khả năng khớp.
    """
    lowered = text.casefold()
    return any(marker.casefold() in lowered for marker in markers)


def _is_warning(text: str) -> bool:
    """Câu này có phải *cảnh báo* không.

    Hai điều kiện, và điều kiện thứ hai là chỗ bản trước sai: câu phải **mở đầu** bằng dấu
    hiệu cảnh báo, hoặc chứa dấu hiệu **mạnh**.  Nếu chỉ cần "chứa chữ không", mục LƯU Ý sẽ
    hứng cả mảnh câu hướng dẫn ("… CHỈ khi màn hình hiện tại không có thanh điều hướng") và
    lặp lại nguyên phần bước.
    """
    stripped = text.strip()
    if _has_marker_exact(stripped[:14], _WARNING_LEADS):
        return True
    return _has_marker_exact(stripped, _WARNING_STRONG)


def _sentences(text: object) -> list[str]:
    """Câu trong một đoạn KB, đã bỏ toạ độ và khoảng trắng thừa."""
    cleaned = sanitize_ui_instructions(one_line(text))
    if not cleaned:
        return []
    return [part.strip(" -•") for part in re.split(r"(?<=[.;!?])\s+", cleaned) if part.strip(" -•")]


#: Nhóm từ đồng nghĩa **ngôn ngữ** cho thao tác UI.  KB viết lẫn "scroll" và "cuộn" cho cùng
#: một việc, nên so từ nguyên văn thì hai câu nói cùng điều không bao giờ khớp — đúng ca mâu
#: thuẫn cần xử lý ("hãy scroll xuống dưới cùng" vs "Chỉ cuộn khi nó thật sự chưa hiện").
#: Đây là lớp ngôn ngữ, không phải logic của một ứng dụng: thêm một ứng dụng mới không phải
#: sửa gì ở đây.
_SYNONYMS = (
    ("cuon", "scroll", "luot"),
    ("nhan", "tap", "cham", "bam", "click"),
    ("giu", "hold", "long_press", "press_hold"),
    ("nhap", "go", "dien", "input"),
    ("mo", "vao", "open"),
)

_SYNONYM_INDEX = {
    token: group[0] for group in _SYNONYMS for token in group
}

#: Token của chính các từ nối điều kiện — không tính vào "chủ đề" khi so hai câu, nếu không
#: mọi câu đều dính nhau qua chữ "khi"/"chỉ".
_CONDITION_TOKENS = {token for marker in _CONDITION_MARKERS for token in content_tokens(marker)}

#: Token *thao tác* — giá trị chuẩn của mỗi nhóm đồng nghĩa ở trên.  Hai câu chỉ được coi là
#: nói về **cùng một thao tác** khi chúng dùng chung một token thao tác ("cuộn", "nhấn",
#: "nhập", "mở").  Nhờ vậy việc gộp câu chỉ xảy ra khi thật sự cùng việc, thay vì chỉ trùng
#: một danh từ chung nào đó — và một thao tác cần thiết không bị xoá vì trùng chữ.
_ACTION_TOKENS = {group[0] for group in _SYNONYMS}

#: Một *luật* thì ngắn ("Chỉ cuộn khi nó thật sự chưa hiện.").  Câu dài có chữ điều kiện chỉ là
#: văn xuôi — coi nó là luật sẽ khiến nó "thắng" và xoá mất các câu khác.
_RULE_MAX_TOKENS = 8


def _concept_tokens(text: object) -> set[str]:
    """Token nội dung đã quy về dạng chuẩn, bỏ dấu câu dính cuối từ."""
    tokens = {re.sub(r"\W+$", "", token) for token in content_tokens(text)}
    return {_SYNONYM_INDEX.get(token, token) for token in tokens if token}


def _similar(left: str, right: str, threshold: float = 0.8) -> bool:
    """Hai câu nói cùng một điều (độ phủ token hai chiều, không phụ thuộc thứ tự)."""
    left_tokens, right_tokens = _concept_tokens(left), _concept_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    return overlap / min(len(left_tokens), len(right_tokens)) >= threshold


def _is_conditional(text: str) -> bool:
    return _has_marker(text, _CONDITION_MARKERS)


def _action_concepts(text: str) -> set[str]:
    return _concept_tokens(text) & _ACTION_TOKENS


def _same_action(left: str, right: str) -> bool:
    """Hai câu nói về **cùng một thao tác**.

    Ba điều kiện: **tập** khái niệm thao tác phải *bằng nhau* (không chỉ giao nhau — "nhấn"
    là chạm còn "nhấn giữ" là giữ, hai thao tác khác nhau), và có ít nhất hai token nội dung
    chung (tiếng Việt viết rời âm tiết nên một token chung là bằng chứng quá yếu).
    """
    left_actions, right_actions = _action_concepts(left), _action_concepts(right)
    if not left_actions or left_actions != right_actions:
        return False
    return len(_concept_tokens(left) & _concept_tokens(right)) >= 2


def dedupe_sentences(
    lines: Iterable[str],
    *,
    mark_conflicts: bool = True,
    merge_rules: bool = True,
) -> list[str]:
    """Bỏ câu lặp, và xử lý cặp câu *mâu thuẫn* — chỉ khi có bằng chứng.

    KB hay mô tả cùng một việc ở nhiều chỗ (mô tả và bước), chép cả hai thì prompt đọc như
    nói lắp.  Ba lượt xử lý, mỗi lượt có điều kiện rõ ràng:

    1. **Câu gần trùng** (≥0,8 token chung) ⇒ giữ câu đầu.  Đây là "cùng một ý".
    2. **Câu có điều kiện là bản đầy đủ hơn** của một câu không điều kiện **về cùng một thao
       tác** ⇒ giữ câu có điều kiện.  Điều kiện "cùng một thao tác" là chốt chặn để không
       xoá mất thao tác cần thiết: chỉ khi hai câu nói về cùng việc thì câu có điều kiện mới
       bao trùm được câu kia.
    3. **Xung đột thật** (cùng thao tác, một khẳng định một phủ định, không câu nào nêu điều
       kiện) ⇒ giữ **cả hai** và thêm một dòng nêu rõ hai nguồn chưa thống nhất — kèm chính
       hai câu làm bằng chứng.  Không tự chọn một cái làm sự thật, và không báo xung đột khi
       không chỉ ra được hai câu nào.

    ``merge_rules=False`` để tắt lượt 2: **danh sách bước không được gộp theo luật**.  Hai
    bước liên tiếp cùng là "tap" là hai thao tác khác nhau; gộp chúng theo chữ "tap" là xoá
    mất một thao tác cần thiết.  Luật 2 chỉ dành cho văn xuôi (mô tả, cảnh báo), nơi hai câu
    thật sự nói về cùng một việc.
    """
    kept: list[str] = []
    for line in lines:
        text = one_line(line)
        if not text:
            continue
        match = next((index for index, item in enumerate(kept) if _similar(text, item)), None)
        if match is None:
            kept.append(text)
            continue
        existing = kept[match]
        if _is_conditional(text) and not _is_conditional(existing):
            kept[match] = text
        elif not _is_conditional(text) and _is_conditional(existing):
            continue
        elif _negated(text) != _negated(existing):
            kept.append(text)
        # Còn lại: cùng ý, cùng tính chất ⇒ bỏ câu sau.

    rules = (
        [
            text
            for text in kept
            if _is_conditional(text) and len(_concept_tokens(text)) <= _RULE_MAX_TOKENS
        ]
        if merge_rules
        else []
    )
    if rules:
        for text in [item for item in kept if item not in rules]:
            if any(_same_action(text, rule) for rule in rules):
                kept.remove(text)

    if mark_conflicts:
        evidence = [
            f"«{left}» ≠ «{right}»"
            for index, left in enumerate(kept)
            for right in kept[index + 1 :]
            # Bằng chứng cụ thể: **cùng câu chữ** mà ngược khẳng định — hai bản ghi nói trái
            # nhau.  Không dùng "cùng thao tác" ở đây: tiếng Việt viết rời âm tiết nên một
            # token thao tác chung là bằng chứng quá yếu, và nó từng báo xung đột giả giữa
            # hai tiêu chí xác minh khác nhau chỉ vì cùng chứa "nhập".
            if _similar(left, right) and _negated(left) != _negated(right)
        ]
        if evidence:
            kept.append(f"{CONFLICT_NOTE} Bằng chứng: " + " | ".join(evidence[:2]))
    return kept



# ── Dựng từng mục ────────────────────────────────────────────────────────
def _task_head(entry: TaskKnowledge) -> str:
    """Tên việc để in trong prompt: hành động + đối tượng, không lặp chữ.

    Bước phân tích mục tiêu đôi khi trả ``target`` đã nằm trong ``action`` ("đăng xuất tài
    khoản hiện tại" + "tài khoản hiện tại"); nối thẳng sẽ thành "… tài khoản hiện tại tài
    khoản hiện tại" — đọc như lỗi đánh máy và làm prompt khó theo dõi.
    """
    task = entry.task
    action = one_line(task.action)
    target = one_line(task.target)
    head = action
    if target and normalize(target) not in normalize(action):
        head = f"{action} {target}".strip()
    if task.condition:
        head += f" — {task.condition}"
    return head.strip()


def _primary(entry: TaskKnowledge) -> WorkflowHit | None:
    return entry.primary[0] if entry.primary else None


def _navigation(entry: TaskKnowledge) -> list[Workflow]:
    return [hit.workflow for hit in entry.supporting if is_navigation_workflow(hit.workflow)]


#: Bốn phần của một workflow đã chuẩn hoá.  Prompt được dựng từ *cấu trúc* này chứ không từ
#: văn bản thô: nhờ vậy mỗi câu trong prompt đều biết nó thuộc phần nào, và thứ tự là thứ tự
#: của cấu trúc (điều kiện → thao tác → kết quả mong đợi → xác minh), không phải thứ tự truy
#: hồi trả về.
@dataclass(frozen=True)
class WorkflowShape:
    """Một workflow nhìn theo bốn phần mà prompt cần."""

    preconditions: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    expected_results: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()


def _step_action_text(step: Any) -> str:
    """Dòng thao tác của một bước, gồm cả tham số KB khai (``value``, ``until``).

    Hai trường này bị bỏ trước đây, và đó là mất thông tin thật: 12 bước trong KB khai
    ``value`` (giá trị cần nhập) và 13 bước khai ``until`` ("cuộn cho tới khi X xuất hiện").
    Bỏ ``value`` là agent phải tự đoán giá trị; bỏ ``until`` là mất điều kiện dừng của vòng
    lặp — đúng loại điều kiện mà prompt cần nêu.
    """
    target = sanitize_ui_instructions(one_line(step.target))
    text = one_line(step.action)
    if target:
        text += f": {target}"
    value = sanitize_ui_instructions(one_line(step.value))
    if value:
        text += f" (giá trị: {value})"
    until = sanitize_ui_instructions(one_line(step.until))
    if until:
        text += f" (lặp cho tới khi: {until})"
    if step.optional:
        text += " (chỉ khi cần)"
    return text


def _step_field(step: Any, name: str) -> str:
    """Đọc một trường của bước, chấp nhận cả ``WorkflowStep`` và ``dict``.

    KB trả về object, nhưng payload thô từ file JSON là dict — nhận cả hai thì hàm dùng được
    ở mọi đường vào mà không phải chuyển đổi trước.
    """
    if isinstance(step, dict):
        return str(step.get(name) or "")
    return str(getattr(step, name, "") or "")


def is_search_step(step: Any) -> bool:
    """Bước này có phải *tìm/chọn một mục trong danh sách* không.

    Nhận ra bằng ngôn ngữ KB: target hoặc kết quả mong đợi nhắc tới một tập hợp nhiều mục.
    Bước như vậy cần điều kiện dừng riêng — nếu tìm hết danh sách mà không thấy mục tiêu thì
    phải dừng, chứ không được chọn mục khác thay thế.
    """
    text = normalize(f"{_step_field(step, 'target')} {_step_field(step, 'expected_state')}")
    # So trên bản đã chuẩn hoá ở *cả hai* phía: ``normalize`` bỏ dấu, nên so marker còn dấu
    # với văn bản mất dấu là không bao giờ khớp (lỗi này đã gặp nhiều lần trong repo).
    return any(normalize(marker) in text for marker in _LIST_MARKERS)


def normalize_workflow(workflow: Workflow) -> WorkflowShape:
    """Chuẩn hoá workflow thành ``preconditions → actions → expected_results → verification``.

    Thứ tự các thao tác **giữ nguyên như trong KB**: bước sau của một workflow đã được viết
    theo quan hệ phụ thuộc (bước 2 cần kết quả của bước 1), nên đảo thứ tự là phá phụ thuộc.
    ``expected_state`` của từng bước là *điều kiện hoàn thành của bước đó* — khác với
    ``success_criteria`` (điều kiện hoàn thành cả workflow), và hai thứ này không được trộn.

    ``expected_results`` **thẳng hàng theo chỉ số với** ``actions`` (bước không khai
    ``expected_state`` để chuỗi rỗng): lệch chỉ số một bước là gán kết quả mong đợi của bước
    này cho bước khác — prompt sẽ nói sai điều kiện hoàn thành của bước.
    """
    actions: list[str] = []
    expected: list[str] = []
    for step in workflow.steps:
        actions.append(_step_action_text(step))
        expected.append(sanitize_ui_instructions(one_line(step.expected_state)))
    return WorkflowShape(
        preconditions=tuple(
            sanitize_ui_instructions(one_line(text)) for text in workflow.preconditions if text
        ),
        actions=tuple(actions),
        expected_results=tuple(expected),
        verification=tuple(
            sanitize_ui_instructions(one_line(text))
            for text in workflow.success_criteria
            if text
        ),
    )


def render_tasks(entries: Sequence[TaskKnowledge]) -> list[str]:
    """Mục 1: việc cần làm, giữ nguyên định danh và điều kiện của tester."""
    return [f"{index}. {_task_head(entry)}" for index, entry in enumerate(entries, 1)]


def render_state(entries: Sequence[TaskKnowledge]) -> list[str]:
    """Mục 2: điều kiện trước — *điều phải xác minh*, không phải sự thật mặc định.

    Gộp theo việc: một việc có ba điều kiện thì in một dòng, không lặp tên việc ba lần.
    """
    lines: list[str] = []
    for entry in entries:
        hit = _primary(entry)
        if hit is None:
            continue
        conditions = normalize_workflow(hit.workflow).preconditions
        if conditions:
            lines.append(f"- «{_task_head(entry)}»: " + "; ".join(conditions))
    return dedupe_sentences(lines)


def render_steps(entries: Sequence[TaskKnowledge]) -> list[str]:
    """Mục 3: điều hướng tới màn hình trước, rồi các bước của workflow chính, theo thứ tự.

    Mỗi việc là một tiêu đề nhỏ, các bước đánh số *trong* việc đó: đọc xuôi theo trình tự,
    và không lặp tên việc ở mọi bước.  Thứ tự đến từ **cấu trúc** (điều hướng → thao tác
    theo đúng thứ tự KB), không từ thứ tự truy hồi; kết quả mong đợi của từng bước đi kèm
    ngay trên bước đó để agent biết khi nào bước đã xong.  Chỗ KB thiếu bước thì nói thẳng
    là phải quan sát UI.
    """
    blocks: list[list[str]] = []
    for entry in entries:
        head = _task_head(entry)
        hit = _primary(entry)
        steps: list[str] = []
        # Điều hướng đứng trước vì đó là phụ thuộc trạng thái: workflow chính chỉ chạy được
        # khi đang ở màn hình của nó.
        for workflow in _navigation(entry):
            shape = normalize_workflow(workflow)
            for index, action in enumerate(shape.actions):
                text = f"tới màn hình cần dùng ({workflow.id}): {action}"
                state = shape.expected_results[index]
                if state:
                    text += f" → {state}"
                if is_search_step(workflow.steps[index]):
                    text += f" {SEARCH_GUARD}"
                steps.append(text)
        if hit is None:
            steps.append(
                "KB chưa có hướng dẫn cho việc này. Quan sát UI để xác định cách làm; "
                "KHÔNG tự suy diễn quy trình."
            )
        else:
            shape = normalize_workflow(hit.workflow)
            if not shape.actions:
                steps.append("KB chưa ghi các bước cụ thể. Quan sát UI để xác định thao tác.")
            for index, action in enumerate(shape.actions):
                text = action
                state = shape.expected_results[index]
                if state:
                    text += f" → {state}"
                if is_search_step(hit.workflow.steps[index]):
                    text += f" {SEARCH_GUARD}"
                steps.append(text)
        # Lọc trùng *bên trong* một việc, rồi mới đánh số: đánh số trước sẽ để lại lỗ
        # (bước bị bỏ vì trùng) làm prompt trông như thiếu bước.  Ở đây **không** gộp theo
        # luật điều kiện: hai bước liên tiếp cùng là "tap" vẫn là hai thao tác khác nhau.
        steps = dedupe_sentences(steps, mark_conflicts=False, merge_rules=False)
        # Truy vết: tiêu đề nêu rõ workflow nào đang được dùng, để đọc prompt là biết nguồn.
        sources = [workflow.id for workflow in _navigation(entry)]
        if hit is not None:
            sources.append(hit.workflow.id)
        title = f"«{head}»" + (f" — theo KB: {', '.join(sources)}" if sources else "")
        blocks.append([title] + [f"  {i}. {text}" for i, text in enumerate(steps, 1)])
    lines: list[str] = []
    for block in blocks:
        lines.extend(block)
    return lines


def render_notes(entries: Sequence[TaskKnowledge]) -> list[str]:
    """Mục 4: cảnh báo — metadata, câu cảnh báo trong bước, và *luật của app*.

    Luật (``home.switch_house_modal``: "modal là gợi ý đổi nhà, KHÔNG phải lỗi") là thứ ngăn
    agent báo FAILED oan khi gặp một modal bình thường.  Nó thuộc *lưu ý*, không thuộc các
    bước — giữ đúng chỗ thì không biến nó thành việc phải làm.

    Chỉ nhận câu *thật sự* là cảnh báo: câu mở đầu bằng dấu hiệu cảnh báo, hoặc có dấu hiệu
    mạnh.  Bản trước nhận cả câu chỉ chứa chữ "không" nên mục này lẫn cả mảnh câu hướng dẫn
    ("… không có thanh điều hướng dưới cùng") — trùng với phần bước và làm loãng cảnh báo.
    """
    lines: list[str] = []
    for entry in entries:
        for hit in [*entry.primary, *entry.supporting]:
            workflow = hit.workflow
            for text in workflow_warnings(workflow):
                lines.append(f"- {sanitize_ui_instructions(one_line(text))}")
            for step in workflow.steps:
                for sentence in _sentences(step.target):
                    if _is_warning(sentence):
                        lines.append(f"- {sentence}")
            if is_rule(workflow):
                for sentence in _sentences(workflow.description):
                    lines.append(f"- {sentence}")
    return dedupe_sentences(lines)


def render_verify(
    entries: Sequence[TaskKnowledge],
    final_goal: dict,
    original_prompt: str = "",
) -> list[str]:
    """Mục 5: kết quả trung gian tách khỏi mục tiêu cuối.

    **Mục tiêu cuối luôn phải có mặt.**  Khi bước phân tích mục tiêu hỏng (hoặc không rút ra
    được ``final_goal``), bản trước **không in dòng nào** về mục tiêu cuối — prompt chỉ còn
    "chưa đủ bằng chứng thì báo CHƯA XÁC MINH", và agent không có cách nào biết mình đã xong.
    Đo trên máy thật: agent vào màn hẹn giờ, thấy "Có 0 hẹn giờ" (đúng điều kiện tester hỏi),
    nhưng vì prompt không nêu điều kiện đó nên nó thoát ra rồi vào lại nhiều lần.  Nên khi
    thiếu ``final_goal`` thì lấy **nguyên văn câu của tester** làm mục tiêu cuối.

    Điều kiện có dạng điều kiện ("… lớn hơn 0 thì false, còn ngược lại") được giữ **nguyên
    văn**, không quy về ``type = value`` — quy về như vậy tạo ra một câu vô nghĩa và làm mất
    chính điều kiện cần đánh giá.
    """
    lines: list[str] = []
    for entry in entries:
        hit = _primary(entry)
        if hit is None:
            continue
        for text in normalize_workflow(hit.workflow).verification:
            lines.append(f"- Xong «{_task_head(entry)}»: {text}")
    goal_type = one_line(final_goal.get("type"))
    goal_value = one_line(final_goal.get("expected_value"))
    if goal_type and goal_value:
        target = f"{goal_type} — {goal_value}"
    else:
        target = goal_type or goal_value
    if not target:
        target = one_line(original_prompt)
    if target:
        lines.append(f"- MỤC TIÊU CUỐI (đọc đúng như tester viết): {target}")
    lines.append(
        "- Đạt một kết quả trung gian ở trên KHÔNG phải là hoàn thành cả việc. Chưa đủ bằng "
        "chứng cho mục tiêu cuối thì báo CHƯA XÁC MINH, kèm màn hình đang thấy."
    )
    lines.append(
        "- Khi mục tiêu cuối ĐÃ xác minh được bằng quan sát trên UI ⇒ KẾT THÚC ngay và báo "
        "kết quả. Không làm lại một bước đã xong, không thoát ra rồi vào lại, kể cả khi kế "
        "hoạch nhắc lại việc đó."
    )
    return dedupe_sentences(lines)


def render_stop_conditions(entries: Sequence[TaskKnowledge]) -> list[str]:
    """Mục điều kiện dừng: khi nào KHÔNG được đi tiếp, và kết luận phải nêu gì.

    Ba điều kiện đến từ *logic chung*, không từ ứng dụng nào:

    * bước chỉ bắt đầu khi kết quả của bước trước đã quan sát được — điều kiện tiên quyết
      của mỗi bước chính là kết quả mà KB khai cho bước ngay trước nó;
    * bước tìm/chọn trong danh sách chỉ đi tiếp khi thấy **đúng** mục tiêu: không chọn mục
      thay thế, không tự đoán giá trị, không quét lại vô hạn;
    * hết danh sách mà không thấy ⇒ dừng và kết luận ``NOT_FOUND`` kèm lý do cụ thể; **chưa**
      kiểm tra hết ⇒ ``NOT_VERIFIED``, không được kết luận là không có.

    ``NOT_FOUND`` và ``NOT_VERIFIED`` phải khác nhau: gộp chúng lại thì một lần quét dở bị
    báo như một kết luận chắc chắn, và ngược lại một kết luận chắc chắn bị coi là thiếu bằng
    chứng.  Phần nói về danh sách chỉ in khi thật sự có bước tìm/chọn, để prompt không phình.
    """
    has_search = any(
        is_search_step(step)
        for entry in entries
        for hit in [*entry.primary, *entry.supporting]
        for step in hit.workflow.steps
    )
    lines = [
        "- Chỉ bắt đầu một bước khi kết quả mà bước ngay trước đó nói phải thấy đã quan sát "
        "được trên UI. Chưa thấy thì dừng lại quan sát, không làm tiếp bước sau.",
        "- Làm đúng thao tác mà KB ghi (chạm, nhấn giữ, nhập, cuộn…). Nếu một thao tác không "
        "tạo ra kết quả mà KB nói phải thấy, KHÔNG lặp lại y nguyên quá 2 lần: kiểm lại xem có "
        "phải thao tác khác (ví dụ nhấn giữ thay vì chạm), rồi nếu vẫn không được thì dừng và "
        "báo chưa làm được kèm màn hình đang thấy.",
    ]
    if has_search:
        lines.extend(
            [
                "- Bước tìm/chọn trong danh sách: chỉ đi tiếp khi tìm thấy ĐÚNG mục tiêu. "
                "Không chọn mục khác thay thế, không tự đoán giá trị, không bịa là đã thấy.",
                f"- Đã kiểm tra HẾT danh sách mà không có mục tiêu ⇒ DỪNG và kết luận "
                f"FAILED với lý do cụ thể: mục cần tìm, đã kiểm tra những gì, màn hình đang "
                f"thấy (mã {CODE_NOT_FOUND}).",
                f"- CHƯA kiểm tra hết danh sách ⇒ không được kết luận là không có; tiếp tục "
                f"kiểm tra, hoặc kết luận chưa đủ bằng chứng (mã {CODE_NOT_VERIFIED}).",
                "- Không lặp lại cùng một thao tác tìm kiếm quá 2 lần và không quét lại danh "
                "sách vô hạn. Nếu lặp mà màn hình không đổi, coi như đã kiểm tra hết.",
            ]
        )
    return lines


def compose_prompt(
    original_prompt: str,
    entries: Sequence[TaskKnowledge],
    final_goal: dict,
    *,
    max_chars: int,
) -> str:
    """Prompt cuối cùng: prompt gốc nguyên văn + các mục có nội dung, đúng thứ tự.

    Mục nào không có nội dung thì bỏ — việc đơn giản không cần đủ năm tiêu đề.  Mục
    ``CÁC BƯỚC THỰC HIỆN`` không bao giờ bị bỏ (chỉ cắt bớt) vì nó là phần nói *cách làm*.
    """
    blocks: list[tuple[str, list[str]]] = [
        (SECTION_TASK, render_tasks(entries)),
        (SECTION_STATE, render_state(entries)),
        (SECTION_STEPS, render_steps(entries)),
        (SECTION_STOP, render_stop_conditions(entries)),
        (SECTION_NOTES, render_notes(entries)),
        (SECTION_VERIFY, render_verify(entries, final_goal, original_prompt)),
    ]
    body: list[str] = [GROUND_RULE]
    used = len(GROUND_RULE)
    for header, lines in blocks:
        if not lines:
            continue
        chunk = [header, *lines]
        size = sum(len(line) + 1 for line in chunk)
        if used + size > max_chars and header != SECTION_STEPS:
            continue
        body.extend(chunk)
        used += size
    goal = (original_prompt or "").strip()
    return f"{goal}\n\n" + "\n".join(body).strip()
