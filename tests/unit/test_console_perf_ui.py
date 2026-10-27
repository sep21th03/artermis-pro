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

"""Tab Performance trong giao diện console — kiểm tra tĩnh trên ``index.html``.

Không có trình duyệt trong test, nên ở đây khoá những thứ nếu thiếu thì tab sẽ hỏng im
lặng: nút tab, hai bảng theo đúng yêu cầu, dòng N/A cho số đo thiếu, phần nhận định
bottleneck, và nút xuất JSON.
"""

from __future__ import annotations

import importlib
from pathlib import Path

console_server = importlib.import_module("apps.test_console.server")

INDEX = Path(console_server.__file__).parent / "static" / "index.html"


def html() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_the_performance_tab_exists_and_has_a_pane():
    page = html()

    assert 'data-tab="perf"' in page
    assert 'id="perfPane"' in page


def test_the_tab_is_wired_into_set_tab():
    page = html()

    assert "$('perfPane').style.display = tab === 'perf'" in page
    assert "startPerfPolling()" in page
    assert "stopPerfPolling()" in page


def test_the_stage_table_has_the_required_columns():
    page = html()

    for header in ("Stage", "Calls", "Total", "Avg", "Max", "Errors", "% Runtime"):
        assert f"<th class=\"num\">{header}</th>" in page or f"<th>{header}</th>" in page


def test_the_timeline_table_has_the_required_columns():
    page = html()

    for header in ("Step", "Stage", "Action", "Duration", "Model", "Tokens", "Result"):
        assert f"<th>{header}</th>" in page or f"<th class=\"num\">{header}</th>" in page


def test_missing_measurements_render_as_na_not_zero():
    page = html()

    assert "return 'N/A';" in page
    # Token thiếu phải là gạch, không phải 0.
    assert "row.total_tokens === null" in page


def test_each_timeline_row_can_be_expanded_with_the_drilldown_fields():
    page = html()

    for field in ("Request ID", "Retry", "HTTP", "TTFT", "UI:", "Chờ trước span", "Span:"):
        assert field in page
    assert "perf-detail" in page


def test_the_findings_block_renders_evidence_lines():
    page = html()

    assert "renderPerfFindings" in page
    assert "perf-finding" in page
    assert "f.evidence" in page


def test_the_export_button_opens_the_json_report():
    page = html()

    assert "btnPerfExport" in page
    assert "/performance.json" in page


def test_the_ui_polls_the_performance_endpoint_while_the_tab_is_open():
    page = html()

    assert "/performance" in page
    assert "setInterval(loadPerf, 3000)" in page


def test_the_empty_state_explains_that_data_appears_while_running():
    page = html()

    assert 'id="perfEmpty"' in page
    assert "Số liệu xuất hiện ngay khi agent" in page
