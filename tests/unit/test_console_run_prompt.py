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

"""Prompt tester đã duyệt có thật sự được gửi cho agent không.

Lỗi thật đã gặp: tester bấm "Chuẩn bị prompt", xem bản đã ghép kiến thức (có cả
``navigation.open_menu``), rồi bấm "Chạy test" mà **không** bấm "Dùng bản này".  Agent
nhận đúng câu gốc 43 ký tự, không có KIẾN THỨC nào, nên Planner tự bịa bước điều hướng
("tap profile icon ở góc trên bên phải"), Object Detector xác nhận vật thể không tồn tại
với ``confidence=1.00``, và cú tap trả ``ui_changed=False``.  Không có dấu hiệu nào: log
chỉ ghi câu gốc, y hệt một lượt chạy cố ý không dùng kiến thức.

Hai tính chất được khoá ở đây:

1. **Bỏ quên bản đã chuẩn bị thì phải ồn.** Console biết nó vừa ghép kiến thức cho đúng
   câu này, nên lượt chạy không có ``prompt_override`` phải sinh cảnh báo.
2. **Trạng thái đó phải nhìn thấy được.** UI nói rõ lượt chạy sắp tới gửi prompt nào, và
   bản đã duyệt được giữ qua lần tải lại trang (trước đây chỉ nằm trong bộ nhớ trang).
"""

from __future__ import annotations

import importlib
from pathlib import Path

console_server = importlib.import_module("apps.test_console.server")

INDEX = Path(console_server.__file__).parent / "static" / "index.html"
GOAL = "đăng xuất và đăng nhập tài khoản 0972842548"
PREPARED = GOAL + "\n\nKIẾN THỨC ỨNG DỤNG:\n- navigation.open_menu: tap tab Menu ở cuối thanh điều hướng"


def index_html() -> str:
    return INDEX.read_text(encoding="utf-8")


# ── Cảnh báo khi bỏ quên bản đã chuẩn bị ─────────────────────────────────
def test_running_without_the_prepared_prompt_warns():
    console_server.remember_prepared_prompt(GOAL, PREPARED)

    warning = console_server.forgotten_prepared_prompt(GOAL, "")

    assert "PROMPT GỐC" in warning
    assert str(len(PREPARED)) in warning
    assert "Dùng bản này" in warning


def test_running_with_the_prepared_prompt_is_silent():
    console_server.remember_prepared_prompt(GOAL, PREPARED)

    assert console_server.forgotten_prepared_prompt(GOAL, PREPARED) == ""


def test_nothing_is_prepared_so_there_is_nothing_to_warn_about():
    assert console_server.forgotten_prepared_prompt("câu chưa từng chuẩn bị", "") == ""


def test_the_goal_is_matched_ignoring_spacing_and_case():
    console_server.remember_prepared_prompt("  Đăng xuất   VÀ đăng nhập  ", PREPARED)

    assert console_server.forgotten_prepared_prompt("đăng xuất và đăng nhập", "") != ""


def test_a_prepared_prompt_equal_to_the_goal_is_not_remembered():
    """KB không ghép được gì thì bản "chuẩn bị" chính là câu gốc — không có gì để quên."""
    console_server.remember_prepared_prompt("câu gốc y nguyên", "câu gốc y nguyên")

    assert console_server.forgotten_prepared_prompt("câu gốc y nguyên", "") == ""


# ── Trạng thái phải nhìn thấy được ────────────────────────────────────────
def test_the_ui_shows_which_prompt_the_next_run_will_send():
    html = index_html()

    assert 'id="runPromptState"' in html
    assert "function updateRunPromptState" in html
    assert "prompt đã chuẩn bị" in html
    assert "prompt gốc" in html


def test_the_ui_keeps_the_approved_prompt_across_a_page_reload():
    """Cờ này từng chỉ nằm trong bộ nhớ trang: tải lại là mất, lượt sau lặng lẽ chạy câu gốc."""
    html = index_html()

    assert "testconsole.promptOverride" in html
    assert "function setPromptOverride" in html
    assert "persistPromptOverride" in html


def test_the_run_request_still_carries_the_override():
    html = index_html()

    assert "prompt_override: promptOverride" in html
