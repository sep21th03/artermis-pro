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
from artemis.knowledge.text import content_tokens, identity_tokens, normalize

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

#: Từ chỉ *một mục đang dùng* — dấu hiệu điều kiện trước nói về **trạng thái hiện tại**
#: ("Đã chọn đúng nhà") chứ không phải một đối tượng để thao tác ("Đã xác định đúng nhà cần
#: xóa").  Lớp ngôn ngữ, không phải từ khoá của ứng dụng nào.
_SELECTION_MARKERS = ("chọn", "đang ở", "đang dùng", "đang chọn", "đang hoạt động", "hiện tại")

#: Dấu hiệu điều kiện trước nói về **màn hình/trạng thái hiển thị** — nhóm duy nhất mà câu
#: "workflow khác đạt được điều kiện này" là đúng; xem :func:`render_condition_achievers`.
_SCREEN_MARKERS = ("màn hình", "thanh điều hướng", "đã mở", "đang ở")

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


def _mentions(text: object, phrase: str) -> bool:
    """``phrase`` có mặt trong ``text`` xét theo **token**, không phải theo chuỗi con.

    So chuỗi con khớp sai trong tiếng Việt không dấu (``"chon"`` nằm trong ``"chong"``), và
    ``content_tokens`` đã bỏ từ dừng nên ``"đang ở"`` chỉ còn ``o`` — đúng thứ cần so.
    """
    tokens = set(content_tokens(text))
    return all(part in tokens for part in content_tokens(phrase))


def _mentions_any(text: object, phrases: Sequence[str]) -> bool:
    """``text`` có nhắc tới **một trong** các cụm ``phrases`` hay không."""
    return any(_mentions(text, phrase) for phrase in phrases)



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


def _overlap_ratio(left: str, right: str) -> float:
    """Tỉ lệ token khái niệm chung trên câu **ngắn hơn** (0 khi một câu rỗng).

    Tách ra thành một hàm vì có hai chỗ cần **cùng công thức**: nhận ra hai câu nói cùng một
    điều, và tìm workflow *đạt được* một điều kiện trước. Để hai chỗ tự viết lại công thức là
    để chúng lệch nhau lúc nào không biết.
    """
    left_tokens, right_tokens = _concept_tokens(left), _concept_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


def _similar(left: str, right: str, threshold: float = 0.8) -> bool:
    """Hai câu nói cùng một điều (độ phủ token hai chiều, không phụ thuộc thứ tự)."""
    return _overlap_ratio(left, right) >= threshold


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


#: Hướng vuốt của công cụ là hướng **ngón tay**, không phải hướng danh sách chạy.  Chỗ này từng
#: làm agent kẹt: KB ghi "cuộn xuống" (ý là xem mục ở dưới) nên nó chọn ``direction='down'`` — mà
#: ``'down'`` là vuốt xuống, tức lộ nội dung **bên trên**.  Đo 29/10/2026: hai lượt "Vuốt" liên
#: tiếp trên màn Menu, màn hình không đổi, mục 'Đăng xuất' không bao giờ hiện.
_SCROLL_DOWN_WORDS = ("cuon xuong", "scroll down", "vuot xuong", "keo xuong", "swipe down")
_SCROLL_UP_WORDS = ("cuon len", "scroll up", "vuot len", "keo len", "swipe up")
SWIPE_DOWN_HINT = "(muốn lộ nội dung BÊN DƯỚI thì vuốt LÊN: direction='up')"
SWIPE_UP_HINT = "(muốn lộ nội dung BÊN TRÊN thì vuốt XUỐNG: direction='down')"


def _swipe_direction_hint(text: str) -> str:
    """Nhắc hướng vuốt đúng khi chữ của bước nói tới việc cuộn.

    So trên dạng đã bỏ dấu, nên khớp cả khi KB viết hoa/thường hoặc không dấu.
    """
    lowered = normalize(text)
    if any(normalize(word) in lowered for word in _SCROLL_DOWN_WORDS):
        return SWIPE_DOWN_HINT
    if any(normalize(word) in lowered for word in _SCROLL_UP_WORDS):
        return SWIPE_UP_HINT
    return ""


def _step_action_text(step: Any) -> str:
    """Dòng thao tác của một bước, gồm cả tham số KB khai (``value``, ``until``).

    Hai trường này bị bỏ trước đây, và đó là mất thông tin thật: 12 bước trong KB khai
    ``value`` (giá trị cần nhập) và 13 bước khai ``until`` ("cuộn cho tới khi X xuất hiện").
    Bỏ ``value`` là agent phải tự đoán giá trị; bỏ ``until`` là mất điều kiện dừng của vòng
    lặp — đúng loại điều kiện mà prompt cần nêu.

    Bước nào nói tới việc cuộn thì kèm luôn hướng vuốt đúng cho công cụ (xem
    :func:`_swipe_direction_hint`).
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
    hint = _swipe_direction_hint(f"{text} {target} {until}")
    if hint:
        text += f" {hint}"
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


@dataclass(frozen=True)
class StateDimension:
    """Một *trạng thái có giá trị* của ứng dụng, KB khai trong ``metadata.state_dimension``.

    **Vì sao khai ở KB, không phải một bảng trong Python.**  KB đã là nguồn sự thật của luồng
    thao tác; thêm một danh sách từ khoá trong code là chỗ thứ hai phải sửa mỗi khi có ứng
    dụng mới — đúng bài học của ``REQUIRED_INPUT_MARKERS`` đã bị xoá.  Khai trên chính workflow
    **quan sát** được trạng thái đó nên không có bảng nào phải đồng bộ với KB.

    ``label`` là câu người dùng nói để chỉ trạng thái ("tài khoản đang đăng nhập"), còn
    ``value_hint`` là từ dẫn ra **giá trị** của nó ("tài khoản", "nhà") — dùng để biết một giá
    trị tester nêu thuộc trạng thái nào.

    ``set_workflows`` là **nhiều** workflow vì một trạng thái có thể có nhiều đường đặt giá trị
    và đường nào dùng được là do dữ liệu tester cung cấp quyết định: đổi tài khoản đang đăng nhập
    thì có mật khẩu ⇒ ``auth.login_password``, không có mật khẩu mà tài khoản đã lưu trên máy ⇒
    ``auth.switch_account``.  Chốt cứng một đường là dạy agent đăng nhập bằng mật khẩu nó không có.
    """

    id: str
    label: str
    value_hint: str = ""
    observe: str = ""
    #: Các bước của chính workflow *quan sát* (``observe``), dạng ``thao tác → kết quả mong đợi``.
    #: Chỉ nêu id là không đủ: agent không đọc được KB, nên "theo KB auth.current_account" không
    #: cho nó biết phải chạm vào đâu — đo trên máy thật, nó tap mò "profile icon top right" ba
    #: lần rồi kết luận đã đăng nhập.  Guard in thẳng các bước này để có đường quan sát tường minh.
    observe_steps: tuple[str, ...] = ()
    set_workflows: tuple[str, ...] = ()
    clear_workflow: str = ""
    #: Bước của các workflow *đổi* giá trị (``clear_workflow`` + ``set_workflows``), theo thứ tự
    #: ``(id, tên, các bước)``.  Guard chỉ nêu id thì agent không biết phải chạm vào đâu — đo trên
    #: máy thật 29/10/2026: phát hiện đang đăng nhập sai tài khoản nhưng prompt không có bước đăng
    #: xuất, nên agent đi tiếp sang màn của tài khoản sai thay vì đăng xuất rồi đăng nhập lại.
    change_steps: tuple[tuple[str, str, tuple[str, ...]], ...] = ()


def _observe_step_lines(workflow: Workflow) -> tuple[str, ...]:
    """Các bước *quan sát* của workflow khai ``state_dimension``, dạng ``thao tác → kết quả``.

    Guard trạng thái phải trả lời được câu "làm sao biết đang là giá trị nào".  Nêu id workflow
    là chưa đủ vì agent chỉ thấy prompt, không đọc KB: đo trên máy thật với câu *"đăng nhập tài
    khoản 0999 rồi vào màn ngữ cảnh chạy khi ấn…"*, agent tap mò "profile icon top right" ba lần
    rồi tự kết luận "we have already verified the account" và bỏ qua bước đăng nhập — trong khi
    KB đã ghi rõ cách đọc: mở **tab Menu** rồi đọc số ở khối tài khoản trên cùng.

    Dùng đúng cách render của :func:`render_steps` (``action → expected_state``, kèm guard tìm
    kiếm) để mục 2 và mục 3 của prompt đọc giống nhau; không bịa thêm chữ nào ngoài KB.
    """
    shape = normalize_workflow(workflow)
    lines: list[str] = []
    for index, action in enumerate(shape.actions):
        text = action
        state = shape.expected_results[index]
        if state:
            text += f" → {state}"
        if index < len(workflow.steps) and is_search_step(workflow.steps[index]):
            text += f" {SEARCH_GUARD}"
        lines.append(text)
    return tuple(dedupe_sentences(lines, mark_conflicts=False, merge_rules=False))


def state_dimensions(workflows: Iterable[Workflow]) -> list[StateDimension]:
    """Đọc khai báo trạng thái từ ``metadata.state_dimension`` của các workflow của một app.

    ``observe`` luôn là chính workflow đang khai (nó là workflow *đọc* giá trị); ``set_workflows``
    và ``clear_workflow`` do KB trỏ tới.  Workflow được trỏ tới mà **không có trong KB** thì bỏ
    trống, không in ra: prompt không được nhắc một id không tồn tại.
    """
    workflows = list(workflows)
    known = {workflow.id for workflow in workflows}
    out: list[StateDimension] = []
    for workflow in workflows:
        spec = (workflow.metadata or {}).get("state_dimension")
        if not isinstance(spec, dict):
            continue
        dimension_id = one_line(spec.get("id"))
        label = one_line(spec.get("label"))
        if not dimension_id or not label:
            continue
        setters = spec.get("set_workflows") or spec.get("set_workflow") or ()
        if isinstance(setters, str):
            setters = [setters]
        clear = one_line(spec.get("clear_workflow"))
        out.append(
            StateDimension(
                id=dimension_id,
                label=label,
                value_hint=one_line(spec.get("value_hint")),
                observe=workflow.id,
                observe_steps=_observe_step_lines(workflow),
                set_workflows=tuple(
                    item
                    for item in (one_line(value) for value in setters)
                    if item and item in known
                ),
                clear_workflow=clear if clear in known else "",
                change_steps=_change_step_lines(
                    [
                        clear if clear in known else "",
                        *(
                            item
                            for item in (one_line(value) for value in setters)
                            if item in known
                        ),
                    ],
                    {workflow.id: workflow for workflow in workflows},
                ),
            )
        )
    return out


def _change_step_lines(
    workflow_ids: Sequence[str],
    by_id: dict[str, Workflow],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """Bước của các workflow đổi giá trị, đúng thứ tự ``clear_workflow`` rồi tới ``set_workflows``.

    Thứ tự này là *trình tự thật* khi đổi tài khoản: đăng xuất trước (đưa app về màn Đăng nhập),
    rồi mới đăng nhập bằng tài khoản đích.
    """
    out: list[tuple[str, str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for workflow_id in workflow_ids:
        if not workflow_id or workflow_id in seen:
            continue
        workflow = by_id.get(workflow_id)
        if workflow is None:
            continue
        steps = _observe_step_lines(workflow)
        if not steps:
            continue
        seen.add(workflow_id)
        out.append((workflow_id, one_line(workflow.name), steps))
    return tuple(out)


def _dimension_value(entry: TaskKnowledge, dimension: StateDimension, goal_value: str) -> str:
    """Giá trị mà việc này (hoặc mục tiêu cuối) nêu cho trạng thái ``dimension``.

    **Giá trị là định danh, không phải cả mệnh đề.**  Đo trên lần chạy thật: bước phân tích
    mục tiêu trả ``target`` = ``"0972842548 mật khẩu (Hoa21092003)"``, nên lấy nguyên ``target``
    làm giá trị sẽ sinh dòng "tài khoản đang đăng nhập LÀ «0972842548 mật khẩu (Hoa21092003)»"
    — vừa sai (không tài khoản nào tên như vậy) vừa kéo mật khẩu vào phép so sánh.  Ở đây lấy
    **định danh** trong câu tester (``identity_tokens``) trước, và chỉ nhận cả ``target`` khi nó
    ngắn — tức đúng là một giá trị ("0999", "nhà Dled").

    Không suy diễn: không có giá trị dùng được ⇒ chuỗi rỗng, và guard không in (thiếu một lời
    khuyên còn hơn một phép so sánh sai).
    """
    hint = dimension.value_hint or dimension.label
    target = one_line(entry.task.target)
    if target and _mentions(f"{entry.task.action} {target}", hint):
        identifiers = identity_tokens(target)
        if identifiers:
            return identifiers[0]
        if len(_concept_tokens(target)) <= 3:
            return target
        return ""
    # Việc này không nêu giá trị, nhưng mục tiêu cuối có thể nêu (điều kiện trước "Đã chọn
    # đúng nhà" của `scene.create_press` lấy giá trị từ "nhà Dled" ở mục tiêu cuối).
    goal = one_line(goal_value)
    if goal and _mentions(goal, hint):
        identifiers = identity_tokens(goal)
        return identifiers[0] if identifiers else goal
    return ""


def _guard_lines(
    entry: TaskKnowledge,
    dimension: StateDimension,
    trigger: str,
    value: str,
) -> list[str]:
    """Các dòng của một guard trạng thái: khi nào điều kiện đã đúng, cách quan sát, cách đổi.

    ``value`` là giá trị tester đã nêu (hàm gọi đã chắc chắn có): guard chỉ tồn tại để **so**
    giá trị, nên không có giá trị thì không có guard.
    """
    head = _task_head(entry)
    lines = [
        f"- «{head}»: {trigger} chỉ được coi là ĐÃ ĐÚNG khi quan sát thấy {dimension.label} "
        f"LÀ «{value}». Đang là giá trị KHÁC thì điều kiện CHƯA đúng: phải đạt «{value}» "
        f"trước khi bỏ qua việc này."
    ]
    if dimension.observe:
        if dimension.observe_steps:
            # Mỗi dòng tự định danh ("Bước N") thay vì dựa vào thụt lề: mục 2 đi qua
            # ``dedupe_sentences`` (mục 3 thì không), và bước dedupe chuẩn hoá mỗi dòng nên
            # thụt lề bị mất — số thứ tự trơ ra ở cột 0 thì đọc như mục con của NHIỆM VỤ.
            lines.append(
                f"- Cách quan sát {dimension.label} (theo KB {dimension.observe})"
                f" — làm lần lượt {len(dimension.observe_steps)} bước:"
            )
            lines.extend(
                f"- Bước {index}: {text}"
                for index, text in enumerate(dimension.observe_steps, 1)
            )
        else:
            lines.append(f"- Cách quan sát {dimension.label}: theo KB {dimension.observe}.")
    setters = list(dimension.set_workflows)
    if dimension.clear_workflow and setters:
        lines.append(
            f"- Nếu phải đổi {dimension.label}: làm theo KB {dimension.clear_workflow}, rồi "
            f"một trong: {', '.join(setters)} — chọn cách mà câu tester đủ dữ liệu để làm."
        )
    elif dimension.clear_workflow:
        lines.append(f"- Nếu phải đổi {dimension.label}: làm theo KB {dimension.clear_workflow}.")
    elif setters:
        lines.append(
            f"- Nếu phải đặt {dimension.label}: làm theo KB " + ", ".join(setters) + "."
        )
    if dimension.change_steps:
        # Bước cụ thể của các workflow đổi giá trị: guard chỉ nêu id thì agent phải tự tìm mục
        # Đăng xuất/chuyển tài khoản trên màn hình, và đo trên máy thật nó đã bỏ qua luôn việc đổi.
        # Chỉ in workflow **chưa có trong prompt** (bước của cách làm chính đã nằm ở mục kiến thức),
        # và tối đa hai workflow — mỗi workflow 4 bước, in hết cả ba là ăn hết hạn mức của brief.
        covered = {hit.workflow.id for hit in [*entry.primary, *entry.supporting]}
        missing = [item for item in dimension.change_steps if item[0] not in covered]
        # Ưu tiên bước "xoá giá trị" (đăng xuất) — đó là phần luôn thiếu khi agent phát hiện sai
        # tài khoản; các cách *đặt* giá trị thường đã là cách làm chính nên bước của chúng đã có
        # trong prompt.  Tối đa một workflow: bốn bước kèm điều kiện dừng đã ~1.100 ký tự.
        picks = [
            item for item in missing if item[0] == dimension.clear_workflow
        ][:1] or missing[:1]
        if picks:
            lines.append(f"- Bước để đổi {dimension.label} (theo KB, làm lần lượt):")
            for workflow_id, name, steps in picks:
                lines.append(f"- «{workflow_id}» — {name}:")
                lines.extend(
                    f"- Bước {index}: {text}" for index, text in enumerate(steps, 1)
                )
    lines.append(
        f"- Không quan sát được {dimension.label} ⇒ DỪNG, báo {CODE_NOT_VERIFIED}; KHÔNG được "
        f"coi là đã đúng."
    )
    return lines


def render_state_guards(
    entries: Sequence[TaskKnowledge],
    dimensions: Sequence[StateDimension] = (),
    final_goal: dict | None = None,
) -> list[str]:
    """Mục 2 (phần 2): trạng thái *có giá trị* phải đạt — và cách quan sát nó.

    **Vì sao cần.**  Điều kiện ``"chỉ khi chưa đăng nhập"`` là phép kiểm tra **nhị phân**:
    đang đăng nhập bằng *bất kỳ* tài khoản nào cũng tính là "đã đúng", nên agent **bỏ qua việc
    đăng nhập**.  Đo trên máy thật với câu *"đăng nhập tài khoản 0999 rồi vào màn ngữ cảnh chạy
    khi ấn…"*: prompt không có dòng nào buộc phải quan sát *đang đăng nhập bằng tài khoản nào*,
    và agent vào thẳng màn Ngữ cảnh.  Yêu cầu thật của tester là **so giá trị**, không phải so
    trạng thái.

    Hai đường sinh ra cùng một luật "chỉ đúng khi thấy ĐÚNG giá trị":

    * việc có ``condition`` (tester/KB nói "chỉ khi…") và việc đó nói về trạng thái này;
    * điều kiện trước của cách làm chính nói về một mục **đang dùng** ("Đã chọn đúng nhà").

    Mỗi cặp (việc, trạng thái) chỉ in một lần, dù cả hai đường cùng đúng.
    """
    goal_value = one_line((final_goal or {}).get("expected_value"))
    lines: list[str] = []
    covered: set[tuple[int, str]] = set()
    for entry in entries:
        for dimension in dimensions:
            key = (entry.index, dimension.id)
            if key in covered:
                continue
            value = _dimension_value(entry, dimension, goal_value)
            # Không có giá trị tester nêu thì **không** có gì để so: điều kiện "chỉ khi chưa
            # đăng nhập" khi tester không nêu tài khoản nào đúng là phép kiểm nhị phân, và dòng
            # "quan sát thấy đúng giá trị mà câu tester nêu" chỉ làm prompt khó hiểu.
            if not value:
                continue
            trigger = ""
            condition = one_line(entry.task.condition)
            if condition and _mentions(
                f"{entry.task.action} {entry.task.target}", dimension.label
            ):
                trigger = f"điều kiện «{condition}»"
            if not trigger:
                hit = _primary(entry)
                preconditions = normalize_workflow(hit.workflow).preconditions if hit else ()
                for text in preconditions:
                    if _mentions_any(text, _SELECTION_MARKERS) and _mentions(
                        text, dimension.label
                    ):
                        trigger = f"điều kiện trước «{text}»"
                        break
            if not trigger:
                continue
            covered.add(key)
            lines.extend(_guard_lines(entry, dimension, trigger, value))
    return lines


def render_condition_achievers(
    entries: Sequence[TaskKnowledge],
    index: Sequence[Workflow] = (),
    dimensions: Sequence[StateDimension] = (),
    final_goal: dict | None = None,
    *,
    threshold: float = 0.8,
) -> list[str]:
    """Mục 2 (phần 3): điều kiện trước *đạt được* bằng workflow khác — nói rõ **cách đạt**.

    Đo trên KB thật (61 workflow, 148 điều kiện trước):

    * khớp với **mọi trường** của workflow khác (``success_criteria`` + ``expected_state``) ở
      ngưỡng 0,8 cho 21 cặp nhưng chỉ **7 cặp đúng (33%)** — sai điển hình là *phản nghĩa*:
      "Ứng dụng đang ở màn hình Đăng nhập" ↔ "Ứng dụng **rời** màn hình Đăng nhập" (0,86);
    * khớp **chỉ với ``success_criteria``** ở cùng ngưỡng cho 9 cặp, **8 cặp đúng**.

    Ngữ nghĩa của chênh lệch đó: ``success_criteria`` là trạng thái *sau khi* workflow xong nên
    là điểm vào hợp lệ cho workflow khác; ``expected_state`` là trạng thái *giữa* một workflow
    khác nên không thể là đích đến.  Vì vậy ở đây chỉ dùng ``success_criteria``, và chỉ cho
    điều kiện nói về **màn hình/trạng thái hiển thị** — cặp sai duy nhất ở 0,8 là "Có quyền
    chia sẻ các thiết bị" ← "màn hình chia sẻ … xuất hiện", mà mở màn hình không tạo ra quyền.

    Hạ ngưỡng xuống 0,75 là hỏng ngay (đo được): nhiều cặp "Người dùng đã đăng nhập" bị ghép
    với ``auth.logout`` vì tiêu chí của nó là "*Không còn* thanh điều hướng …" — ngược nghĩa.
    Sau chốt màn hình, đo lại **trên KB thật bằng chính hàm này**: 6 lời khuyên, cả 6 đều đúng
    (``auth.switch_account`` ← ``auth.logout``; bốn ``navigation.*`` ← ``auth.login_password``;
    ``room.toggle_all_devices`` ← ``navigation.open_rooms``).  Hai cặp đúng bị chốt màn hình bỏ
    qua (``scene.run_press`` ← ``scene.create_press``, ``timer.edit_device_timer`` ←
    ``timer.create_device_timer``) vì điều kiện của chúng nói về *dữ liệu có sẵn*, không phải
    màn hình — đổi lại là không còn lời khuyên sai nào.

    **Chốt "tester đã nêu giá trị".**  Đạt một điều kiện trước có thể phải **phá** trạng thái
    hiện tại (đăng xuất để về màn hình Đăng nhập).  Phá trạng thái mà tester không yêu cầu thì
    tệ hơn hẳn việc thiếu một lời khuyên, nên dòng này chỉ in khi việc đó nêu **giá trị cụ thể**
    cho một trạng thái (tài khoản, nhà).  Hệ quả đã biết: cặp *cộng thêm* (mở Menu cần "đã đăng
    nhập" ← ``auth.login_password``) không được in khi câu tester không nêu tài khoản nào — muốn
    bật thêm trường hợp thì khai thêm ``state_dimension`` trong KB, không sửa code.
    """
    goal_value = one_line((final_goal or {}).get("expected_value"))
    lines: list[str] = []
    for entry in entries:
        hit = _primary(entry)
        if hit is None:
            continue
        if not any(_dimension_value(entry, item, goal_value) for item in dimensions):
            continue
        for precondition in normalize_workflow(hit.workflow).preconditions:
            if not _mentions_any(precondition, _SCREEN_MARKERS):
                continue
            best: tuple[float, str] | None = None
            for candidate in index:
                if candidate.id == hit.workflow.id:
                    continue
                for criterion in candidate.success_criteria:
                    score = _overlap_ratio(precondition, criterion)
                    if score >= threshold and (best is None or score > best[0]):
                        best = (score, candidate.id)
            if best is None:
                continue
            lines.append(
                f"- Nếu «{precondition}» chưa đúng: làm theo KB {best[1]} để đạt "
                f"«{precondition}» trước khi làm «{_task_head(entry)}»."
            )
    return lines


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
    index: Sequence[Workflow] = (),
    dimensions: Sequence[StateDimension] = (),
) -> str:
    """Prompt cuối cùng: prompt gốc nguyên văn + các mục có nội dung, đúng thứ tự.

    Mục nào không có nội dung thì bỏ — việc đơn giản không cần đủ năm tiêu đề.  Mục
    ``CÁC BƯỚC THỰC HIỆN`` không bao giờ bị bỏ (chỉ cắt bớt) vì nó là phần nói *cách làm*.

    ``index`` (toàn bộ workflow của app) và ``dimensions`` (các trạng thái có giá trị mà KB
    khai) là dữ liệu KB **phụ trợ**: thiếu chúng thì prompt vẫn dựng được, chỉ không nói được
    *cách đạt* một điều kiện trước và *cách quan sát* một trạng thái.
    """
    state_lines = dedupe_sentences(
        [
            *render_state(entries),
            *render_state_guards(entries, dimensions, final_goal),
            *render_condition_achievers(entries, index, dimensions, final_goal),
        ]
    )
    blocks: list[tuple[str, list[str]]] = [
        (SECTION_TASK, render_tasks(entries)),
        (SECTION_STATE, state_lines),
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
