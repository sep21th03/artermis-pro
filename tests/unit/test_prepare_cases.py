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

"""Sáu ca bắt buộc của bước chuẩn bị prompt.

Mỗi ca là một câu tester thật, và mỗi khẳng định là một điều đã từng sai:

* **Ca 1** — câu ba việc: modal 'Chuyển nhà' từng được coi là cách chuyển nhà, và
  ``auth.logout`` từng lọt vào vì một alias trùng từ.
* **Ca 2** — hai việc ngược nhau: đăng xuất rồi đăng nhập một tài khoản cụ thể.
* **Ca 3** — chỉ một việc, KB không có workflow chính: phải nói ra khoảng trống.
* **Ca 4** — việc KB không hề biết: không được kéo workflow lạ vào.
* **Ca 5** — KB rỗng: giữ nguyên mục tiêu, không bịa quy trình.
* **Ca 6** — workflow phụ mâu thuẫn: giữ phần hữu ích, chặn phần chỉ dẫn sai.
"""

from __future__ import annotations

import json

import pytest

from artemis.knowledge.intent import ROLE_PRIMARY
from artemis.knowledge.composer import (
    SECTION_NOTES,
    SECTION_STEPS,
    SECTION_VERIFY,
)
from artemis.knowledge.models import RetrievalResult, Workflow, WorkflowHit
from artemis.knowledge.prepare import (
    STATUS_FALLBACK,
    STATUS_LLM,
    STATUS_NO_KNOWLEDGE,
    PrepareCache,
    PrepareOptions,
    prepare_prompt,
)

OPTS = PrepareOptions(
    cache_ttl=0.0,
    max_knowledge_chars=1600,
    max_enhanced_chars=4000,
    intent_timeout=5.0,
)


def workflow(workflow_id: str, name: str, description: str = "", **kwargs) -> Workflow:
    return Workflow(
        id=workflow_id,
        app_id="hunonic",
        name=name,
        description=description,
        aliases=list(kwargs.get("aliases", [])),
        preconditions=list(kwargs.get("preconditions", [])),
        success_criteria=list(kwargs.get("success_criteria", [])),
        tags=list(kwargs.get("tags", [])),
        steps=[{"action": "tap", "target": t} for t in kwargs.get("steps", [])],
        status="verified",
        revision_no=1,
        updated_at="2026-10-26T10:00:00",
    )


LOGOUT = workflow(
    "auth.logout",
    "Đăng xuất tài khoản",
    "Đăng xuất khỏi tài khoản Hunonic đang đăng nhập.",
    aliases=["đăng xuất", "thoát tài khoản", "đăng nhập tài khoản khác"],
    preconditions=["Người dùng đã đăng nhập"],
    success_criteria=["Màn hình đăng nhập hiện ra"],
    tags=["auth", "logout"],
    steps=["Mục 'Đăng xuất' ở cuối danh sách Menu"],
)
SWITCH_ACCOUNT = workflow(
    "auth.switch_account",
    "Đăng nhập bằng tài khoản đã lưu (Chuyển tài khoản)",
    "Mở danh sách tài khoản đã từng đăng nhập trên máy này rồi chọn một tài khoản để "
    "đăng nhập. DÙNG KHI prompt chỉ nêu tên/số tài khoản mà KHÔNG có mật khẩu.",
    aliases=["đăng nhập tài khoản", "chuyển tài khoản"],
    preconditions=["App đang ở màn hình Đăng nhập", "Tài khoản đã từng đăng nhập trên máy này"],
    success_criteria=["Màn hình chính của app hiện ra"],
    tags=["auth", "đăng nhập"],
    steps=["Mũi tên caret-down cuối ô số điện thoại"],
)
MODAL = workflow(
    "home.switch_house_modal",
    "Xử lý modal 'Chuyển nhà' hiện sau khi vào Trang chủ",
    "Modal 'Chuyển nhà' bật lên sau khi vào Trang chủ là GỢI Ý ĐỔI NHÀ của app, "
    "KHÔNG phải lỗi và không phải màn hình lạ.",
    aliases=["modal chuyển nhà", "chuyển nhà", "đổi nhà"],
    preconditions=["Đã đăng nhập và vừa vào Trang chủ"],
    success_criteria=["Không còn modal 'Chuyển nhà' che màn hình"],
    tags=["rule", "modal"],
    steps=["nút 'Huỷ' trong modal 'Chuyển nhà' để đóng modal"],
)


class FakeService:
    """Truy xuất giả: xếp hạng bằng độ phủ token, đúng tinh thần kênh keyword."""

    available = True
    degraded_reason = ""

    def __init__(self, workflows) -> None:
        self._workflows = list(workflows)
        self._store = self

    def find_app_by_package(self, package):
        return "hunonic" if package == "com.iot.hunonic" else None

    def get_workflow(self, workflow_id):
        return next((item for item in self._workflows if item.id == workflow_id), None)

    def retrieve(self, query, app_id, version_id=None, top_k=None, run_id=None):
        from artemis.knowledge.text import content_tokens, coverage, normalize

        tokens = content_tokens(query)
        hits = []
        for index, item in enumerate(self._workflows):
            score = coverage(tokens, normalize(item.embedding_text()))
            if score > 0:
                hits.append(
                    WorkflowHit(
                        workflow=item,
                        score=score,
                        channels={"keyword": index + 1},
                        trusted=score >= 0.6,
                    )
                )
        hits.sort(key=lambda found: -found.score)
        return RetrievalResult(
            found=any(found.trusted for found in hits),
            query=query,
            app_id=app_id,
            hits=hits[:3],
            reason="" if hits else "không tìm thấy",
        )


def llm(plan: dict, *, synthesis: str | None = None):
    """LLM giả: JSON cho lượt phân tích; lượt tổng hợp mặc định giữ nguyên đầu vào."""

    def call(messages, *, max_tokens, timeout):
        if "tách một yêu cầu" in messages[0]["content"]:
            return json.dumps(plan, ensure_ascii=False)
        if synthesis is not None:
            return synthesis
        body = messages[1]["content"].split("nguồn duy nhất được dùng]", 1)[-1]
        return body.split("[PROFILE CHẠY]", 1)[0].strip()

    return call


def run(prompt: str, plan: dict, workflows, *, synthesis: str | None = None):
    return prepare_prompt(
        prompt,
        app_package="com.iot.hunonic",
        profile="pro",
        service=FakeService(workflows),
        synthesizer=llm(plan, synthesis=synthesis),
        options=OPTS,
        cache=PrepareCache(ttl=0.0),
    )


def roles(outcome) -> dict[str, str]:
    return {row["workflow_id"]: row["role"] for row in outcome.matched_workflows}


CASE1_PROMPT = "mở app và đăng nhập rồi chuyển sang nhà dled"
CASE1_PLAN = {
    "tasks": [
        {"action": "mở app", "condition": "chỉ khi app chưa mở", "required": True},
        {"action": "đăng nhập", "condition": "chỉ khi chưa đăng nhập", "required": True},
        {"action": "chuyển nhà", "target": "dled", "required": True},
    ],
    "final_goal": {"type": "nhà hiện tại", "expected_value": "dled"},
}


# ── Ca 1 ─────────────────────────────────────────────────────────────────
def test_case1_the_goal_is_the_house_change_not_the_login():
    outcome = run(CASE1_PROMPT, CASE1_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    assert outcome.status == STATUS_LLM
    assert [task["query"] for task in outcome.tasks] == [
        "mở app",
        "đăng nhập",
        "chuyển nhà dled",
    ]
    assert outcome.plan["final_goal"]["expected_value"] == "dled"


def test_case1_the_house_name_is_preserved():
    outcome = run(CASE1_PROMPT, CASE1_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    assert "dled" in outcome.enhanced_prompt
    assert outcome.original_prompt == CASE1_PROMPT


def test_case1_logout_is_never_the_workflow_that_executes():
    outcome = run(CASE1_PROMPT, CASE1_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    assert roles(outcome)["auth.logout"] != ROLE_PRIMARY
    # Nhắc tới nó như *bối cảnh* thì được; đưa nó vào phần chỉ dẫn thì không.
    instructions = outcome.enhanced_prompt.split(SECTION_NOTES, 1)[0]
    assert "auth.logout" not in instructions
    assert "Đăng xuất" not in instructions
    # Và tiêu chí/bước của nó không bao giờ vào prompt.
    assert "Màn hình đăng nhập hiện ra" not in outcome.enhanced_prompt


def test_case1_the_modal_is_not_presented_as_the_way_to_switch_house():
    outcome = run(CASE1_PROMPT, CASE1_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    # Chỉ dẫn cấm: "nhấn Huỷ".  Ngoài ra tiêu chí của một luật không được vào prompt.
    assert "Huỷ" not in outcome.enhanced_prompt
    assert "Không còn modal" not in outcome.enhanced_prompt
    # Nhưng thông tin hữu ích của luật đó vẫn còn, ở mục bối cảnh.
    assert SECTION_NOTES in outcome.enhanced_prompt
    assert "KHÔNG phải lỗi" in outcome.enhanced_prompt


def test_case1_the_completion_criterion_verifies_the_current_house():
    outcome = run(CASE1_PROMPT, CASE1_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    criteria = outcome.enhanced_prompt.split(SECTION_VERIFY, 1)[1]
    assert "dled" in criteria


def test_case1_the_missing_switch_house_workflow_is_declared():
    outcome = run(CASE1_PROMPT, CASE1_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    assert "chưa có hướng dẫn" in outcome.enhanced_prompt
    assert [gap["task"]["query"] for gap in outcome.knowledge_gaps] == [
        "mở app",
        "chuyển nhà dled",
    ]


# ── Ca 2 ─────────────────────────────────────────────────────────────────
CASE2_PROMPT = "đăng xuất rồi đăng nhập tài khoản 0972842548"
CASE2_PLAN = {
    "tasks": [
        {"action": "đăng xuất", "required": True},
        {"action": "đăng nhập", "target": "0972842548", "required": True},
    ],
    "final_goal": {"type": "tài khoản đang đăng nhập", "expected_value": "0972842548"},
}


def test_case2_logout_is_a_required_task_and_login_follows():
    outcome = run(CASE2_PROMPT, CASE2_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    assert [task["query"] for task in outcome.tasks] == ["đăng xuất", "đăng nhập 0972842548"]
    assert roles(outcome)["auth.logout"] == ROLE_PRIMARY
    assert roles(outcome)["auth.switch_account"] == ROLE_PRIMARY


def test_case2_the_account_number_is_preserved_and_verified():
    outcome = run(CASE2_PROMPT, CASE2_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    assert "0972842548" in outcome.enhanced_prompt
    criteria = outcome.enhanced_prompt.split(SECTION_VERIFY, 1)[1]
    assert "0972842548" in criteria


def test_case2_a_saved_account_is_not_assumed():
    """Điều kiện của workflow phải được trình bày như *điều phải kiểm tra*."""
    outcome = run(CASE2_PROMPT, CASE2_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    assert "KHÔNG mặc định là đúng" in outcome.enhanced_prompt
    assert "đã từng đăng nhập trên máy này" in outcome.enhanced_prompt


def test_case2_the_modal_rule_stays_out_of_the_instructions():
    outcome = run(CASE2_PROMPT, CASE2_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    plan_and_knowledge = outcome.enhanced_prompt.split(SECTION_NOTES, 1)[0]
    assert "Huỷ" not in plan_and_knowledge


# ── Ca 3 ─────────────────────────────────────────────────────────────────
CASE3_PROMPT = "chuyển sang nhà dled"
CASE3_PLAN = {
    "tasks": [{"action": "chuyển nhà", "target": "dled", "required": True}],
    "final_goal": {"type": "nhà hiện tại", "expected_value": "dled"},
}


def test_case3_no_login_or_logout_is_added():
    outcome = run(CASE3_PROMPT, CASE3_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    assert [task["action"] for task in outcome.tasks] == ["chuyển nhà"]
    assert "auth.logout" not in roles(outcome) or roles(outcome)["auth.logout"] != ROLE_PRIMARY
    assert "đăng xuất" not in outcome.enhanced_prompt.lower()


def test_case3_an_already_correct_house_ends_the_task_after_verification():
    outcome = run(CASE3_PROMPT, CASE3_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    criteria = outcome.enhanced_prompt.split(SECTION_VERIFY, 1)[1]
    assert "nhà hiện tại" in criteria
    assert "dled" in criteria


def test_case3_the_unrelated_account_workflow_is_excluded():
    outcome = run(CASE3_PROMPT, CASE3_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    assert "auth.switch_account" in [row["workflow_id"] for row in outcome.excluded_workflows]


# ── Ca 4 ─────────────────────────────────────────────────────────────────
CASE4_PROMPT = "bật đèn phòng khách"
CASE4_PLAN = {
    "tasks": [{"action": "bật đèn", "target": "phòng khách", "required": True}],
    "final_goal": {"type": "đèn phòng khách", "expected_value": "bật"},
}


def test_case4_no_irrelevant_workflow_is_retrieved():
    outcome = run(CASE4_PROMPT, CASE4_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    assert outcome.matched_workflows == []
    # Workflow luật trùng từ ("bật lên") bị loại chứ không thành bối cảnh.
    assert "home.switch_house_modal" in [row["workflow_id"] for row in outcome.excluded_workflows]


def test_case4_no_extra_goal_is_added():
    outcome = run(CASE4_PROMPT, CASE4_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    assert [task["action"] for task in outcome.tasks] == ["bật đèn"]
    assert outcome.status == STATUS_NO_KNOWLEDGE
    assert outcome.enhanced_prompt == CASE4_PROMPT


def test_case4_the_gap_is_reported_even_though_nothing_is_injected():
    outcome = run(CASE4_PROMPT, CASE4_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL])

    assert [gap["task"]["query"] for gap in outcome.knowledge_gaps] == ["bật đèn phòng khách"]


# ── Ca 5: KB không có workflow phù hợp ───────────────────────────────────
def test_case5_an_empty_kb_returns_a_gap_and_no_invented_procedure():
    outcome = run(CASE4_PROMPT, CASE4_PLAN, [])

    assert outcome.status == STATUS_NO_KNOWLEDGE
    assert outcome.enhanced_prompt == CASE4_PROMPT
    assert len(outcome.knowledge_gaps) == 1
    assert outcome.knowledge_gaps[0]["task"]["action"] == "bật đèn"
    assert outcome.knowledge_gaps[0]["reason"]
    assert outcome.matched_workflows == []


def test_case5_the_goal_is_kept_so_the_agent_can_explore():
    outcome = run(CASE4_PROMPT, CASE4_PLAN, [])

    assert outcome.enhanced_prompt.startswith(CASE4_PROMPT)
    assert outcome.tasks[0]["target"] == "phòng khách"


# ── Ca 6: workflow phụ có chỉ dẫn mâu thuẫn ──────────────────────────────
#: Đầu ra của một LLM "cố tình viết sai".  Từ khi prompt do composer ghép, văn bản này
#: **không** còn đường vào prompt — nhưng vẫn giữ ca kiểm để chứng minh điều đó.
CONTRADICTORY = (
    "1. mở app\n"
    "2. đăng nhập\n"
    "3. chuyển nhà dled: mở modal 'Chuyển nhà' rồi nhấn 'Huỷ' cho nhanh\n"
    "- nhà hiện tại là dled"
)


def test_case6_a_contradicting_instruction_never_reaches_the_prompt():
    """Ca 6: luật phụ mâu thuẫn với mục tiêu (\"nhấn Huỷ để đóng modal\").

    Bản trước phải *từ chối* bản LLM viết lại (và rơi về bản dự phòng) vì LLM có thể biến
    luật thành bước.  Nay prompt do composer ghép nên chỉ workflow được chọn mới có bước:
    chỉ dẫn sai không thể xuất hiện ngay từ cấu trúc, không cần cơ chế từ chối.
    """
    outcome = run(
        CASE1_PROMPT, CASE1_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL], synthesis=CONTRADICTORY
    )

    assert "Huỷ" not in outcome.enhanced_prompt
    assert "đóng modal" not in outcome.enhanced_prompt
    assert outcome.enhanced_prompt.startswith(CASE1_PROMPT)


def test_case6_the_useful_part_of_the_rule_is_kept():
    outcome = run(
        CASE1_PROMPT, CASE1_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL], synthesis=CONTRADICTORY
    )

    # Luật của app vẫn còn, ở mục LƯU Ý: modal là gợi ý bình thường, không phải lỗi.
    assert SECTION_NOTES in outcome.enhanced_prompt
    assert "KHÔNG phải lỗi" in outcome.enhanced_prompt
    assert "dled" in outcome.enhanced_prompt


def test_case6_the_instruction_part_has_no_modal_action():
    outcome = run(
        CASE1_PROMPT, CASE1_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL], synthesis=CONTRADICTORY
    )

    instruction = outcome.enhanced_prompt.split(SECTION_NOTES, 1)[0]
    assert "Huỷ" not in instruction
    assert "đóng modal" not in instruction


# ── Kết quả chung cho mọi ca ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("prompt", "plan", "workflows"),
    [
        (CASE1_PROMPT, CASE1_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL]),
        (CASE2_PROMPT, CASE2_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL]),
        (CASE3_PROMPT, CASE3_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL]),
        (CASE4_PROMPT, CASE4_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL]),
        (CASE4_PROMPT, CASE4_PLAN, []),
    ],
)
def test_every_case_keeps_the_original_prompt_and_stays_short(prompt, plan, workflows):
    outcome = run(prompt, plan, workflows)

    assert outcome.enhanced_prompt.startswith(prompt)
    assert len(outcome.enhanced_prompt) <= OPTS.max_enhanced_chars
    assert "Các bước:" not in outcome.enhanced_prompt
    assert not any(char.isdigit() for char in outcome.enhanced_prompt.split("bước")[0][-20:])


@pytest.mark.parametrize(
    ("prompt", "plan", "workflows"),
    [
        (CASE1_PROMPT, CASE1_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL]),
        (CASE2_PROMPT, CASE2_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL]),
        (CASE3_PROMPT, CASE3_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL]),
        (CASE4_PROMPT, CASE4_PLAN, [LOGOUT, SWITCH_ACCOUNT, MODAL]),
        (CASE4_PROMPT, CASE4_PLAN, []),
    ],
)
def test_every_case_reports_the_pipeline_it_ran(prompt, plan, workflows):
    outcome = run(prompt, plan, workflows)

    assert outcome.metrics["queries"]
    assert outcome.metrics["plan_source"] == "llm"
    assert "tasks" in outcome.metrics
    assert isinstance(outcome.warnings, list)
