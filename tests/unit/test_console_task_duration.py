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

"""Giới hạn thời gian chạy task: UI cho chọn 8/10/15/20 phút, và **trần phía máy chủ phải đủ lớn**.

Vì sao phải khoá bằng test: lựa chọn trên UI là vô nghĩa nếu watchdog cắt task ở trần cũ. Trước đây
UI tối đa 8 phút và ``BUDGET_MAX_TASK_SECONDS`` đúng bằng 480s, nên khi thêm 10/15/20 phút mà quên
nâng trần thì task vẫn chết ở 8 phút mà không ai hiểu vì sao.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

console_server = importlib.import_module("apps.test_console.server")

MAX_CHOICE_SECONDS = 1200  # 20 phút


def test_the_ui_offers_eight_ten_fifteen_and_twenty_minutes():
    http = TestClient(console_server.app)
    html = http.get("/").text

    for value in ("480", "600", "900", "1200"):
        assert f'<option value="{value}"' in html, f"thiếu lựa chọn {value}s"
    for label in ("8 phút", "10 phút", "15 phút", "20 phút"):
        assert label in html
    # Lựa chọn cũ (2/5 phút) không còn — preset nào trỏ vào giá trị không tồn tại sẽ đặt select về rỗng.
    assert "2 phút (khuyến nghị cho Flash)" not in html


def test_preset_limits_are_values_that_exist_in_the_select():
    """Preset gán thẳng ``$('timeLimit').value``; giá trị không có trong <select> ⇒ ``maxSeconds = NaN``."""
    html = TestClient(console_server.app).get("/").text

    assert "p.profile === 'pro' ? '900' : '480'" in html


def test_the_server_ceiling_allows_the_largest_ui_choice(monkeypatch):
    monkeypatch.delenv("BUDGET_MAX_TASK_SECONDS", raising=False)
    monkeypatch.delenv("TASK_IDLE_TIMEOUT_SECONDS", raising=False)

    budget = console_server.Budget()

    assert budget.max_task_seconds >= MAX_CHOICE_SECONDS
    # Task 20 phút có những bước dài; 120s im lặng bị coi là treo là cắt oan.
    assert console_server.IDLE_TIMEOUT_SECONDS >= 240


def test_a_twenty_minute_request_is_accepted(monkeypatch):
    req = console_server.RunRequest(goal="đăng nhập tài khoản 0999", max_seconds=MAX_CHOICE_SECONDS)
    assert req.max_seconds == MAX_CHOICE_SECONDS

    with pytest.raises(Exception):
        console_server.RunRequest(goal="đăng nhập tài khoản 0999", max_seconds=7200)
