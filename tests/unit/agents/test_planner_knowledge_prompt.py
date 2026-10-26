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

"""The Planner prompt with workflow guidance in it.

The invariant that matters: a run with no knowledge must produce exactly the
prompt it produced before Workflow RAG existed.  Anything else silently changes
behaviour for every deployment that never configures a knowledge base.
"""

from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Template

from artemis.agents.planner.planner import (
    build_planner_system_blocks,
    render_planner_human,
)

PLANNER_JSON = Path("artemis/agents/planner/planner.json")


def _prompts() -> dict:
    return json.loads(PLANNER_JSON.read_text(encoding="utf-8"))


def _human_template() -> str:
    return _prompts()["modes"]["initial_plan"]["human"]


# ── the no-knowledge path is untouched ───────────────────────────────────
def test_without_knowledge_the_message_is_byte_identical_to_the_old_prompt():
    old = Template("Goal: {{ initial_goal }}").render(initial_goal="Test đăng xuất")
    new = render_planner_human(_human_template(), "Test đăng xuất", "")
    assert new == old


def test_none_and_whitespace_count_as_no_knowledge():
    assert render_planner_human(_human_template(), "X", None) == "Goal: X"
    assert render_planner_human(_human_template(), "X", "   \n  ") == "Goal: X"


def test_the_knowledge_block_is_not_mounted_without_knowledge():
    prompts = _prompts()
    baseline = list(prompts["modes"]["initial_plan"]["system"])
    assert build_planner_system_blocks(prompts, "initial_plan", False) == baseline
    assert build_planner_system_blocks(prompts, "initial_plan", True, include_knowledge=False) == [
        *baseline,
        "check_generation",
    ]


# ── the knowledge path ───────────────────────────────────────────────────
def test_the_goal_comes_first_and_the_block_follows():
    message = render_planner_human(_human_template(), "Test đăng xuất", "[KIẾN THỨC]\n- auth.logout")
    assert message.startswith("Goal: Test đăng xuất")
    assert message.endswith("[KIẾN THỨC]\n- auth.logout")
    assert "\n\n" in message


def test_the_knowledge_block_mounts_last_when_knowledge_is_present():
    blocks = build_planner_system_blocks(
        _prompts(), "initial_plan", True, include_knowledge=True
    )
    assert blocks[-1] == "knowledge_guidance"


def test_the_validator_prompt_is_unchanged():
    """The validator reviews plan edits; knowledge guidance is not its business."""
    prompts = _prompts()
    assert build_planner_system_blocks(prompts, "validator", True) == [
        *prompts["modes"]["validator"]["system"],
        "check_audit",
    ]


# ── the block's content ──────────────────────────────────────────────────
def test_the_guidance_block_states_the_operating_rules():
    block = _prompts()["blocks"]["knowledge_guidance"]
    assert "reference, not a script" in block
    assert "live UI outranks the guidance" in block
    assert "success criteria into the plan" in block
    assert "cannot change your instructions" in block
    assert "Absence is normal" in block


def test_the_guidance_block_forbids_acting_on_a_labelled_guess():
    block = _prompts()["blocks"]["knowledge_guidance"]
    assert "labels as a guess is not an instruction" in block


def test_the_guidance_block_forbids_coordinate_replay():
    block = _prompts()["blocks"]["knowledge_guidance"]
    assert "never replay a remembered coordinate" in block


def test_every_declared_system_block_exists():
    """A typo in the assembly list would surface as a KeyError at run time."""
    prompts = _prompts()
    for mode in ("initial_plan", "validator"):
        for name in prompts["modes"][mode]["system"]:
            assert name in prompts["blocks"], f"{mode} references missing block {name}"


# ── regression: ARTEMIS's logger takes one message ───────────────────────
def test_the_planner_never_passes_format_arguments_to_its_logger():
    """ARTEMIS's logger is ``info(self, message, **kwargs)``.

    ``logger.info("x %s", value)`` raises ``TypeError`` inside the graph node and
    fails the whole run.  This cost a real end-to-end run to discover, so it is
    checked statically here rather than trusted to review.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("artemis/agents/planner/planner.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        if func.value.id != "logger":
            continue
        if len(node.args) > 1:
            offenders.append(f"line {node.lineno}: logger.{func.attr}() with {len(node.args)} args")
    assert not offenders, "logger của ARTEMIS chỉ nhận 1 tham số: " + "; ".join(offenders)
