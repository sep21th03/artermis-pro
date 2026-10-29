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

"""Phase 1 tối ưu token: phần bất biến đứng trước, phần biến động về sau.

**Vì sao.** Provider chỉ cache được **tiền tố chung** giữa các lượt. Bản cũ để section
``## Current Plan & Execution History`` (đổi mỗi lượt) nằm giữa system message, nên tiền tố chung chỉ
tới đó là hết; đuôi template (chỉ dẫn Phase 2/3, bất biến) lại nằm **sau** ảnh + danh sách UI trong
message human nên không bao giờ cache được. Đo trên trace thật ``5b459634`` (2 lượt liên tiếp, số
provider báo qua ``countTokens``): tiền tố chung 5.311 token (55% prompt) → **7.372 token (76%)** sau
khi di chuyển khối. Không thêm/bớt một quy tắc nào — chỉ đổi vị trí.

**Điều các test dưới đây khoá lại:**

1. system message = **toàn bộ chỉ dẫn**, không phụ thuộc kế hoạch (⇒ giống nhau giữa mọi lượt);
2. kế hoạch + lịch sử nằm ở **đầu** message human, ảnh/UI vẫn còn nguyên và đứng sau;
3. mọi nhóm quy tắc của đuôi template vẫn hiện diện đúng một lần;
4. tổng ký tự không đổi (không thêm, không mất nội dung);
5. có công tắc để quay lại cách sắp xếp cũ.
"""

from types import SimpleNamespace

import pytest

from artemis.agents.operator.prompts import (
    PLAN_SECTION_HEADING,
    PromptBuilder,
    TemplatePromptComponent,
    load_operator_prompts,
    split_plan_section,
    stable_prefix_enabled,
)

#: Nhóm quy tắc nằm trong **đuôi** template (sau mốc ``# CURRENT OBSERVATION``) — bản cũ để chúng ở
#: cuối message human, nơi không thể cache; đây là danh sách không được phép mất.
TAIL_RULES = (
    "Loop Exit Evaluation",
    "Preserve Memory & Pre-Action Snapshotting",
    "Background Tasks & Instrumentation",
    "Phase 3: Target Selection & Physical Execution",
    "Target Resolution & Index Safety",
    "Action Selection",
)


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(execution_setup=None, actuator=None, data_engine=None)


async def _render(plan_and_history: str) -> tuple[str, list[str]]:
    """(system, các part human dạng chữ) của đường legacy."""
    builder = PromptBuilder()
    await TemplatePromptComponent()(
        builder,
        SimpleNamespace(initial_goal="GOAL"),
        _ctx(),
        prompts=load_operator_prompts(),
        template_name="main_template",
        plan_and_history=plan_and_history,
    )
    human = [p for p in builder.human_parts if isinstance(p, str)]
    if builder.human_footer:
        human.append(builder.human_footer)
    return "".join(builder.system_parts), human


# ── split_plan_section ──────────────────────────────────────────────────
def test_split_plan_section_extracts_the_volatile_block():
    text = f"# A\nrule a\n{PLAN_SECTION_HEADING}\nplan line\n# B\nrule b\n"

    static, plan = split_plan_section(text)

    assert PLAN_SECTION_HEADING in plan and "plan line" in plan
    assert "rule a" in static and "rule b" in static
    assert PLAN_SECTION_HEADING not in static and "plan line" not in static
    assert len(static) + len(plan) == len(text)  # không mất, không nhân bản ký tự nào


def test_split_plan_section_without_the_heading_changes_nothing():
    text = "# A\nrule a\n# B\nrule b\n"

    static, plan = split_plan_section(text)

    assert static == text
    assert plan == ""


def test_split_plan_section_at_the_end_of_the_text():
    text = f"# A\nrule a\n{PLAN_SECTION_HEADING}\nplan only\n"

    static, plan = split_plan_section(text)

    assert "plan only" in plan and "plan only" not in static
    assert "rule a" in static


# ── công tắc ────────────────────────────────────────────────────────────
def test_stable_prefix_is_on_by_default_and_can_be_switched_off(monkeypatch):
    monkeypatch.delenv("ARTEMIS_PROMPT_STABLE_PREFIX", raising=False)
    assert stable_prefix_enabled() is True

    monkeypatch.setenv("ARTEMIS_PROMPT_STABLE_PREFIX", "0")
    assert stable_prefix_enabled() is False


# ── sắp xếp prompt ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_system_message_carries_all_instructions_and_no_plan():
    system, human = await _render("PLAN_SENTINEL")

    assert "PLAN_SENTINEL" not in system
    assert PLAN_SECTION_HEADING in human[0]  # kế hoạch ở đầu message human
    for rule in TAIL_RULES:
        assert rule in system, rule


@pytest.mark.asyncio
async def test_two_turns_with_different_plans_share_the_whole_system_message():
    """Đây là tính chất làm nên cache: system giống nhau dù kế hoạch/lịch sử đã đổi."""
    first, _human_a = await _render("PLAN_A\n- [x] step")
    second, _human_b = await _render("PLAN_B\n- [ ] other step")

    assert first == second


@pytest.mark.asyncio
async def test_no_rule_is_lost_and_no_content_is_duplicated(monkeypatch):
    """Chuyển khối chỉ đổi vị trí: tổng ký tự bằng nhau, mỗi quy tắc đúng một lần."""
    system_on, human_on = await _render("PLAN_X")
    with_plan = system_on + "".join(human_on)

    assert sum(with_plan.count(rule) for rule in TAIL_RULES) == len(TAIL_RULES)
    assert with_plan.count(PLAN_SECTION_HEADING) == 1
    assert "PLAN_X" in with_plan

    monkeypatch.setenv("ARTEMIS_PROMPT_STABLE_PREFIX", "0")
    system_off, human_off = await _render("PLAN_X")
    without_plan = system_off + "".join(human_off)

    # Cùng nội dung, chỉ khác vị trí (chênh 1 ký tự nối "\n" khi ghép đuôi vào system).
    assert abs(len(with_plan) - len(without_plan)) <= 2
    for rule in TAIL_RULES:
        assert rule in without_plan  # bản cũ cũng có, chỉ khác vị trí


@pytest.mark.asyncio
async def test_switching_off_restores_the_previous_layout(monkeypatch):
    monkeypatch.setenv("ARTEMIS_PROMPT_STABLE_PREFIX", "0")

    system, human = await _render("PLAN_OLD")

    assert "PLAN_OLD" in system  # kế hoạch nằm trong system như trước
    assert "T+mm:ss" in human[-1]  # đuôi template nằm cuối message human như trước
