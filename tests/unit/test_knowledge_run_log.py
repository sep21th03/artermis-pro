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

"""What a run prints about the knowledge it did or did not get.

The ``flash`` profile runs ``FlashRunner``, which has no Planner -- so a
retrieved workflow has nowhere to be consumed.  A tester who reads "RAG: chèn
kiến thức" without that caveat would reasonably believe the guidance was used.
"""

from __future__ import annotations

from artemis.knowledge.integration import KnowledgeContext
from third_party.mobile_use.main import _log_knowledge


def _capture(capsys) -> str:
    return capsys.readouterr().out


def test_flash_profile_is_told_the_guidance_will_not_be_used(capsys):
    knowledge = KnowledgeContext(text="BLOCK", found=True, trusted_ids=["auth.logout"], app_id="hunonic")
    _log_knowledge(knowledge, "flash")
    out = _capture(capsys)
    assert "workflow auth.logout" in out
    assert "KHÔNG có Planner" in out
    assert "--profile pro" in out


def test_a_planner_profile_gets_no_warning(capsys):
    knowledge = KnowledgeContext(text="BLOCK", found=True, trusted_ids=["auth.logout"], app_id="hunonic")
    _log_knowledge(knowledge, "pro")
    out = _capture(capsys)
    assert "chèn kiến thức" in out
    assert "KHÔNG có Planner" not in out


def test_the_profile_comparison_is_case_insensitive(capsys):
    knowledge = KnowledgeContext(text="BLOCK", found=True, app_id="hunonic")
    _log_knowledge(knowledge, "FLASH")
    assert "KHÔNG có Planner" in _capture(capsys)


def test_nothing_injected_reports_the_reason(capsys):
    knowledge = KnowledgeContext(found=False, reason="App chưa có trong knowledge base")
    _log_knowledge(knowledge, "pro")
    out = _capture(capsys)
    assert "không chèn kiến thức" in out
    assert "App chưa có trong knowledge base" in out


def test_nothing_injected_and_no_reason_still_prints_something(capsys):
    _log_knowledge(KnowledgeContext(), "pro")
    assert "không có workflow phù hợp" in _capture(capsys)


def test_a_guess_only_run_is_reported_as_not_injected(capsys):
    """found=False means not injected, so the line must not claim otherwise."""
    knowledge = KnowledgeContext(
        text="[CÓ THỂ LIÊN QUAN] ...", found=False, trusted_ids=[], reason="chưa đủ tin cậy"
    )
    _log_knowledge(knowledge, "pro")
    out = _capture(capsys)
    assert "không chèn kiến thức" in out
    assert "chèn kiến thức từ" not in out


# ── a block fetched from a console elsewhere ─────────────────────────────
def test_a_remote_block_says_where_it_came_from(capsys):
    """Two machines can hold the knowledge; the log must not blur which one."""
    knowledge = KnowledgeContext(
        text="BLOCK",
        found=True,
        trusted_ids=["auth.logout"],
        app_id="hunonic",
        source="console",
    )
    _log_knowledge(knowledge, "pro")
    out = _capture(capsys)
    assert "console ở xa" in out
    assert "workflow auth.logout" in out


def test_a_local_block_still_says_database(capsys):
    knowledge = KnowledgeContext(
        text="BLOCK", found=True, trusted_ids=["auth.logout"], app_id="hunonic"
    )
    _log_knowledge(knowledge, "pro")
    assert "từ database" in _capture(capsys)


def test_a_block_with_neither_half_is_still_honest(capsys):
    knowledge = KnowledgeContext(text="BLOCK", found=True, source="console")
    _log_knowledge(knowledge, "pro")
    assert "máy đoán" in _capture(capsys)
