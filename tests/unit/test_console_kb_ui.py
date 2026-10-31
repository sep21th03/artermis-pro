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

"""Static checks on the console's Knowledge tab.

The UI is a single hand-written HTML file, so a typo in an element id or an API
path only shows up when a tester clicks the button.  These checks catch both
without a browser: every id the script reaches for must exist, and every
``/api/kb/...`` path it calls must correspond to a route that is actually
registered.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from artemis.knowledge.api import router

INDEX = Path("apps/test_console/static/index.html")


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(html: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "console phải có ít nhất một block <script>"
    return "\n".join(blocks)


def test_the_knowledge_tab_button_exists(html):
    assert 'data-tab="kb"' in html


def test_the_knowledge_pane_exists_and_starts_hidden(html):
    assert re.search(r'id="kbPane"[^>]*style="display:none"', html)


def test_set_tab_toggles_the_knowledge_pane(script):
    assert "$('kbPane').style.display = tab === 'kb'" in script


def test_every_element_id_the_script_uses_exists(html, script):
    declared = set(re.findall(r'id="([^"]+)"', html))
    referenced = set(re.findall(r"\$\('([^']+)'\)", script))
    assert not (referenced - declared), "id không tồn tại trong HTML: " + ", ".join(
        sorted(referenced - declared)
    )


def test_no_duplicate_element_ids(html):
    ids = re.findall(r'id="([^"]+)"', html)
    duplicates = {value for value in ids if ids.count(value) > 1}
    assert not duplicates, "id bị trùng: " + ", ".join(sorted(duplicates))


def test_every_api_path_the_script_calls_is_registered(script):
    """A renamed route must not leave a dead button behind."""
    routes = [route.path for route in router.routes]
    # Path templates -> a form we can prefix-match against the literal JS text.
    normalised = [re.sub(r"\{[^}]+\}", "*", path) for path in routes]

    called = set(re.findall(r"['\"](/api/kb/[A-Za-z0-9_/-]*)", script))
    assert called, "script phải gọi ít nhất một endpoint /api/kb"

    unknown = []
    for path in called:
        probe = path.rstrip("/")
        # `/api/kb/apps/` + id + `/versions` shows up as the literal prefix
        # `/api/kb/apps/`; accept a prefix of a real route.
        if any(route.rstrip("/").startswith(probe) for route in normalised):
            continue
        unknown.append(path)
    assert not unknown, "đường dẫn API không có route: " + ", ".join(sorted(unknown))


def _find_node() -> str | None:
    """``node`` may only exist inside nvm, which is not on the PATH here."""
    import glob
    import os
    import shutil

    found = shutil.which("node")
    if found:
        return found
    patterns = [
        os.path.expanduser("~/.nvm/versions/node/*/bin/node"),
        "/usr/local/bin/node",
        "/opt/homebrew/bin/node",
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return None


def test_the_script_parses_as_javascript(script, tmp_path):
    """Catches a stray brace, which would otherwise break the whole page."""
    import subprocess

    node = _find_node()
    if node is None:
        pytest.skip("node không có trên PATH")
    path = tmp_path / "console.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


# ── the tab covers what the feature is for ───────────────────────────────
@pytest.mark.parametrize(
    "needle",
    [
        "kbAliases",  # quản lý alias
        "kbPre",  # điều kiện trước
        "kbSteps",  # các bước
        "kbCrit",  # tiêu chí thành công
        "kbStatusSel",  # draft / verified / deprecated
        "kbSearch",  # tìm kiếm
        "btnKbTest",  # kiểm tra truy xuất
        "btnKbLogs",  # xem workflow nào đã được dùng
        "btnKbReindex",  # trạng thái embedding
    ],
)
def test_the_tab_covers_the_required_features(html, needle):
    assert needle in html


def test_the_ui_says_steps_are_intent_not_coordinates(html):
    """The one instruction a tester must not get wrong."""
    assert "không</b> phải toạ độ" in html or "không</b> phải toạ độ" in html.replace("\n", " ")


def test_a_tab_can_be_opened_from_the_url(script):
    """``/?tab=kb`` — so the Knowledge tab is bookmarkable (and headless-testable)."""
    assert "URLSearchParams(location.search).get('tab')" in script
    assert "setTab(wantedTab || 'steps')" in script


def test_the_ui_offers_exactly_the_actions_the_backend_accepts(script):
    """A mismatch would let the UI send an action the API rejects with a 422."""
    from artemis.knowledge.models import STEP_ACTIONS

    block = re.search(r"const KB_ACTIONS = \[(.*?)\];", script, re.S)
    assert block, "không tìm thấy KB_ACTIONS trong script"
    offered = set(re.findall(r"\['([a-z_]+)'", block.group(1)))
    assert offered == set(STEP_ACTIONS), (
        "UI và backend lệch nhau — chỉ có ở UI: "
        + str(sorted(offered - set(STEP_ACTIONS)))
        + ", chỉ có ở backend: "
        + str(sorted(set(STEP_ACTIONS) - offered))
    )


def test_the_status_options_match_the_backend(html):
    from artemis.knowledge.models import WORKFLOW_STATUSES

    block = re.search(r'id="kbStatusSel">(.*?)</select>', html, re.S)
    assert block, "không tìm thấy select kbStatusSel"
    offered = set(re.findall(r'<option value="([a-z]+)"', block.group(1)))
    assert offered == set(WORKFLOW_STATUSES)


# ── Regression: không được tham chiếu hàm khai báo ở <script> khác ───────────────────────
#
# Lỗi thật 30/10/2026: phần gắn sự kiện đăng nhập KB được chèn vào <script> thứ nhất trong khi
# `kbLogin`/`kbLogout` khai báo ở <script> thứ hai ⇒ ReferenceError ngay khi trang tải ⇒ script thứ
# nhất chết giữa đường ⇒ **mọi tab của console không bấm được**. Test này khoá lại: helper dùng trong
# `api()` phải nằm cùng script, và phần gắn sự kiện phải nằm sau định nghĩa hàm, cùng script.

SCRIPT_RE = re.compile(r"<script[^>]*>(?P<body>.*?)</script>", re.DOTALL)


def _script_blocks(html: str) -> list[str]:
    return [m.group("body") for m in SCRIPT_RE.finditer(html)]


def test_kb_token_helpers_live_in_the_same_script_as_api(html):
    blocks = _script_blocks(html)
    api_block = next((b for b in blocks if "async function api(" in b), None)

    assert api_block is not None
    for name in ("function kbToken(", "function kbSetToken(", "function kbAuthHint("):
        assert name in api_block, f"{name} phải ở cùng <script> với api() — nếu không, api() ném ReferenceError"


def test_kb_login_wiring_happens_after_the_functions_in_the_same_script(html):
    blocks = _script_blocks(html)
    login_block = next((b for b in blocks if "async function kbLogin(" in b), None)

    assert login_block is not None
    assert "function kbAuthInit(" in login_block
    assert "kbLoginBtn" in login_block.split("function kbAuthInit(", 1)[1], (
        "phần gắn sự kiện phải nằm trong kbAuthInit của chính script này"
    )
    # Không được gắn sự kiện ở top-level của bất kỳ script nào (DOM có thể chưa có phần tử).
    for block in blocks:
        for line in block.splitlines():
            if line[:1] in (" ", "\t", "") or "$" not in line:
                continue
            assert ".onclick" not in line, f"gắn sự kiện ở top-level rất dễ chết: {line.strip()}"


def test_kb_auth_init_is_called_at_boot(html):
    assert re.search(r"^kbAuthInit\(\);\s*$", html, re.MULTILINE), (
        "kbAuthInit() phải được gọi ở khối boot cuối file"
    )
