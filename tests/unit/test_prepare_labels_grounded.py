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

"""Chốt ``labels_grounded``: vocabulary có nguồn gốc, và sửa **cục bộ** thay vì bỏ cả prompt.

Bối cảnh đo trên máy thật 29/10/2026: một nhãn bị coi là "bịa" (dù nó có thật trong KB, chỉ là
không nằm trong tập truy hồi) ⇒ chốt fail ⇒ ``fallback`` thay **cả** prompt ⇒ tester mất toàn bộ
hướng dẫn KB ⇒ agent tự bấm Đăng xuất và bị chặn 3 lần. Hai luật được khoá ở đây:

1. Nhãn **có thật trong KB** (kể cả mục không được truy hồi) hoặc trong câu tester là hợp lệ.
2. Nhãn bịa chỉ bị bỏ ở **dòng chứa nó**; riêng CÁC BƯỚC THỰC HIỆN và XÁC MINH KẾT QUẢ vẫn là
   lỗi ở mức cả prompt — không được bỏ một bước để cho qua.
"""

from __future__ import annotations

from artemis.knowledge.composer import (
    SECTION_NOTES,
    SECTION_STEPS,
    SECTION_TASK,
    SECTION_VERIFY,
)
from artemis.knowledge.models import Workflow, WorkflowStep
from artemis.knowledge.prepare import (
    composed_label_vocabulary,
    strip_ungrounded_labels,
    ungrounded_labels,
    validate_composed,
)


SWITCH_ACCOUNT = Workflow(
    id="auth.switch_account",
    app_id="hunonic",
    name="Đăng nhập bằng tài khoản đã lưu",
    steps=[WorkflowStep(action="tap", target="Mũi tên caret-down cuối ô số điện thoại")],
    status="verified",
)

NAV_ENTRY = Workflow(
    id="navigation.open_context_from_home",
    app_id="hunonic",
    name="Mở màn hình Ngữ cảnh từ home",
    aliases=["Ngữ cảnh chạy khi ấn"],
    metadata={"type": "navigation", "screen": "home"},
    status="draft",
)


COMPOSED = (
    "đăng nhập tài khoản 0999 rồi tạo ngữ cảnh chạy khi ấn\n\n"
    f"{SECTION_TASK}\n"
    "- Việc: đăng nhập tài khoản «0999»\n\n"
    f"{SECTION_STEPS}\n"
    "  1. tap: «Mũi tên caret-down cuối ô số điện thoại»\n\n"
    f"{SECTION_NOTES}\n"
    "- Cách vào màn 'Ngữ cảnh chạy khi ấn': theo KB navigation.open_context_from_home\n"
    "- Nhãn do model tự nghĩ: 'Nút ba chấm ở góc phải trên'\n\n"
    f"{SECTION_VERIFY}\n"
    "- Thấy màn 'Ngữ cảnh chạy khi ấn' là đạt\n"
)


# ── vocabulary có nguồn gốc ──────────────────────────────────────────────
def test_a_label_from_a_kb_entry_outside_the_retrieved_set_is_grounded():
    """Mục KB **có thật** nhưng không được truy hồi vẫn là nguồn hợp lệ cho nhãn.

    Đây chính là ca đã làm prompt máy thật rơi về bản dự phòng: composer nhắc tới mục điều hướng
    (``navigation.*``) không nằm trong tập truy hồi của câu tester.
    """
    vocabulary = composed_label_vocabulary(
        "đăng nhập tài khoản 0999 rồi tạo ngữ cảnh chạy khi ấn",
        [],
        vocabulary_workflows=[SWITCH_ACCOUNT, NAV_ENTRY],
    )

    ungrounded = ungrounded_labels(COMPOSED, vocabulary)

    # Chỉ còn đúng nhãn model tự nghĩ; mọi nhãn đến từ KB hoặc từ câu tester đều đã có căn cứ.
    assert ungrounded == ["Nút ba chấm ở góc phải trên"]


def test_without_the_index_a_real_kb_label_looks_invented():
    """Răng của test trên: thiếu nguồn KB thì chốt phải bắt — nếu không, test trên vô nghĩa.

    Nhãn ở đây là **tên workflow** trong KB (không có trong câu tester), nên nó chỉ được coi là có
    căn cứ khi mục KB đó nằm trong vocabulary.
    """
    composed = f"{SECTION_NOTES}\n- Đi theo KB: 'Mở màn hình Ngữ cảnh từ home'\n"
    original = "đăng nhập tài khoản 0999 rồi tạo ngữ cảnh chạy khi ấn"

    with_index = composed_label_vocabulary(original, [], vocabulary_workflows=[NAV_ENTRY])
    without_index = composed_label_vocabulary(original, [])

    assert ungrounded_labels(composed, with_index) == []
    assert "Mở màn hình Ngữ cảnh từ home" in ungrounded_labels(composed, without_index)


def test_tester_data_is_grounded_by_the_original_prompt():
    """Dữ liệu tester ("Mesh Tạo 2") không phải nhãn bịa."""
    composed = f"{SECTION_NOTES}\n- Chuyển sang nhà «Mesh Tạo 2»\n"
    vocabulary = composed_label_vocabulary("chuyển nhà sang Mesh Tạo 2", [])
    assert ungrounded_labels(composed, vocabulary) == []


def test_quote_and_case_variants_do_not_count_as_invented():
    """Cùng một nhãn viết khác nháy/hoa-thường thì không được coi là bịa."""
    vocabulary = composed_label_vocabulary("", [], vocabulary_workflows=[SWITCH_ACCOUNT])
    composed = f"{SECTION_NOTES}\n- Bấm ’MŨI TÊN CARET-DOWN CUỐI Ô SỐ ĐIỆN THOẠI‘\n"
    assert ungrounded_labels(composed, vocabulary) == []


# ── sửa cục bộ thay vì bỏ cả prompt ─────────────────────────────────────
def test_only_the_offending_line_is_removed():
    """Nhãn bịa ở mục LƯU Ý ⇒ bỏ đúng dòng đó, phần còn lại giữ nguyên."""
    cleaned, removed, blocked = strip_ungrounded_labels(COMPOSED, ["Nút ba chấm ở góc phải trên"])

    assert blocked == []
    assert len(removed) == 1
    assert "Nút ba chấm" in removed[0]
    # Phần có căn cứ vẫn còn: nhãn KB, bước thao tác, tiêu chí xác minh.
    assert "Mũi tên caret-down cuối ô số điện thoại" in cleaned
    assert "navigation.open_context_from_home" in cleaned
    assert "Thấy màn 'Ngữ cảnh chạy khi ấn' là đạt" in cleaned


def test_a_violation_inside_the_steps_is_not_strippable():
    """CÁC BƯỚC THỰC HIỆN: không được bỏ một bước để cho qua chốt."""
    composed = (
        f"{SECTION_STEPS}\n"
        "  1. tap: 'Nút ba chấm ở góc phải trên'\n"
        f"{SECTION_NOTES}\n"
        "- ghi chú bình thường\n"
    )

    cleaned, removed, blocked = strip_ungrounded_labels(composed, ["Nút ba chấm ở góc phải trên"])

    assert removed == []
    assert len(blocked) == 1
    # Không sửa nội dung khi vi phạm nằm ở mục không được sửa (chỉ chuẩn hoá khoảng trắng cuối).
    assert "1. tap: 'Nút ba chấm ở góc phải trên'" in cleaned
    assert SECTION_STEPS in cleaned
    assert "- ghi chú bình thường" in cleaned


def test_a_violation_inside_the_verification_is_not_strippable():
    """XÁC MINH KẾT QUẢ: cũng là mục không được sửa cục bộ."""
    composed = f"{SECTION_VERIFY}\n- Đạt khi thấy 'Bảng điều khiển riêng'\n"
    _, removed, blocked = strip_ungrounded_labels(composed, ["Bảng điều khiển riêng"])

    assert removed == []
    assert len(blocked) == 1


def test_a_section_left_empty_by_stripping_loses_its_header():
    """Mục chỉ còn tiêu đề là mục rỗng — phải bỏ luôn tiêu đề."""
    composed = (
        f"{SECTION_TASK}\n"
        "- Việc: mở app\n\n"
        f"{SECTION_NOTES}\n"
        "- chỉ dòng này bị bỏ: 'Nhãn bịa hoàn toàn'\n"
    )
    cleaned, removed, _ = strip_ungrounded_labels(composed, ["Nhãn bịa hoàn toàn"])

    assert len(removed) == 1
    assert SECTION_NOTES not in cleaned
    assert SECTION_TASK in cleaned


def test_stripping_is_a_no_op_without_labels():
    assert strip_ungrounded_labels(COMPOSED, []) == (COMPOSED, [], [])


def test_validation_after_stripping_passes_with_the_widened_vocabulary():
    """Đúng chuỗi việc mà ``prepare`` làm: bỏ dòng ⇒ kiểm lại ⇒ đạt, không rơi về bản dự phòng."""
    vocabulary = composed_label_vocabulary(
        "đăng nhập tài khoản 0999 rồi tạo ngữ cảnh chạy khi ấn",
        [],
        vocabulary_workflows=[SWITCH_ACCOUNT, NAV_ENTRY],
    )
    cleaned, removed, blocked = strip_ungrounded_labels(
        COMPOSED, ungrounded_labels(COMPOSED, vocabulary)
    )

    assert removed and not blocked
    checks = validate_composed(
        "đăng nhập tài khoản 0999 rồi tạo ngữ cảnh chạy khi ấn",
        cleaned,
        [],
        max_chars=4000,
        # Id trong prompt (``navigation.open_context_from_home``) là mục **có thật trong KB** —
        # đúng cách ``prepare`` truyền ``known_extra`` = toàn bộ id của chỉ mục KB.
        known_extra={"navigation.open_context_from_home"},
        vocabulary_workflows=[SWITCH_ACCOUNT, NAV_ENTRY],
    )
    assert [check.name for check in checks if not check.ok] == []
