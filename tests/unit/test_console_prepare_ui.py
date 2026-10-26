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

"""Static checks on the console's "Chuẩn bị prompt" flow.

The UI is one hand-written HTML file, so a typo in an element id, an unregistered
API path or a status the UI cannot render only shows up when a tester clicks the
button.  These checks catch all three without a browser.

The flow itself is a contract: *tester types → server prepares → tester reviews
and edits → tester runs*.  Each arrow is asserted here, because a break in any of
them silently sends the agent a prompt nobody approved.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

console_server = importlib.import_module("apps.test_console.server")

INDEX = Path("apps/test_console/static/index.html")


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(html: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "console phải có ít nhất một block <script>"
    return "\n".join(blocks)


def test_the_prepare_button_exists(html):
    assert 'id="btnPreparePrompt"' in html


def test_the_prepare_again_button_exists(html):
    assert 'id="btnReprepare"' in html


def test_there_is_a_place_to_show_the_prepare_status(html):
    assert 'id="prepareStatus"' in html


def test_every_element_id_the_script_uses_exists(html, script):
    declared = set(re.findall(r'id="([^"]+)"', html))
    referenced = set(re.findall(r"\$\(\'([^\']+)\'\)", script))

    assert not (referenced - declared), "id không tồn tại trong HTML: " + ", ".join(
        sorted(referenced - declared)
    )


def test_no_duplicate_element_ids(html):
    ids = re.findall(r'id="([^"]+)"', html)
    duplicates = {value for value in ids if ids.count(value) > 1}

    assert not duplicates, "id bị trùng: " + ", ".join(sorted(duplicates))


def test_the_ui_calls_the_prepare_endpoint(script):
    assert "/api/knowledge/prepare" in script


def test_the_prepare_endpoint_exists_on_this_app():
    """Nút bấm phải có route thật: ở vai trò mặc định (``all``) nó tồn tại."""
    assert "/api/knowledge/prepare" in set(console_server.app.openapi()["paths"])


def test_the_prepare_request_carries_the_prompt_package_and_profile(script):
    call = script.split("/api/knowledge/prepare", 1)[1].split("});", 1)[0]

    assert "prompt:" in call
    assert "app_package" in call
    assert "profile" in call


def test_the_prepare_button_and_the_again_button_are_wired(script):
    assert "$('btnPreparePrompt').onclick" in script
    assert "$('btnReprepare').onclick" in script


def test_the_refresh_button_asks_for_a_fresh_synthesis(script):
    assert "preparePrompt(true)" in script
    assert "preparePrompt(false)" in script


def test_the_enhanced_prompt_lands_in_the_editable_box(script):
    """Tester phải xem/sửa được bản đã tổng hợp trước khi chạy."""
    assert "$('promptPreview').value = r.enhanced_prompt" in script


def test_the_approved_prompt_is_what_gets_sent_to_run(script):
    assert "prompt_override: promptOverride" in script


def test_every_prepare_status_has_a_label(script):
    for status in ("llm", "fallback", "no_knowledge", "degraded"):
        assert re.search(rf"\b{status}:", script), f"thiếu nhãn cho trạng thái {status}"


def test_the_status_line_shows_the_source_and_the_workflows(script):
    body = script.split("function describePrepare", 1)[1].split("\n}", 1)[0]

    assert "matched_workflows" in body
    assert "kb_version" in body
    assert "cached" in body
    assert "checks" in body
    assert "fallback_reason" in body


def test_the_status_line_separates_primary_from_supporting(script):
    """Tester phải phân biệt được workflow thực hiện với workflow chỉ là bối cảnh."""
    body = script.split("function describePrepare", 1)[1].split("\n}", 1)[0]

    assert "role === 'primary'" in body
    assert "role === 'supporting'" in body
    assert "excluded_workflows" in body


def test_the_status_line_shows_the_tasks_and_the_gaps(script):
    body = script.split("function describePrepare", 1)[1].split("\n}", 1)[0]

    assert "r.tasks" in body
    assert "knowledge_gaps" in body


def test_the_status_line_reports_llm_usage_and_cache(script):
    body = script.split("function describePrepare", 1)[1].split("\n}", 1)[0]

    assert "metrics" in body
    assert "llm_calls" in body



def test_an_edge_machine_gets_an_actionable_message(script):
    """Vai trò edge không có route: phải chỉ đúng máy chủ, không phải lỗi mơ hồ."""
    body = script.split("function prepareErrorHint", 1)[1].split("\n}", 1)[0]

    assert "kbApi" in body
    assert "api/knowledge/prepare" in body


def test_editing_the_goal_clears_a_stale_prepare_status(script):
    handler = script.split("$('goal').addEventListener('input'", 1)[1].split("});", 1)[0]

    assert "$('prepareStatus').textContent = ''" in handler


def _find_node() -> str | None:
    """``node`` may only exist inside nvm, which is not on the PATH here."""
    import glob
    import os
    import shutil

    found = shutil.which("node")
    if found:
        return found
    for pattern in (
        os.path.expanduser("~/.nvm/versions/node/*/bin/node"),
        "/usr/local/bin/node",
        "/opt/homebrew/bin/node",
    ):
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return None


def test_the_script_parses_as_javascript(script, tmp_path):
    """Catches a stray brace, which would otherwise break the whole page."""
    import subprocess

    node = _find_node()
    if node is None:
        pytest.skip("node không có trên máy này")
    path = tmp_path / "console.js"
    path.write_text(script, encoding="utf-8")

    result = subprocess.run(
        [node, "--check", str(path)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
