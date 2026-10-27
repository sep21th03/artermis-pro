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

"""Hiểu mục tiêu trước khi truy xuất — và phân loại vai trò của mỗi workflow.

Đây là tầng sửa đúng nguyên nhân gốc của lỗi đã gặp: ``mở app và đăng nhập rồi chuyển
sang nhà dled`` bị coi là một truy vấn, nên mọi workflow trùng từ đều được nối vào
prompt như nhau.  Test ở đây khoá bốn tính chất:

1. **Mỗi việc phải bám vào câu của tester** — không tự thêm "đăng xuất".
2. **Định danh được giữ** — ``dled``, số điện thoại không bị đổi.
3. **Vai trò phân biệt được mục tiêu với bối cảnh** — một luật/modal không bao giờ là
   việc chính, và workflow chỉ trùng từ bị loại.
4. **Không bao giờ raise** — LLM hỏng thì lùi về kế hoạch một việc.
"""

from __future__ import annotations

import json

import pytest

from artemis.knowledge.intent import (
    ROLE_IRRELEVANT,
    ROLE_PRIMARY,
    ROLE_SUPPORTING,
    SECTION_CONTEXT,
    SECTION_CRITERIA,
    SECTION_GAP,
    SECTION_KNOWLEDGE,
    SECTION_PLAN,
    IntentPlan,
    TaskSpec,
    analyze_intent,
    best_role,
    build_deterministic_prompt,
    build_knowledge_brief,
    build_task_knowledge,
    classify_task,
    _drop_contained_tasks,
    deterministic_plan,
    extract_json,
    is_navigation_workflow,
    is_rule,
    plan_queries,
    render_navigation_knowledge,
)
from artemis.knowledge.models import Workflow, WorkflowHit

PROMPT = "mở app và đăng nhập rồi chuyển sang nhà dled"
PLAN_JSON = {
    "tasks": [
        {"action": "mở app", "condition": "chỉ khi app chưa mở", "required": True},
        {"action": "đăng nhập", "condition": "chỉ khi chưa đăng nhập", "required": True},
        {"action": "chuyển nhà", "target": "dled", "required": True},
    ],
    "final_goal": {"type": "nhà hiện tại", "expected_value": "dled"},
}


def workflow(
    workflow_id: str,
    name: str,
    description: str = "",
    *,
    aliases=(),
    preconditions=(),
    success_criteria=(),
    tags=(),
    steps=(),
    status: str = "verified",
) -> Workflow:
    return Workflow(
        id=workflow_id,
        app_id="hunonic",
        name=name,
        description=description,
        aliases=list(aliases),
        preconditions=list(preconditions),
        success_criteria=list(success_criteria),
        tags=list(tags),
        steps=[{"action": "tap", "target": target} for target in steps],
        status=status,
        revision_no=1,
        updated_at="2026-10-26T10:00:00",
    )


def hit(workflow_object: Workflow, *, trusted: bool = True, rank: int = 1) -> WorkflowHit:
    return WorkflowHit(
        workflow=workflow_object, score=0.5, channels={"keyword": rank}, trusted=trusted
    )


def llm(payload: object, *, raw: str | None = None):
    def call(messages, *, max_tokens, timeout):
        return raw if raw is not None else json.dumps(payload, ensure_ascii=False)

    return call


# ── Phân tích mục tiêu ───────────────────────────────────────────────────
def test_three_tasks_are_extracted_in_order():
    plan = analyze_intent(PROMPT, synthesizer=llm(PLAN_JSON))

    assert plan.source == "llm"
    assert [task.action for task in plan.tasks] == ["mở app", "đăng nhập", "chuyển nhà"]
    assert plan.tasks[-1].target == "dled"
    assert plan.tasks[1].condition == "chỉ khi chưa đăng nhập"
    assert plan.final_goal["expected_value"] == "dled"


def test_the_primary_task_is_the_last_one():
    plan = analyze_intent(PROMPT, synthesizer=llm(PLAN_JSON))

    assert plan.primary_task is not None
    assert plan.primary_task.action == "chuyển nhà"


def test_a_task_without_grounding_in_the_prompt_is_dropped():
    """Không tự thêm thao tác: 'đăng xuất' không có trong câu thì không thành việc."""
    payload = {
        "tasks": [
            {"action": "mở app"},
            {"action": "đăng nhập", "condition": "chỉ khi chưa đăng nhập"},
            {"action": "chuyển nhà", "target": "dled"},
            {"action": "đăng xuất"},
        ]
    }

    plan = analyze_intent(PROMPT, synthesizer=llm(payload))

    assert plan.source == "llm"
    assert [task.action for task in plan.tasks] == ["mở app", "đăng nhập", "chuyển nhà"]


def test_a_plan_that_keeps_the_wrong_identifier_falls_back():
    prompt = "đăng nhập tài khoản 0972842548 rồi kiểm tra thông báo"
    payload = {"tasks": [{"action": "đăng nhập", "target": "0123456789"}]}

    plan = analyze_intent(prompt, synthesizer=llm(payload))

    assert plan.source == "deterministic"
    assert "định danh" in plan.reason


def test_a_plan_that_drops_too_much_of_the_prompt_falls_back():
    payload = {"tasks": [{"action": "mở app"}]}

    plan = analyze_intent(PROMPT, synthesizer=llm(payload))

    assert plan.source == "deterministic"


def test_more_than_four_tasks_are_truncated():
    payload = {
        "tasks": [
            {"action": "mở app"},
            {"action": "đăng nhập"},
            {"action": "chuyển nhà", "target": "dled"},
            {"action": "đổi mật khẩu"},
            {"action": "xoá tài khoản"},
        ]
    }

    plan = analyze_intent(PROMPT, synthesizer=llm(payload), max_tasks=3)

    assert len(plan.tasks) == 3


@pytest.mark.parametrize(
    "raw",
    [
        "xin chào, tôi không trả JSON",
        "{không phải json}",
        '```json\n{"tasks": []}\n```',
    ],
)
def test_unusable_answers_fall_back_to_one_task(raw):
    plan = analyze_intent(PROMPT, synthesizer=llm({}, raw=raw))

    assert plan.source == "deterministic"
    assert len(plan.tasks) == 1
    assert plan.tasks[0].action == PROMPT


def test_a_broken_llm_falls_back_instead_of_raising():
    def boom(messages, *, max_tokens, timeout):
        raise RuntimeError("proxy 502")

    plan = analyze_intent(PROMPT, synthesizer=boom)

    assert plan.source == "deterministic"
    assert "502" in plan.reason


def test_without_an_llm_the_whole_sentence_is_one_task():
    plan = analyze_intent(PROMPT, synthesizer=None)

    assert plan.source == "deterministic"
    assert plan.tasks[0].query() == PROMPT


def test_extract_json_accepts_fences_and_surrounding_prose():
    assert extract_json('Đây là kết quả:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('{"a": 1} cảm ơn') == {"a": 1}
    assert extract_json("không có gì") is None


def test_queries_are_one_per_task_and_deduplicated():
    plan = IntentPlan(
        tasks=[
            TaskSpec(action="đăng nhập"),
            TaskSpec(action="đăng nhập"),
            TaskSpec(action="chuyển nhà", target="dled"),
        ]
    )

    assert plan_queries(plan) == ["đăng nhập", "chuyển nhà dled"]


def test_deterministic_plan_keeps_the_sentence_verbatim():
    plan = deterministic_plan("  mở   app và đăng nhập  ")

    assert plan.tasks[0].action == "mở app và đăng nhập"


# ── Phân loại vai trò ────────────────────────────────────────────────────
SWITCH_HOUSE_RULE = workflow(
    "home.switch_house_modal",
    "Xử lý modal 'Chuyển nhà' hiện sau khi vào Trang chủ",
    "Modal 'Chuyển nhà' là GỢI Ý ĐỔI NHÀ của app, KHÔNG phải lỗi.",
    aliases=["modal chuyển nhà", "chuyển nhà", "đổi nhà"],
    success_criteria=["Không còn modal 'Chuyển nhà' che màn hình"],
    tags=["rule", "modal"],
    steps=["nút 'Huỷ' trong modal 'Chuyển nhà' để đóng modal"],
)
LOGOUT = workflow(
    "auth.logout",
    "Đăng xuất tài khoản",
    "Đăng xuất khỏi tài khoản đang đăng nhập",
    aliases=["đăng xuất", "đăng nhập tài khoản khác"],
    preconditions=["Người dùng đã đăng nhập"],
    success_criteria=["Màn hình đăng nhập hiện ra"],
)
SWITCH_ACCOUNT = workflow(
    "auth.switch_account",
    "Đăng nhập bằng tài khoản đã lưu (Chuyển tài khoản)",
    "Mở danh sách tài khoản đã từng đăng nhập rồi chọn một tài khoản để đăng nhập.",
    aliases=["đăng nhập tài khoản", "chuyển tài khoản"],
    preconditions=["Tài khoản đã từng đăng nhập trên máy này"],
    success_criteria=["Màn hình chính của app hiện ra"],
)
#: Bản có bước thật như trong KB — bước mang cả *cách mở* danh sách và cảnh báo mất dữ liệu.
SWITCH_ACCOUNT_WITH_STEPS = workflow(
    "auth.switch_account",
    "Đăng nhập bằng tài khoản đã lưu (Chuyển tài khoản)",
    "Mở danh sách tài khoản đã từng đăng nhập trên máy này rồi chọn một tài khoản để đăng nhập.",
    aliases=["đăng nhập tài khoản", "chuyển tài khoản"],
    preconditions=["Tài khoản đã từng đăng nhập trên máy này"],
    success_criteria=["Màn hình chính của app hiện ra"],
    steps=[
        "Mũi tên trỏ xuống (caret-down) màu xanh ở CUỐI ô nhập số điện thoại",
        "Đúng dòng số điện thoại cần đăng nhập — TUYỆT ĐỐI KHÔNG chạm icon thùng rác đỏ bên "
        "phải dòng, vì nút đó XOÁ tài khoản khỏi danh sách",
        "Nút 'Đăng nhập'",
    ],
)


def roles_for(task: TaskSpec, hits) -> dict[str, str]:
    return {
        assignment.workflow_id: assignment.role
        for assignment in classify_task(task, hits, min_intent_coverage=0.6, min_name_coverage=0.6)
    }


def test_a_rule_workflow_is_never_the_goal():
    """Lỗi đã gặp: modal 'Chuyển nhà' được coi là cách chuyển nhà."""
    task = TaskSpec(action="chuyển nhà", target="dled")

    roles = roles_for(task, [hit(SWITCH_HOUSE_RULE)])

    assert roles["home.switch_house_modal"] == ROLE_SUPPORTING


def test_a_workflow_whose_name_is_the_action_is_primary():
    task = TaskSpec(action="chuyển tài khoản")

    roles = roles_for(task, [hit(SWITCH_ACCOUNT)])

    assert roles["auth.switch_account"] == ROLE_PRIMARY


def test_a_word_overlap_through_an_alias_is_only_supporting():
    """``auth.logout`` có alias 'đăng nhập tài khoản khác' nhưng không phải việc này."""
    task = TaskSpec(action="đăng nhập")

    roles = roles_for(task, [hit(LOGOUT, rank=2)])

    assert roles["auth.logout"] == ROLE_SUPPORTING


def test_a_workflow_that_shares_no_goal_words_is_irrelevant():
    task = TaskSpec(action="bật đèn", target="phòng khách")

    roles = roles_for(task, [hit(SWITCH_HOUSE_RULE), hit(LOGOUT, rank=2)])

    assert roles["home.switch_house_modal"] == ROLE_IRRELEVANT
    assert roles["auth.logout"] == ROLE_IRRELEVANT


def test_an_untrusted_hit_can_never_be_primary():
    task = TaskSpec(action="chuyển tài khoản")

    roles = roles_for(task, [hit(SWITCH_ACCOUNT, trusted=False)])

    assert roles["auth.switch_account"] != ROLE_PRIMARY


def test_rule_detection_uses_tags_or_metadata_kind():
    assert is_rule(SWITCH_HOUSE_RULE) is True
    assert is_rule(LOGOUT) is False
    assert is_rule(workflow("x", "X", tags=["policy"])) is True
    assert is_rule(workflow("y", "Y")) is False


def test_task_knowledge_records_gaps_and_roles():
    plan = IntentPlan(
        tasks=[
            TaskSpec(action="đăng nhập"),
            TaskSpec(action="chuyển nhà", target="dled"),
        ]
    )
    retrieved = [
        (plan.tasks[0], [hit(LOGOUT), hit(SWITCH_ACCOUNT, rank=2)]),
        (plan.tasks[1], [hit(SWITCH_HOUSE_RULE)]),
    ]

    entries = build_task_knowledge(plan, retrieved, min_intent_coverage=0.6, min_name_coverage=0.6)

    assert [entry.gap for entry in entries] == [False, True]
    assert entries[0].primary[0].workflow.id == "auth.switch_account"
    assert entries[1].supporting[0].workflow.id == "home.switch_house_modal"
    assert "bối cảnh" in entries[1].gap_reason


def test_best_role_prefers_primary_over_supporting():
    plan = IntentPlan(tasks=[TaskSpec(action="đăng xuất"), TaskSpec(action="chuyển tài khoản")])
    retrieved = [
        (plan.tasks[0], [hit(LOGOUT)]),
        (plan.tasks[1], [hit(LOGOUT), hit(SWITCH_ACCOUNT, rank=2)]),
    ]

    entries = build_task_knowledge(plan, retrieved, min_intent_coverage=0.6, min_name_coverage=0.6)
    best = best_role(entries)

    assert best["auth.logout"].role == ROLE_PRIMARY
    assert best["auth.switch_account"].role == ROLE_PRIMARY


# ── Dựng phần kiến thức: vai trò quyết định nội dung ─────────────────────
def entries_for(plan: IntentPlan, retrieved) -> list:
    return build_task_knowledge(plan, retrieved, min_intent_coverage=0.6, min_name_coverage=0.6)


def test_a_supporting_workflows_steps_and_criteria_stay_out_of_the_brief():
    """Chốt chặn cho workflow PHỤ: bước và tiêu chí của nó không vào prompt.

    Bước của workflow *chính* thì ngược lại — chúng vào prompt (xem
    :func:`test_primary_knowledge_carries_its_steps`), vì KB ghi cách làm ở đó.
    """
    plan = IntentPlan(tasks=[TaskSpec(action="chuyển nhà", target="dled")])
    entries = entries_for(plan, [(plan.tasks[0], [hit(SWITCH_HOUSE_RULE)])])

    brief = build_knowledge_brief(entries, plan.final_goal, max_chars=4000)

    assert SECTION_PLAN in brief
    assert SECTION_CONTEXT in brief
    assert SECTION_GAP in brief
    assert "nút 'Huỷ'" not in brief
    assert "Không còn modal" not in brief
    assert "KHÔNG phải mục tiêu" in brief


def test_primary_knowledge_carries_conditions_as_things_to_check():
    plan = IntentPlan(tasks=[TaskSpec(action="đăng nhập")])
    entries = entries_for(plan, [(plan.tasks[0], [hit(SWITCH_ACCOUNT)])])

    brief = build_knowledge_brief(entries, plan.final_goal, max_chars=4000)

    assert SECTION_KNOWLEDGE in brief
    assert "không được mặc định là đúng" in brief
    assert "Tài khoản đã từng đăng nhập trên máy này" in brief


def test_primary_knowledge_carries_its_steps():
    """Lỗi đã gặp: prompt chỉ nói "mở danh sách tài khoản đã lưu" mà không nói *cách mở*.

    KB ghi cách mở trong bước ("mũi tên caret-down ở cuối ô số điện thoại") và cảnh báo
    chống mất dữ liệu cũng nằm trong bước — giấu bước là agent gõ số vào ô nhập như một
    người dùng bình thường, và có thể chạm nhầm nút xoá tài khoản.
    """
    plan = IntentPlan(tasks=[TaskSpec(action="đăng nhập")])
    entries = entries_for(plan, [(plan.tasks[0], [hit(SWITCH_ACCOUNT_WITH_STEPS)])])

    brief = build_knowledge_brief(entries, plan.final_goal, max_chars=4000)

    assert SECTION_KNOWLEDGE in brief
    assert "Các bước" in brief
    assert "caret-down" in brief
    assert "XOÁ tài khoản" in brief


def test_plan_and_criteria_survive_a_tight_budget():
    """Hạn mức chật thì cắt kiến thức chi tiết, không cắt cấu trúc."""
    plan = IntentPlan(
        tasks=[TaskSpec(action="đăng nhập"), TaskSpec(action="chuyển nhà", target="dled")]
    )
    entries = entries_for(
        plan,
        [
            (plan.tasks[0], [hit(SWITCH_ACCOUNT)]),
            (plan.tasks[1], [hit(SWITCH_HOUSE_RULE)]),
        ],
    )

    brief = build_knowledge_brief(entries, plan.final_goal, max_chars=700)

    assert SECTION_PLAN in brief
    assert SECTION_GAP in brief
    assert SECTION_CRITERIA in brief


def test_the_knowledge_section_survives_a_tight_budget():
    """Hạn mức chật thì mục kiến thức rút gọn để *còn*, tuyệt đối không biến mất.

    Đo trên KB thật: với ngân sách mặc định cũ (1200), kế hoạch + tiêu chí chiếm hết chỗ
    nên brief chỉ còn 384 ký tự và **mất hẳn** mục KIẾN THỨC — LLM không còn gì để viết
    ngoài kế hoạch, nên nó viết chung chung và chi tiết *chọn tài khoản đã lưu* không bao
    giờ tới được agent.
    """
    plan = IntentPlan(tasks=[TaskSpec(action="đăng nhập")])
    entries = entries_for(plan, [(plan.tasks[0], [hit(SWITCH_ACCOUNT)])])

    brief = build_knowledge_brief(entries, plan.final_goal, max_chars=700)

    assert SECTION_KNOWLEDGE in brief
    # Dòng tiêu đề workflow được giữ trước tiên: biết *workflow nào* quan trọng hơn mô tả dài.
    assert "- auth.switch_account" in brief


def test_a_tiny_budget_still_keeps_the_plan():
    plan = IntentPlan(
        tasks=[TaskSpec(action="đăng nhập"), TaskSpec(action="chuyển nhà", target="dled")]
    )
    entries = entries_for(
        plan,
        [
            (plan.tasks[0], [hit(SWITCH_ACCOUNT)]),
            (plan.tasks[1], [hit(SWITCH_HOUSE_RULE)]),
        ],
    )

    brief = build_knowledge_brief(entries, plan.final_goal, max_chars=120)

    assert SECTION_PLAN in brief
    assert "chuyển nhà dled" in brief


def test_the_deterministic_prompt_keeps_the_goal_and_declares_the_gap():
    plan = IntentPlan(
        tasks=[TaskSpec(action="chuyển nhà", target="dled")],
        final_goal={"type": "nhà hiện tại", "expected_value": "dled"},
    )
    entries = entries_for(plan, [(plan.tasks[0], [hit(SWITCH_HOUSE_RULE)])])

    prompt = build_deterministic_prompt(
        "chuyển sang nhà dled", entries, plan.final_goal, max_chars=4000
    )

    assert prompt.startswith("chuyển sang nhà dled")
    assert "dled" in prompt
    assert SECTION_GAP in prompt
    assert "tự khám phá UI" in prompt
    assert "nút 'Huỷ'" not in prompt


# ── Điều hướng: nối workflow chính với hướng dẫn tới màn hình ──────────────
def navigation_workflow(
    workflow_id: str = "navigation.open_menu",
    name: str = "Mở màn hình Menu",
    *,
    description: str = "Đi tới Menu, nơi chứa thông tin tài khoản và đăng xuất.",
    target: str = "Tab cuối cùng trên thanh điều hướng dưới cùng",
    expected_state: str = "Màn hình Menu và thông tin tài khoản xuất hiện",
) -> Workflow:
    """Workflow ``navigation.*`` — mục đích *chính là* các bước đi vào một màn hình."""
    return Workflow(
        id=workflow_id,
        app_id="hunonic",
        name=name,
        description=description,
        preconditions=["Người dùng đã đăng nhập"],
        steps=[{"action": "tap", "target": target, "expected_state": expected_state}],
        status="draft",
        revision_no=1,
        updated_at="2026-10-27T10:00:00",
    )


def test_navigation_knowledge_is_rendered_with_its_steps():
    """Điều hướng là chỗ duy nhất bước của workflow được vào prompt."""
    lines = render_navigation_knowledge([hit(navigation_workflow())])

    assert lines[0].startswith("- (điều hướng) navigation.open_menu")
    assert any("Tab cuối cùng trên thanh điều hướng dưới cùng" in line for line in lines)
    assert any("Màn hình Menu và thông tin tài khoản xuất hiện" in line for line in lines)


def test_the_brief_puts_navigation_knowledge_in_the_knowledge_section():
    """Lỗi đã gặp: workflow chính nói "'Đăng xuất' ở màn hình Menu" mà không nói cách mở
    màn Menu — agent bịa ra "profile icon ở góc trên bên phải" và tap vào chỗ trống."""
    plan = IntentPlan(tasks=[TaskSpec(action="đăng xuất")])
    entries = entries_for(
        plan,
        [
            (
                plan.tasks[0],
                [hit(LOGOUT), hit(navigation_workflow(), rank=2)],
            )
        ],
    )

    brief = build_knowledge_brief(entries, plan.final_goal, max_chars=4000)

    assert SECTION_KNOWLEDGE in brief
    assert "navigation.open_menu" in brief
    assert "Tab cuối cùng trên thanh điều hướng dưới cùng" in brief


def test_a_plain_supporting_workflow_still_keeps_its_steps_out_of_the_brief():
    """Chốt chặn cũ phải còn: workflow phụ *chức năng* không được lộ bước hay tiêu chí."""
    plan = IntentPlan(tasks=[TaskSpec(action="chuyển nhà", target="dled")])
    entries = entries_for(plan, [(plan.tasks[0], [hit(SWITCH_HOUSE_RULE)])])

    brief = build_knowledge_brief(entries, plan.final_goal, max_chars=4000)

    assert SECTION_CONTEXT in brief
    assert "nút 'Huỷ'" not in brief
    assert "Không còn modal" not in brief


def test_only_the_navigation_namespace_counts_as_navigation():
    assert is_navigation_workflow(navigation_workflow())
    assert not is_navigation_workflow(LOGOUT)
    assert not is_navigation_workflow(workflow("my_navigation.open", "Gần giống nhưng khác"))


# ── Việc bị việc khác bao trùm thì bỏ (tránh agent làm hai lần) ────────────
def test_a_task_contained_in_another_is_dropped():
    """Lỗi đã gặp: câu "vào màn hẹn giờ … 3 lần xem có bao nhiêu …" bị tách thành hai việc, và
    kế hoạch có hai mốc nên agent vào màn hẹn giờ, thoát ra, rồi vào lại."""
    plan = IntentPlan(
        tasks=[
            TaskSpec(action="vào màn hẹn giờ", target="Ổ cắm 12"),
            TaskSpec(
                action="vào màn hẹn giờ 3 lần xem có bao nhiêu danh sách hẹn giờ",
                target="Ổ cắm 12",
            ),
        ],
        source="llm",
    )

    kept = _drop_contained_tasks(plan.tasks)

    assert len(kept) == 1
    assert "bao nhiêu danh sách hẹn giờ" in kept[0].action


def test_a_task_that_only_partly_overlaps_is_kept():
    """Thận trọng: chỉ bỏ khi việc này thật sự *nằm trong* việc kia.  Trùng một phần thì giữ
    cả hai — bỏ nhầm là mất một việc tester yêu cầu."""
    tasks = [
        TaskSpec(action="vào màn hẹn giờ", target="Ổ cắm 12"),
        TaskSpec(action="vào màn hẹn giờ 3 lần xem có bao nhiêu danh sách hẹn giờ"),
    ]

    assert len(_drop_contained_tasks(tasks)) == 2


def test_two_different_tasks_are_both_kept():
    """Khác nhau thì không được bỏ, dù trùng nhiều từ."""
    tasks = [
        TaskSpec(action="bật công tắc", target="Ổ cắm 12"),
        TaskSpec(action="tắt công tắc", target="Ổ cắm 12"),
    ]

    assert len(_drop_contained_tasks(tasks)) == 2

