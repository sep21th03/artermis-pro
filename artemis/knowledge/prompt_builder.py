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

"""Công cụ sắp xếp KB thành prompt: chọn cách làm và xếp chúng thành chuỗi tuần tự.

**Vì sao là một bước riêng, không phải vài dòng ``if``.**  Trước đây tầng phân loại
(``classify_task``) quyết định workflow nào là "cách làm" bằng **trùng từ với tên
workflow**, rồi LLM chỉ được viết lại phần đã chọn.  Cách đó sai ở đúng chỗ khó nhất: đo
trên KB thật, câu ``đăng xuất và đăng nhập tài khoản 0972842548`` khiến
``auth.login_password`` và ``auth.switch_account`` **hoà nhau ở độ phủ tên 1.00**, và thứ
tự truy hồi quyết định ai thắng — nên có lượt chọn ``login_password`` và prompt hướng dẫn
agent *gõ số điện thoại vào ô nhập*, trong khi KB đã ghi rõ hai luật:

* ``auth.switch_account``: "**DÙNG KHI** prompt chỉ nêu tên/số tài khoản mà **KHÔNG có
  mật khẩu**"
* ``auth.login_password``: "**Không dùng** workflow này khi prompt chỉ nêu một tài khoản
  đã lưu mà không có mật khẩu."

Luật nằm trong KB, viết bằng tiếng Việt, nên **người đọc luật phải là model đọc KB**, không
phải một danh sách từ khoá trong Python (bản trước từng có ``REQUIRED_INPUT_MARKERS`` — mỗi
workflow mới, mỗi cách nói mới lại phải sửa code).

Đầu vào: yêu cầu của tester, kế hoạch đã tách, **toàn văn KB** của mọi ứng viên cho từng
việc, và các workflow điều hướng của app.  Đầu ra: mỗi việc dùng workflow nào (hoặc không
có), cái nào bị loại vì không hợp, và những workflow điều hướng cần đi kèm.  Không bao giờ
raise: không có LLM / JSON hỏng / id bịa ⇒ trả ``None`` để ``prepare`` giữ đường dựng bằng
luật.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import logging

from artemis.knowledge.intent import (
    IntentPlan,
    TaskSpec,
    extract_json,
    one_line,
    sanitize_ui_instructions,
)
from artemis.knowledge.models import Workflow

logger = logging.getLogger(__name__)

Synthesizer = Callable[..., str]

SELECTION_SYSTEM = (
    "Bạn chọn CÁCH LÀM cho từng việc kiểm thử, dựa trên mô tả do chính knowledge base "
    "viết. Bạn KHÔNG viết prompt, KHÔNG viết lại yêu cầu — chỉ trả về JSON quyết định.\n"
    "Quy tắc, không có ngoại lệ:\n"
    "1. Đọc mô tả của từng ứng viên. Mô tả nói khi nào dùng nó (\"DÙNG KHI …\") và khi nào "
    "KHÔNG dùng (\"Không dùng … khi …\"). Quyết định theo **nội dung đó** và theo yêu cầu "
    "của tester — KHÔNG theo việc tên ứng viên trùng từ với yêu cầu.\n"
    "2. Điều kiện trước của một ứng viên là thứ phải CÓ trong yêu cầu. Yêu cầu không nêu "
    "thì ứng viên đó không được là cách làm, kể cả tên nó khớp hoàn toàn.\n"
    "3. Mỗi việc đúng MỘT cách làm chính (``primary``), hoặc ``null`` nếu không ứng viên "
    "nào thật sự làm được việc đó. Ứng viên không làm việc đó nhưng có ích làm bối cảnh thì "
    "để trong ``supporting``; ứng viên không hợp thì để trong ``rejected``.\n"
    "4. Điều hướng: nếu cách làm chính diễn ra trên một màn hình mà nó không nói cách mở, "
    "chọn workflow điều hướng (``navigation.*``) đưa tới màn hình đó. Chỉ chọn cái thật cần.\n"
    "5. Trả về JSON đúng dạng, không thêm chữ nào khác:\n"
    '{"tasks": [{"index": 1, "primary": "<id hoặc null>", "supporting": ["<id>"], '
    '"rejected": ["<id>"], "reason": "<vì sao, chiếu theo mô tả KB>"}], '
    '"navigation": {"<id cách làm chính>": ["<id navigation.*>"]}}\n'
    "Chỉ dùng id có trong danh sách ứng viên. ``reason`` viết bằng tiếng Việt, ngắn, nêu "
    "đúng câu trong mô tả KB đã dùng để quyết định."
)


@dataclass
class TaskSelection:
    """Cách làm mà model chọn cho một việc, đọc từ mô tả KB."""

    index: int
    primary: str | None = None
    supporting: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class Selection:
    """Kết quả sắp xếp: chọn cách làm cho từng việc + điều hướng đi kèm."""

    tasks: list[TaskSelection] = field(default_factory=list)
    navigation: dict[str, list[str]] = field(default_factory=dict)

    def for_index(self, index: int) -> TaskSelection | None:
        return next((item for item in self.tasks if item.index == index), None)


def render_candidate(workflow: Workflow) -> str:
    """Toàn văn KB của một ứng viên — đủ để model đọc luật chọn, không chỉ cái tên.

    Mô tả là chỗ chứa "DÙNG KHI / Không dùng khi"; bước là chỗ chứa thao tác cụ thể và cảnh
    báo mất dữ liệu.  Bỏ bớt phần nào ở đây là bắt model quyết định trong tối.
    """
    lines = [f"- {workflow.id} | {one_line(workflow.name)}"]
    if workflow.aliases:
        lines.append(f"  tên gọi khác: {', '.join(one_line(alias) for alias in workflow.aliases)}")
    if workflow.description:
        lines.append(f"  mô tả: {sanitize_ui_instructions(one_line(workflow.description))}")
    if workflow.preconditions:
        lines.append(
            "  điều kiện trước (phải có trong yêu cầu của tester): "
            + "; ".join(
                sanitize_ui_instructions(one_line(text)) for text in workflow.preconditions
            )
        )
    for index, step in enumerate(workflow.steps, 1):
        target = sanitize_ui_instructions(one_line(step.target))
        state = sanitize_ui_instructions(one_line(step.expected_state))
        text = f"    {index}. {one_line(step.action)}"
        if target:
            text += f": {target}"
        if state:
            text += f" → {state}"
        lines.append(text)
    return "\n".join(lines)


def build_selection_message(
    request: str,
    plan: IntentPlan,
    candidates: Sequence[tuple[TaskSpec, Sequence[Workflow]]],
    navigation: Sequence[Workflow],
) -> str:
    """Nội dung gửi cho model: yêu cầu gốc + từng việc kèm toàn văn KB của ứng viên."""
    parts = [
        "[YÊU CẦU GỐC CỦA TESTER — không được viết lại]",
        request.strip(),
        "",
        "[CÁC VIỆC CẦN CHỌN CÁCH LÀM]",
    ]
    for index, (task, workflows) in enumerate(candidates, 1):
        head = " ".join(part for part in (task.action, task.target) if part)
        if task.condition:
            head += f" — điều kiện: {task.condition}"
        parts.append(f"VIỆC {index}: {head}")
        if not workflows:
            parts.append("  (không có ứng viên nào được truy hồi)")
        for workflow in workflows:
            parts.append(render_candidate(workflow))
        parts.append("")
    parts.append("[WORKFLOW ĐIỀU HƯỚNG CỦA APP — dùng để chọn cách tới màn hình]")
    if navigation:
        for workflow in navigation:
            parts.append(render_candidate(workflow))
    else:
        parts.append("  (KB chưa có workflow điều hướng nào)")
    return "\n".join(parts)


def parse_selection(payload: object, allowed: set[str]) -> Selection | None:
    """Đọc JSON của model thành :class:`Selection`, loại bỏ mọi id không có thật.

    Id bịa không được phép lọt vào prompt (đó là cách một workflow tưởng tượng trở thành
    bước phải làm), nên mọi id đều phải nằm trong danh sách ứng viên đã đưa.
    """
    if not isinstance(payload, dict):
        return None
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        return None

    tasks: list[TaskSelection] = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        primary = item.get("primary")
        primary = primary if isinstance(primary, str) and primary in allowed else None
        supporting = [
            value
            for value in item.get("supporting") or []
            if isinstance(value, str) and value in allowed
        ]
        rejected = [
            value
            for value in item.get("rejected") or []
            if isinstance(value, str) and value in allowed
        ]
        tasks.append(
            TaskSelection(
                index=index,
                primary=primary,
                supporting=[value for value in supporting if value != primary],
                rejected=[value for value in rejected if value not in {primary, *supporting}],
                reason=one_line(item.get("reason"))[:300],
            )
        )

    navigation: dict[str, list[str]] = {}
    raw_navigation = payload.get("navigation")
    if isinstance(raw_navigation, dict):
        for key, value in raw_navigation.items():
            if not isinstance(key, str) or key not in allowed:
                continue
            values = [item for item in value or [] if isinstance(item, str) and item in allowed]
            if values:
                navigation[key] = values

    if not tasks:
        return None
    return Selection(tasks=tasks, navigation=navigation)


def select_workflows(
    request: str,
    plan: IntentPlan,
    candidates: Sequence[tuple[TaskSpec, Sequence[Workflow]]],
    navigation: Sequence[Workflow],
    synthesizer: Synthesizer | None,
    *,
    max_tokens: int = 900,
    timeout: float = 30.0,
) -> Selection | None:
    """Gọi model đọc KB rồi chọn cách làm cho từng việc.  ``None`` = không quyết được.

    Không bao giờ raise: LLM thiếu/hỏng/trả rác thì bên gọi giữ nguyên đường dựng bằng luật.
    """
    if synthesizer is None:
        return None
    allowed = {workflow.id for _, workflows in candidates for workflow in workflows}
    allowed |= {workflow.id for workflow in navigation}
    if not allowed:
        return None

    messages = [
        {"role": "system", "content": SELECTION_SYSTEM},
        {
            "role": "user",
            "content": build_selection_message(request, plan, candidates, navigation),
        },
    ]
    try:
        raw = synthesizer(messages, max_tokens=max_tokens, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - chọn cách làm hỏng thì dùng đường dựng bằng luật
        logger.warning("Chọn cách làm từ KB thất bại: %s", exc)
        return None

    payload = extract_json(raw if isinstance(raw, str) else getattr(raw, "text", ""))
    if payload is None:
        logger.warning("Model không trả về JSON hợp lệ khi chọn cách làm từ KB")
        return None
    selection = parse_selection(payload, allowed)
    if selection is None:
        logger.warning("JSON chọn cách làm không dùng được")
    return selection


__all__ = [
    "SELECTION_SYSTEM",
    "Selection",
    "TaskSelection",
    "build_selection_message",
    "parse_selection",
    "render_candidate",
    "select_workflows",
]
