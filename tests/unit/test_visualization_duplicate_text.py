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

"""Warnings appended to the element list the agent chooses indices from.

Reported from a real run: an agent spent 20 steps tapping "Đăng xuất"
interchangeably -- the settings row and the button inside the dialog it opens --
because both carry the exact same label and the list said nothing about it.
"""

from __future__ import annotations

from artemis.utils.visualization import (
    _inject_duplicate_text_warnings,
    format_minimal_list_with_elements,
)


def _line(idx: int, text: str, bounds: str = "[0,0][100,100]", kind: str = "Text") -> str:
    return f"[{idx}] {kind}: '{text}' | Bounds: {bounds}"


# ── the warning itself ───────────────────────────────────────────────────
def test_the_same_label_twice_is_flagged_on_both_lines():
    lines = [_line(7, "Đăng xuất"), _line(10, "Đăng xuất")]
    out = _inject_duplicate_text_warnings(lines)
    assert all("WARNING" in line for line in out)
    assert "[10]" in out[0] and "[7]" in out[1]


def test_a_unique_label_is_left_alone():
    lines = [_line(1, "Ngôn ngữ"), _line(2, "Âm lịch")]
    assert _inject_duplicate_text_warnings(lines) == lines


def test_an_empty_list_is_left_alone():
    assert _inject_duplicate_text_warnings([]) == []


def test_three_duplicates_list_both_siblings():
    lines = [_line(1, "Đăng xuất"), _line(2, "Đăng xuất"), _line(3, "Đăng xuất")]
    out = _inject_duplicate_text_warnings(lines)
    assert "[2] and [3]" in out[0]
    assert "+" not in out[0].split("WARNING:")[1].split("carry")[0]


def test_more_than_two_siblings_are_summarised():
    """The warning must not grow without bound on a list of identical rows."""
    lines = [_line(i, "Đăng xuất") for i in range(1, 5)]
    out = _inject_duplicate_text_warnings(lines)
    assert "[2] and [3]" in out[0]
    assert "+1 more" in out[0]


def test_lines_without_text_are_never_touched():
    """Icon-only rows carry no label, so they cannot be confused by label."""
    lines = [_line(1, "Đăng xuất"), "[2] Icon: '' | Bounds: [0,0][10,10]", _line(3, "Đăng xuất")]
    out = _inject_duplicate_text_warnings(lines)
    assert out[1] == lines[1]


def test_the_warning_is_added_not_replacing_the_description():
    """The list must stay a faithful description of the screen."""
    lines = [_line(7, "Đăng xuất", "[0,100][500,200]"), _line(10, "Đăng xuất", "[500,300][700,400]")]
    out = _inject_duplicate_text_warnings(lines)
    assert out[0].startswith(lines[0])
    assert "[0,100][500,200]" in out[0]


def test_hint_and_ocr_lines_are_covered_too():
    lines = [
        _line(1, "Tìm kiếm", kind="Hint"),
        _line(2, "Tìm kiếm", kind="OCR Text"),
    ]
    assert all("WARNING" in line for line in _inject_duplicate_text_warnings(lines))


def test_an_apostrophe_in_the_label_does_not_break_parsing():
    lines = [_line(1, "Đăng xuất"), _line(2, "Đăng xuất")]
    out = _inject_duplicate_text_warnings(lines)
    assert len(out) == 2 and all("WARNING" in line for line in out)


def test_a_label_made_only_of_invisible_characters_is_ignored():
    """A zero-width label renders as nothing, so it cannot be chosen by name."""
    lines = [_line(7, "\u200b"), _line(18, "\u200b")]
    assert _inject_duplicate_text_warnings(lines) == lines


def test_a_symbol_label_is_still_worth_flagging():
    """'+' or '✓' are real, readable labels even without letters."""
    lines = [_line(3, "+"), _line(9, "+")]
    assert all("WARNING" in line for line in _inject_duplicate_text_warnings(lines))


# ── wiring: the warning reaches the list the agent actually reads ────────
def _node(text: str, bounds: str) -> dict:
    return {"text": text, "bounds": bounds, "class": "android.view.ViewGroup", "resource-id": ""}


def test_the_agent_list_flags_a_menu_row_and_its_dialog_button():
    """The reported scenario, through the real formatter."""
    hierarchy = [
        _node("Ngôn ngữ", "[0,300][720,400]"),
        _node("Đăng xuất", "[0,1093][692,1198]"),      # hàng trong menu
        _node("Bạn có chắc chắn muốn đăng xuất không?", "[100,700][620,800]"),
        _node("Đăng xuất", "[532,534][912,592]"),       # nút trong modal
    ]
    text, elements, _labels = format_minimal_list_with_elements(hierarchy, width=720, height=1600)

    assert "WARNING" in text
    assert "same text 'Đăng xuất'" in text
    flagged = [line for line in text.splitlines() if "same text" in line]
    assert len(flagged) == 2, "cả hai dòng cùng tên đều phải được cảnh báo"


def test_a_screen_with_no_repeated_labels_stays_clean():
    hierarchy = [_node("Ngôn ngữ", "[0,300][720,400]"), _node("Âm lịch", "[0,420][720,520]")]
    text, _elements, _labels = format_minimal_list_with_elements(hierarchy, width=720, height=1600)
    assert "WARNING" not in text
