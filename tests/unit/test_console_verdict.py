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

"""How the console decides ĐẠT / KHÔNG ĐẠT / CHƯA XÁC MINH.

The failure this guards against was reported from a real run: the agent called
``report_task_status(status="completed")`` while the logout had not happened, and
the console showed **ĐẠT** because it trusted that self-report.  A verdict must
rest on evidence (the Checker's check results), never on the agent's word.
"""

from __future__ import annotations

import importlib

console = importlib.import_module("apps.test_console.server")

#: The exact line ``exit_settlement`` writes into a trace.
REAL_LINE = (
    "ℹ Run outcome: task_status=partial, tests(passed=0, failed=0,"
    " inconclusive=1, unchecked=0, retired=0)"
)


# ── reading the evidence ─────────────────────────────────────────────────
def test_parses_the_line_exit_settlement_writes():
    outcome = console.parse_run_outcome(REAL_LINE)
    assert outcome == {
        "task_status": "partial",
        "passed": 0,
        "failed": 0,
        "inconclusive": 1,
        "unchecked": 0,
    }


def test_parses_a_clean_pass():
    outcome = console.parse_run_outcome(
        "Run outcome: task_status=completed, tests(passed=2, failed=0,"
        " inconclusive=0, unchecked=0, retired=0)"
    )
    assert outcome["passed"] == 2 and outcome["task_status"] == "completed"


def test_parses_a_verified_failure():
    outcome = console.parse_run_outcome(
        "Run outcome: task_status=completed, tests(passed=1, failed=1,"
        " inconclusive=0, unchecked=0, retired=0)"
    )
    assert outcome["failed"] == 1


def test_a_trace_without_the_line_has_no_evidence():
    """Profile ``flash`` runs FlashRunner and never reaches exit settlement."""
    assert console.parse_run_outcome("ℹ ✅ Automation 'x' is success ✅") is None
    assert console.parse_run_outcome("") is None


# ── the verdict ──────────────────────────────────────────────────────────
def test_no_evidence_is_unverified_not_pass():
    """The reported bug: a self-declared completion was shown as ĐẠT."""
    verdict, conclusion, issues = console.verified_verdict(None)
    assert verdict == "unverified"
    assert "CHƯA XÁC MINH" in conclusion
    assert "flash" in conclusion
    assert issues, "phải nói rõ vì sao chưa xác minh"


def test_verified_pass_requires_check_items():
    verdict, conclusion, issues = console.verified_verdict(
        {"task_status": "completed", "passed": 2, "failed": 0, "inconclusive": 0, "unchecked": 0}
    )
    assert verdict == "pass"
    assert "2 tiêu chí" in conclusion
    assert issues == []


def test_a_failed_check_is_a_failure():
    verdict, conclusion, _ = console.verified_verdict(
        {"task_status": "completed", "passed": 1, "failed": 1, "inconclusive": 0, "unchecked": 0}
    )
    assert verdict == "fail"
    assert "KHÔNG ĐẠT" in conclusion


def test_an_inconclusive_check_is_not_a_pass():
    verdict, conclusion, _ = console.verified_verdict(
        {"task_status": "partial", "passed": 0, "failed": 0, "inconclusive": 1, "unchecked": 0}
    )
    assert verdict == "unverified"
    assert "chưa rõ" in conclusion


def test_a_partial_pass_with_an_inconclusive_item_is_not_a_clean_pass():
    """One verified item must not paper over another the Checker could not judge."""
    verdict, _, _ = console.verified_verdict(
        {"task_status": "completed", "passed": 2, "failed": 0, "inconclusive": 1, "unchecked": 0}
    )
    assert verdict == "unverified"


def test_no_check_items_at_all_is_unverified():
    verdict, conclusion, _ = console.verified_verdict(
        {"task_status": "completed", "passed": 0, "failed": 0, "inconclusive": 0, "unchecked": 0}
    )
    assert verdict == "unverified"
    assert "không khai tiêu chí" in conclusion


def test_a_failure_outranks_an_inconclusive_item():
    """With one proven failure the run is failed, whatever else is unclear."""
    verdict, _, _ = console.verified_verdict(
        {"task_status": "partial", "passed": 1, "failed": 1, "inconclusive": 1, "unchecked": 0}
    )
    assert verdict == "fail"


def test_every_verdict_the_backend_can_return_has_a_title_in_the_ui():
    """A missing label would render the raw string in the result card."""
    import pathlib
    import re

    html = pathlib.Path("apps/test_console/static/index.html").read_text(encoding="utf-8")
    for verdict in ("pass", "fail", "stopped", "running", "unverified"):
        assert f"{verdict}:" in html, f"UI thiếu nhãn cho verdict {verdict!r}"
    assert ".result.unverified{" in html
