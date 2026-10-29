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

"""AI Prompt Preparation — cơ chế: hiểu mục tiêu, truy xuất theo việc, phân loại, dự phòng.

Sáu tính chất được khoá ở đây, mỗi tính chất là một cách hỏng khác nhau:

1. **Mỗi việc một truy vấn** — câu nhiều việc không còn là một truy vấn duy nhất.
2. **Vai trò quyết định nội dung** — luật/modal là bối cảnh, không bao giờ là mục tiêu;
   workflow chỉ trùng từ bị loại và được báo ra.
3. **Bất biến của prompt gốc** — mục tiêu và định danh không đổi.
4. **Không bỏ sót việc** — mọi việc trong kế hoạch còn trong prompt.
5. **Không bịa, không UI cứng** — không workflow lạ, không toạ độ.
6. **Không bao giờ chặn tester** — LLM/KB hỏng thì trả bản dựng bằng luật + lý do.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from artemis.knowledge.composer import (
    SECTION_NOTES,
    SECTION_STATE,
    SECTION_STEPS,
    SECTION_TASK,
    SECTION_VERIFY,
)
from artemis.knowledge.intent import sanitize_ui_instructions
from artemis.knowledge.models import RetrievalResult, Workflow, WorkflowHit
from artemis.knowledge.prepare import (
    STATUS_DEGRADED,
    STATUS_FALLBACK,
    STATUS_LLM,
    STATUS_NO_KNOWLEDGE,
    PrepareCache,
    PrepareOptions,
    RateLimiter,
    prepare_prompt,
)
from artemis.knowledge.text import identity_tokens

PROMPT = "mở app và đăng nhập rồi chuyển sang nhà dled"
OPTS = PrepareOptions(
    cache_ttl=60.0,
    cache_size=8,
    max_knowledge_chars=1600,
    max_enhanced_chars=4000,
    intent_timeout=5.0,
)
PLAN = {
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
    metadata=None,
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
        metadata=dict(metadata or {}),
        status="verified",
        revision_no=1,
        updated_at="2026-10-26T10:00:00",
    )


LOGOUT = workflow(
    "auth.logout",
    "Đăng xuất tài khoản",
    "Đăng xuất khỏi tài khoản đang đăng nhập",
    aliases=["đăng xuất", "đăng nhập tài khoản khác"],
    preconditions=["Người dùng đã đăng nhập"],
    success_criteria=["Màn hình đăng nhập hiện ra"],
    tags=["auth"],
    steps=["Mục 'Đăng xuất' ở cuối danh sách Menu"],
    metadata={"warnings": ["Hàng trên 'Đăng xuất' là 'Đánh giá 5 sao' — đừng tap nhầm"]},
)
SWITCH_ACCOUNT = workflow(
    "auth.switch_account",
    "Đăng nhập bằng tài khoản đã lưu (Chuyển tài khoản)",
    "Mở danh sách tài khoản đã từng đăng nhập rồi chọn một tài khoản để đăng nhập.",
    aliases=["đăng nhập tài khoản", "chuyển tài khoản"],
    preconditions=["Tài khoản đã từng đăng nhập trên máy này"],
    success_criteria=["Màn hình chính của app hiện ra"],
    tags=["auth"],
    steps=["Mũi tên caret-down cuối ô số điện thoại"],
)
MODAL = workflow(
    "home.switch_house_modal",
    "Xử lý modal 'Chuyển nhà' hiện sau khi vào Trang chủ",
    "Modal 'Chuyển nhà' là GỢI Ý ĐỔI NHÀ của app, KHÔNG phải lỗi.",
    aliases=["modal chuyển nhà", "chuyển nhà", "đổi nhà"],
    preconditions=["Đã đăng nhập và vừa vào Trang chủ"],
    success_criteria=["Không còn modal 'Chuyển nhà' che màn hình"],
    tags=["rule", "modal"],
    steps=["nút 'Huỷ' trong modal 'Chuyển nhà' để đóng modal"],
)


class FakeService:
    """Truy xuất giả theo độ phủ token — đúng tinh thần kênh keyword của RAG."""

    available = True
    degraded_reason = ""

    def __init__(self, workflows=(), *, app_id: str | None = "hunonic") -> None:
        self._workflows = list(workflows)
        self._app_id = app_id
        self._store = self
        self.queries: list[str] = []

    def find_app_by_package(self, package):
        return self._app_id if package == "com.iot.hunonic" else None

    def get_workflow(self, workflow_id):
        """Store thật có hàm này; cache dùng nó để xác nhận phiên bản KB."""
        return next((item for item in self._workflows if item.id == workflow_id), None)

    def list_workflows(self, app_id):
        """Bước sắp xếp cần *toàn bộ* ứng viên điều hướng, không chỉ những gì truy hồi được."""
        return list(self._workflows)

    def retrieve(self, query, app_id, version_id=None, top_k=None, run_id=None):
        from artemis.knowledge.text import content_tokens, coverage, normalize

        self.queries.append(query)
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


def synthesizer(plan: dict | None = None, *, synthesis: str | None = None, error=None, selection=None):
    """LLM giả ba lượt: JSON cho phân tích, JSON cho sắp xếp, văn bản cho tổng hợp.

    Mặc định lượt tổng hợp trả lại đúng phần kiến thức đã gửi — hành vi của một LLM
    "đúng nhưng lười", đủ để mọi kiểm tra chất lượng đi qua.  Lượt *sắp xếp* mặc định trả
    về payload của lượt phân tích (không phải JSON chọn cách làm), nên bước sắp xếp không
    quyết được và prepare giữ đường phân loại theo từ — trừ khi test truyền ``selection``.
    """

    def call(messages, *, max_tokens, timeout):
        call.calls.append(messages)
        if error is not None:
            raise error
        if "tách một yêu cầu" in messages[0]["content"]:
            return json.dumps(plan if plan is not None else PLAN, ensure_ascii=False)
        if "chọn CÁCH LÀM" in messages[0]["content"] and selection is not None:
            return json.dumps(selection, ensure_ascii=False)
        if synthesis is not None:
            return synthesis
        body = messages[1]["content"].split("nguồn duy nhất được dùng]", 1)[-1]
        return body.split("[PROFILE CHẠY]", 1)[0].strip()

    call.calls = []
    return call


def prepare(service, synth=None, **kwargs):
    kwargs.setdefault("options", OPTS)
    kwargs.setdefault("cache", PrepareCache())
    kwargs.setdefault("app_package", "com.iot.hunonic")
    return prepare_prompt(PROMPT, service=service, synthesizer=synth, **kwargs)


# ── Đường hạnh phúc ──────────────────────────────────────────────────────
def test_each_task_is_retrieved_separately():
    service = FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL])

    prepare(service, synthesizer())

    assert service.queries == ["mở app", "đăng nhập", "chuyển nhà dled"]


def test_the_llm_path_returns_a_structured_prompt():
    outcome = prepare(FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL]), synthesizer())

    assert outcome.status == STATUS_LLM
    assert outcome.enhanced_prompt.startswith(PROMPT)
    assert [task["query"] for task in outcome.tasks] == ["mở app", "đăng nhập", "chuyển nhà dled"]
    # Prompt do composer ghép: năm mục theo thứ tự, mục nào không có nội dung thì bỏ.
    for section in (SECTION_TASK, SECTION_STEPS, SECTION_VERIFY):
        assert section in outcome.enhanced_prompt
    assert outcome.source["synthesis"] == "deterministic"
    assert outcome.ready is True


def test_roles_decide_what_enters_the_prompt():
    outcome = prepare(FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL]), synthesizer())

    roles = {row["workflow_id"]: row["role"] for row in outcome.matched_workflows}
    assert roles["auth.switch_account"] == "primary"
    assert roles["home.switch_house_modal"] == "supporting"
    assert roles["auth.logout"] == "supporting"
    # Workflow phụ không được thành bước, và tiêu chí của nó không vào prompt.
    steps_block = outcome.enhanced_prompt.split(SECTION_STEPS, 1)[1]
    assert "nút 'Huỷ'" not in steps_block
    assert "Không còn modal" not in outcome.enhanced_prompt


def test_gaps_are_declared_for_tasks_without_a_primary_workflow():
    outcome = prepare(FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL]), synthesizer())

    assert [gap["task"]["query"] for gap in outcome.knowledge_gaps] == [
        "mở app",
        "chuyển nhà dled",
    ]
    assert "dled" in outcome.enhanced_prompt
    assert "chưa có hướng dẫn" in outcome.enhanced_prompt
    assert "KHÔNG tự suy diễn" in outcome.enhanced_prompt


def test_two_llm_calls_and_stage_metrics_are_reported():
    """Hai lượt: phân tích mục tiêu → **sắp xếp KB**.  Ghép prompt không gọi LLM."""
    call = synthesizer()
    outcome = prepare(FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL]), call)

    assert len(call.calls) == 2
    assert outcome.metrics["llm_calls"] == 2
    assert outcome.metrics["plan_source"] == "llm"
    assert outcome.metrics["queries"] == ["mở app", "đăng nhập", "chuyển nhà dled"]
    for key in ("intent_ms", "retrieval_ms", "compose_ms", "total_ms"):
        assert key in outcome.metrics


def test_the_source_reports_per_query_hits():
    outcome = prepare(FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL]), synthesizer())

    per_query = {row["query"]: row["hits"] for row in outcome.source["per_query"]}
    assert per_query["chuyển nhà dled"][0] == "home.switch_house_modal"
    assert "auth.switch_account" in per_query["đăng nhập"]
    assert outcome.source["kb_version"]


def test_primary_conditions_and_warnings_are_kept_as_things_to_check():
    outcome = prepare(FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL]), synthesizer())

    assert "KHÔNG mặc định là đúng" in outcome.enhanced_prompt
    assert "Tài khoản đã từng đăng nhập trên máy này" in outcome.enhanced_prompt


def test_warnings_from_primary_workflows_are_reported():
    plan = {"tasks": [{"action": "đăng xuất", "required": True}]}
    outcome = prepare_prompt(
        "đăng xuất khỏi tài khoản đang dùng",
        app_package="com.iot.hunonic",
        service=FakeService([LOGOUT]),
        synthesizer=synthesizer(plan),
        options=PrepareOptions(cache_ttl=0.0),
    )

    assert any("Đánh giá 5 sao" in warning for warning in outcome.warnings)
    assert "Đánh giá 5 sao" in outcome.enhanced_prompt


# ── Không có kiến thức: không gọi LLM ────────────────────────────────────
def test_no_hits_at_all_means_no_llm_call():
    call = synthesizer()
    outcome = prepare(FakeService([]), call)

    assert outcome.status == STATUS_NO_KNOWLEDGE
    assert outcome.enhanced_prompt == PROMPT
    assert len(call.calls) == 1  # chỉ lượt phân tích mục tiêu
    assert [gap["task"]["query"] for gap in outcome.knowledge_gaps] == [
        "mở app",
        "đăng nhập",
        "chuyển nhà dled",
    ]


def test_an_unregistered_app_returns_the_original_prompt():
    service = FakeService([LOGOUT], app_id=None)

    outcome = prepare(service, synthesizer(), app_package="com.la.hoac")

    assert outcome.status == STATUS_NO_KNOWLEDGE
    assert "com.la.hoac" in outcome.fallback_reason
    assert service.queries == []


def test_an_unavailable_service_returns_the_original_prompt(monkeypatch):
    import artemis.knowledge.service as service_module

    monkeypatch.setattr(service_module, "get_service", lambda: service_module.KnowledgeService())

    outcome = prepare_prompt(PROMPT, synthesizer=synthesizer(), options=OPTS)

    assert outcome.status == STATUS_DEGRADED
    assert outcome.enhanced_prompt == PROMPT


def test_a_service_that_raises_never_escapes(monkeypatch):
    import artemis.knowledge.service as service_module

    def explode():
        raise RuntimeError("database mất kết nối")

    monkeypatch.setattr(service_module, "get_service", explode)

    outcome = prepare_prompt(PROMPT, synthesizer=synthesizer(), options=OPTS)

    assert outcome.status == STATUS_DEGRADED
    assert "database mất kết nối" in outcome.fallback_reason


def test_a_degraded_retrieval_returns_the_original_prompt():
    class Degraded(FakeService):
        def retrieve(self, query, app_id, version_id=None, top_k=None, run_id=None):
            return RetrievalResult(
                found=False,
                query=query,
                app_id=app_id,
                degraded=True,
                reason="database is down",
            )

    outcome = prepare(Degraded([LOGOUT]), synthesizer())

    assert outcome.status == STATUS_DEGRADED
    assert outcome.enhanced_prompt == PROMPT


def test_an_empty_prompt_is_answered_not_rejected():
    outcome = prepare_prompt("   ", service=FakeService([LOGOUT]), options=OPTS)

    assert outcome.status == STATUS_NO_KNOWLEDGE


# ── LLM hỏng ⇒ prompt vẫn ra, ghép bằng luật ─────────────────────────────
def test_a_broken_llm_still_yields_a_composed_prompt():
    """LLM hỏng chỉ mất phần *hiểu yêu cầu* và *chọn cách làm*; phần ghép prompt là code,
    nên tester vẫn nhận prompt có cấu trúc, kèm cảnh báo để kiểm lại cách làm chính."""
    outcome = prepare(
        FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL]), synthesizer(error=RuntimeError("proxy 502"))
    )

    assert outcome.status == STATUS_FALLBACK
    assert any("mô tả KB" in str(note) for note in outcome.warnings)
    assert outcome.enhanced_prompt.startswith(PROMPT)
    for section in (SECTION_TASK, SECTION_VERIFY):
        assert section in outcome.enhanced_prompt
    assert outcome.source["synthesis"] == "deterministic"


def test_without_an_llm_the_prompt_is_still_composed_from_knowledge():
    """Không có LLM thì mất phần hiểu yêu cầu + chọn cách làm, nhưng prompt vẫn được ghép
    từ kiến thức đã truy hồi — không còn rơi về "một việc cho cả câu"."""
    service = FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL])

    outcome = prepare(service, None)

    assert outcome.status == STATUS_FALLBACK
    assert service.queries == [PROMPT]
    assert outcome.metrics["plan_source"] == "deterministic"
    assert outcome.enhanced_prompt.startswith(PROMPT)
    assert SECTION_TASK in outcome.enhanced_prompt

def test_a_second_identical_request_is_served_from_cache():
    cache = PrepareCache(ttl=60)
    service = FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL])
    call = synthesizer()

    first = prepare(service, call, cache=cache)
    second = prepare(service, call, cache=cache)

    assert first.cached is False
    assert second.cached is True
    assert second.enhanced_prompt == first.enhanced_prompt
    assert second.source["cached"] is True
    assert second.metrics["llm_calls"] == 0
    assert len(call.calls) == 2  # chỉ lần đầu gọi LLM (phân tích + sắp xếp)


def test_editing_a_workflow_invalidates_the_cache():
    cache = PrepareCache(ttl=60)
    call = synthesizer()

    prepare(FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL]), call, cache=cache)
    edited = MODAL.model_copy(update={"revision_no": 9, "updated_at": "2026-10-27T09:00:00"})
    after_edit = prepare(FakeService([LOGOUT, SWITCH_ACCOUNT, edited]), call, cache=cache)

    assert after_edit.cached is False


def test_use_cache_false_bypasses_the_cache():
    cache = PrepareCache(ttl=60)
    call = synthesizer()

    prepare(FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL]), call, cache=cache)
    again = prepare(
        FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL]), call, cache=cache, use_cache=False
    )

    assert again.cached is False


def test_a_request_whose_selection_failed_is_still_usable():
    """Bước sắp xếp hỏng thì prompt vẫn ghép được từ kiến thức truy hồi, và được cache lại."""
    cache = PrepareCache(ttl=60)
    broken = {"on": True}

    def call(messages, *, max_tokens, timeout):
        if "tách một yêu cầu" in messages[0]["content"]:
            return json.dumps(PLAN, ensure_ascii=False)
        if broken["on"]:
            raise RuntimeError("proxy 502")
        return json.dumps({"tasks": [{"index": 1, "primary": None}]}, ensure_ascii=False)

    first = prepare(FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL]), call, cache=cache)
    second = prepare(FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL]), call, cache=cache)

    assert first.enhanced_prompt.startswith(PROMPT)
    assert any("mô tả KB" in str(note) for note in first.warnings)
    assert second.cached is True


# ── Hạn mức và cấu hình ──────────────────────────────────────────────────
def test_the_rate_limiter_blocks_the_nth_plus_one_request():
    limiter = RateLimiter(max_events=2, window_seconds=30)

    assert limiter.allow("k") == (True, 0.0)
    assert limiter.allow("k")[0] is True
    allowed, retry_after = limiter.allow("k")

    assert allowed is False
    assert 0 < retry_after <= 30
    assert limiter.allow("other")[0] is True


def test_the_rate_limiter_forgets_old_events(monkeypatch):
    import artemis.knowledge.prepare as prepare_module

    clock = {"now": 1000.0}
    monkeypatch.setattr(prepare_module, "time", SimpleNamespace(time=lambda: clock["now"]))
    limiter = RateLimiter(max_events=1, window_seconds=60)

    assert limiter.allow("k")[0] is True
    assert limiter.allow("k")[0] is False
    clock["now"] += 61

    assert limiter.allow("k")[0] is True


def test_options_come_from_the_environment():
    fake = SimpleNamespace(
        ARTEMIS_PREPARE_MAX_KNOWLEDGE_CHARS=500,
        ARTEMIS_PREPARE_MAX_PROMPT_CHARS=2000,
        ARTEMIS_PREPARE_MAX_OUTPUT_TOKENS=123,
        ARTEMIS_PREPARE_TIMEOUT=3.5,
        ARTEMIS_PREPARE_CACHE_TTL=5.0,
        ARTEMIS_PREPARE_CACHE_SIZE=7,
        ARTEMIS_PREPARE_RATE_LIMIT=4,
        ARTEMIS_PREPARE_RATE_WINDOW=9.0,
        ARTEMIS_PREPARE_COVERAGE_FLOOR=0.7,
        ARTEMIS_PREPARE_TOP_K=2,
        ARTEMIS_PREPARE_INTENT_ENABLED=False,
        ARTEMIS_PREPARE_MAX_TASKS=2,
        ARTEMIS_PREPARE_INTENT_MAX_TOKENS=111,
        ARTEMIS_PREPARE_INTENT_TIMEOUT=4.5,
        ARTEMIS_PREPARE_NAME_COVERAGE_FLOOR=0.8,
        ARTEMIS_PREPARE_INTENT_COVERAGE_FLOOR=0.7,
        ARTEMIS_PREPARE_CONFLICT_FLOOR=0.9,
    )

    options = PrepareOptions.from_settings(fake)

    assert options.max_knowledge_chars == 500
    assert options.max_enhanced_chars == 2000
    assert options.intent_enabled is False
    assert options.max_tasks == 2
    assert options.intent_max_tokens == 111
    assert options.intent_timeout == 4.5
    assert options.name_coverage_floor == 0.8
    assert options.intent_coverage_floor == 0.7
    assert options.conflict_floor == 0.9
    assert options.top_k == 2


def test_options_fall_back_to_defaults_without_settings():
    options = PrepareOptions.from_settings(SimpleNamespace())

    # 1200 từng là mặc định, nhưng nó không đủ cho hai workflow: kế hoạch + tiêu chí chiếm
    # hết ngân sách nên mục kiến thức bị bỏ toàn bộ (xem compact_knowledge_lines).
    assert options.max_knowledge_chars == 2400
    assert options.intent_enabled is True
    assert options.max_tasks == 4
    assert options.rate_limit == 30


def test_intent_analysis_can_be_switched_off():
    options = PrepareOptions(intent_enabled=False, cache_ttl=0.0)
    call = synthesizer()
    service = FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL])

    outcome = prepare(service, call, options=options)

    assert service.queries == [PROMPT]
    assert outcome.metrics["plan_source"] == "deterministic"
    # Tắt phân tích mục tiêu thì chỉ còn lượt sắp xếp KB; ghép prompt không gọi LLM.
    assert len(call.calls) == 1


def test_the_top_k_override_reaches_retrieval():
    service = FakeService([LOGOUT, SWITCH_ACCOUNT, MODAL])
    captured: list = []
    original = service.retrieve

    def spy(query, app_id, version_id=None, top_k=None, run_id=None):
        captured.append(top_k)
        return original(query, app_id, version_id=version_id, top_k=top_k, run_id=run_id)

    service.retrieve = spy

    prepare(service, synthesizer(), options=PrepareOptions(top_k=1, cache_ttl=0.0))

    assert captured == [1, 1, 1]


# ── Chuẩn hoá và định danh ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "chạm vào (648, 1514) rồi thoát",
        "bounds [28,1093,692,1198] là nút đăng xuất",
        "nút ở 648,1514 px",
        "dùng toạ độ 120dp để chạm",
    ],
)
def test_coordinates_and_pixels_are_stripped(text):
    cleaned = sanitize_ui_instructions(text)

    assert "648" not in cleaned
    assert "1093" not in cleaned
    assert "120" not in cleaned


def test_sanitizing_keeps_ordinary_numbers():
    """Số điện thoại và số lượng của tester không được coi là toạ độ."""
    assert sanitize_ui_instructions("gọi 0987654321 đặt 2 thiết bị") == (
        "gọi 0987654321 đặt 2 thiết bị"
    )


def test_identity_tokens_find_the_data_that_must_not_change():
    assert identity_tokens("gọi 0987654321, mail a.b@x.vn, mã DEV-01, xem https://a.b/c") == [
        "0987654321",
        "a.b@x.vn",
        "DEV-01",
        "https://a.b/c",
    ]


# ── Điều hướng: workflow chính cần tới một màn hình ────────────────────────
LOGOUT_PROMPT = "đăng xuất khỏi tài khoản đang đăng nhập"
LOGOUT_PLAN = {
    "tasks": [{"action": "đăng xuất", "required": True}],
    "final_goal": {"type": "trạng thái đăng nhập", "expected_value": "đã đăng xuất"},
}
LOGOUT_ON_MENU = workflow(
    "auth.logout",
    "Đăng xuất tài khoản",
    "Ở màn hình Menu, mục 'Đăng xuất' nằm ở khoảng 73% chiều cao và đã hiện sẵn.",
    aliases=["đăng xuất"],
    preconditions=["Người dùng đã đăng nhập"],
    success_criteria=["Màn hình đăng nhập hiện ra"],
    tags=["auth"],
    steps=["Mục 'Đăng xuất' ở cuối danh sách"],
)
NAVIGATION_MENU = workflow(
    "navigation.open_menu",
    "Mở màn hình Menu",
    # Cố ý KHÔNG dùng từ nào của câu tester ("đăng", "xuất", "nhập"): để nó chỉ tới được
    # prompt qua truy vấn *màn hình* của tầng điều hướng, chứ không nhờ trùng từ.
    "Đi tới Menu, nơi chứa các mục cài đặt của ứng dụng.",
    aliases=["mở menu"],
    preconditions=["Ứng dụng đang mở"],
    success_criteria=["Màn hình Menu hiện ra"],
    steps=["Tab cuối cùng trên thanh điều hướng dưới cùng"],
)


def prepare_logout(service, synth=None, **kwargs):
    kwargs.setdefault("options", OPTS)
    kwargs.setdefault("cache", PrepareCache())
    kwargs.setdefault("app_package", "com.iot.hunonic")
    return prepare_prompt(LOGOUT_PROMPT, service=service, synthesizer=synth, **kwargs)

# ── Sắp xếp KB: model đọc mô tả KB rồi chọn cách làm ───────────────────────
LOGIN_PROMPT = "đăng nhập tài khoản 0972842548"
LOGIN_PLAN = {
    "tasks": [{"action": "đăng nhập", "target": "0972842548", "required": True}],
    "final_goal": {"type": "tài khoản đăng nhập", "expected_value": "0972842548"},
}
#: Hai ứng viên mà luật chọn nằm **trong mô tả**, y như KB thật.
LOGIN_BY_PASSWORD = workflow(
    "auth.login_password",
    "Đăng nhập bằng tài khoản và mật khẩu",
    "Đăng nhập bằng số điện thoại hoặc email và mật khẩu do tester cung cấp. Không dùng "
    "workflow này khi prompt chỉ nêu một tài khoản đã lưu mà không có mật khẩu.",
    aliases=["đăng nhập", "đăng nhập bằng mật khẩu"],
    preconditions=[
        "Ứng dụng đang ở màn hình Đăng nhập",
        "Tester đã cung cấp tài khoản và mật khẩu hợp lệ",
    ],
    steps=["Ô Số điện thoại hoặc Email", "Ô Mật khẩu", "Nút Đăng nhập"],
)
LOGIN_BY_SAVED = workflow(
    "auth.switch_account",
    "Đăng nhập bằng tài khoản đã lưu (Chuyển tài khoản)",
    "Mở danh sách tài khoản đã từng đăng nhập trên máy này rồi chọn một tài khoản để đăng "
    "nhập. DÙNG KHI prompt chỉ nêu tên/số tài khoản mà KHÔNG có mật khẩu.",
    aliases=["đăng nhập", "chuyển tài khoản"],
    preconditions=[
        "App đang ở màn hình Đăng nhập",
        "Tài khoản đã từng đăng nhập trên máy này",
        "Promt chỉ ghi mỗi tài khoản không ghi mật khẩu",
    ],
    steps=[
        "Mũi tên trỏ xuống (caret-down) màu xanh ở CUỐI ô nhập số điện thoại",
        "Đúng dòng số điện thoại cần đăng nhập",
        "Nút 'Đăng nhập'",
    ],
)
NAVIGATION_MENU = workflow(
    "navigation.open_menu",
    "Mở màn hình Menu",
    "Đi tới Menu, nơi chứa các mục cài đặt của ứng dụng.",
    aliases=["mở menu"],
    preconditions=["Ứng dụng đang mở"],
    steps=["Tab cuối cùng trên thanh điều hướng dưới cùng"],
)


def prepare_login(service, synth=None, prompt=LOGIN_PROMPT, **kwargs):
    kwargs.setdefault("options", OPTS)
    kwargs.setdefault("cache", PrepareCache())
    kwargs.setdefault("app_package", "com.iot.hunonic")
    return prepare_prompt(prompt, service=service, synthesizer=synth, **kwargs)


#: Câu tester thật có mật khẩu (đúng ca đã hỏng: prompt mang mật khẩu mà vẫn đi theo luồng
#: "tài khoản đã lưu", vốn ghi rõ điều kiện "promt chỉ ghi mỗi tài khoản không ghi mật khẩu").
WITH_PASSWORD = "đăng nhập tài khoản 0972842548 mật khẩu (Hoa21092003)"

#: Hai ứng viên dựng theo đúng hình dạng KB thật: tên/alias hoà nhau về mọi kênh trùng từ, nên
#: **thứ tự truy hồi** là thứ duy nhất còn quyết định — đúng cách lần chạy thật đã chọn sai.
LEXICAL_SWITCH = workflow(
    "auth.switch_account",
    "Đăng nhập tài khoản",
    "Mở danh sách tài khoản đã lưu rồi chọn một tài khoản. DÙNG KHI prompt chỉ nêu tài khoản mà "
    "KHÔNG có mật khẩu.",
    aliases=["đăng nhập"],
    preconditions=[
        "App đang ở màn hình Đăng nhập",
        "Tài khoản cần đăng nhập đã từng đăng nhập trên máy này",
        "Promt chỉ ghi mỗi tài khoản không ghi mật khẩu",
    ],
    steps=[
        "Mũi tên trỏ xuống ở cuối ô nhập số điện thoại",
        "Dòng số điện thoại trong sheet Chuyển tài khoản",
        "Nút Đăng nhập",
    ],
)
LEXICAL_PASSWORD = workflow(
    "auth.login_password",
    "Đăng nhập tài khoản bằng mật khẩu",
    "Đăng nhập bằng số điện thoại hoặc email và mật khẩu do tester cung cấp.",
    aliases=["đăng nhập"],
    preconditions=[
        "Ứng dụng đang ở màn hình Đăng nhập",
        "Tester đã cung cấp tài khoản và mật khẩu hợp lệ",
    ],
    steps=["Ô Số điện thoại hoặc Email", "Ô Mật khẩu", "Nút Đăng nhập"],
)
LEXICAL_PLAN = {
    "tasks": [{"action": "đăng nhập tài khoản", "target": "0972842548", "required": True}],
    "final_goal": {"type": "tài khoản đăng nhập", "expected_value": "0972842548"},
}


def _login_roles(outcome) -> dict:
    return {row["workflow_id"]: row for row in outcome.matched_workflows}


def test_a_choice_that_violates_its_own_precondition_is_replaced():
    """Model đọc mô tả KB mà chọn sai thì chốt tất định phải hạ nó và nâng ứng viên hợp lệ."""
    service = FakeService([LOGIN_BY_PASSWORD, LOGIN_BY_SAVED])

    outcome = prepare_login(
        service,
        synthesizer(
            LOGIN_PLAN,
            selection={
                "tasks": [
                    {
                        "index": 1,
                        "primary": "auth.switch_account",
                        "supporting": [],
                        "rejected": [],
                        "reason": "model chọn sai: prompt có mật khẩu",
                    }
                ],
                "navigation": {},
            },
        ),
        prompt=WITH_PASSWORD,
    )

    roles = _login_roles(outcome)
    assert roles["auth.login_password"]["role"] == "primary"
    assert roles["auth.switch_account"]["role"] == "supporting"
    assert "điều kiện trước" in roles["auth.switch_account"]["role_reason"]
    # Điều kiện trước bị vi phạm không được xuất hiện như điều phải kiểm tra trên UI.
    assert "Promt chỉ ghi mỗi tài khoản không ghi mật khẩu" not in outcome.enhanced_prompt
    assert "auth.login_password" in outcome.enhanced_prompt


def test_the_validator_allows_ids_that_are_in_the_kb_but_not_retrieved():
    """Guard nhắc workflow **có thật trong KB** dù nó không được truy hồi — phải cho qua.

    ``auth.logout`` và ``auth.current_account`` không nằm trong tập truy hồi của câu tester
    (chúng là *cách đạt* và *cách quan sát*, không phải việc cần làm), nên nếu chốt "nhắc
    workflow không có trong KB" không được mở cho chúng thì prompt đúng bị thay bằng bản dự
    phòng — guard biến mất đúng lúc cần nhất.
    """
    from artemis.knowledge.intent import ROLE_PRIMARY, RoleAssignment, TaskKnowledge, TaskSpec
    from artemis.knowledge.prepare import failed_checks, validate_composed

    composed = (
        "đăng nhập tài khoản 0999\n\n"
        "CÁC BƯỚC THỰC HIỆN:\n"
        "«đăng nhập tài khoản 0999» — theo KB auth.switch_account\n"
        "  1. tap: Mũi tên caret-down cuối ô số điện thoại\n"
        "- Cách quan sát tài khoản đang đăng nhập: theo KB auth.current_account.\n"
        "- Nếu phải đổi: làm theo KB auth.logout rồi auth.login_password.\n"
    )
    entry = TaskKnowledge(
        task=TaskSpec(action="đăng nhập tài khoản", target="0999"),
        index=0,
        primary=[
            WorkflowHit(workflow=SWITCH_ACCOUNT, score=0.9, channels={"keyword": 1}, trusted=True)
        ],
        roles=[RoleAssignment(workflow_id=SWITCH_ACCOUNT.id, role=ROLE_PRIMARY, reason="test")],
    )

    allowed = validate_composed(
        composed,
        composed,
        [entry],
        max_chars=4000,
        known_extra={"auth.current_account", "auth.logout", "auth.login_password"},
    )
    assert failed_checks(allowed) == []

    # Răng của test: thiếu ``known_extra`` thì đúng chốt đó phải bắt được.
    blocked = validate_composed(composed, composed, [entry], max_chars=4000)
    assert "workflow_ids_grounded" in [check.name for check in failed_checks(blocked)]


def test_the_lexical_path_is_guarded_too():
    """Lượt chọn cách làm hỏng ⇒ prepare rơi về phân loại theo từ.  Chốt phải áp *cả* đường này,
    vì đây chính là đường đã tạo ra prompt sai trên máy thật.

    Hai ứng viên ở đây dựng đúng như KB thật: tên và alias hoà nhau tới mức chỉ còn **thứ tự
    truy hồi** quyết định ai là cách làm chính (``switch_account`` được truy hồi trước, y như
    lần chạy thật), nên nếu thiếu chốt thì ``switch_account`` sẽ làm cách làm chính.
    """
    service = FakeService([LEXICAL_SWITCH, LEXICAL_PASSWORD])

    outcome = prepare_login(service, synthesizer(LEXICAL_PLAN), prompt=WITH_PASSWORD)

    roles = _login_roles(outcome)
    assert roles["auth.switch_account"]["role"] == "supporting"
    assert "điều kiện trước" in roles["auth.switch_account"]["role_reason"]
    assert roles["auth.login_password"]["role"] == "primary"
    assert "auth.login_password" in outcome.enhanced_prompt
    assert "Promt chỉ ghi mỗi tài khoản không ghi mật khẩu" not in outcome.enhanced_prompt



# ── Cách quan sát trạng thái: bước phải vào prompt, không chỉ id workflow ──
ACCOUNT_PLAN = {
    "tasks": [
        {
            "action": "đăng nhập tài khoản",
            "target": "0999",
            "condition": "chỉ khi chưa đăng nhập bằng tài khoản 0999",
            "required": True,
        }
    ],
    "final_goal": {"type": "tài khoản đang đăng nhập", "expected_value": "0999"},
}


def test_the_observation_steps_of_a_state_dimension_reach_the_prompt():
    """Ca thật 28/10/2026: prompt chỉ ghi *"theo KB auth.current_account"* ⇒ agent tap mò.

    Chốt ``labels_grounded`` cũng phải mở cho workflow *quan sát*: nhãn được trích dẫn trong
    bước quan sát ('Menu') chỉ nằm trong workflow đó, mà nó không thuộc tập truy hồi — thiếu
    ``extra_workflows`` thì cả prompt ghép bị thay bằng bản dự phòng, và guard biến mất đúng lúc
    cần nhất.
    """
    from artemis.knowledge.prepare import failed_checks

    observer = Workflow(
        id="auth.current_account",
        app_id="hunonic",
        name="Xem tài khoản đang đăng nhập",
        description="Đọc số điện thoại/email của tài khoản đang đăng nhập.",
        aliases=["tài khoản đang đăng nhập"],
        steps=[
            {
                "action": "tap",
                "target": "Tab 'Menu' ở ngoài cùng bên phải của thanh điều hướng dưới cùng",
                "expected_state": "Màn hình Menu hiện ra, khối tài khoản ở trên cùng",
            },
            {
                "action": "assert",
                "target": "Số điện thoại hoặc email ở khối tài khoản trên cùng màn Menu",
                "expected_state": "Đọc được đúng số điện thoại/email của tài khoản đang đăng nhập",
            },
        ],
        metadata={
            "state_dimension": {
                "id": "account",
                "label": "tài khoản đang đăng nhập",
                "value_hint": "tài khoản",
                "set_workflows": ["auth.switch_account"],
                "clear_workflow": "auth.logout",
            }
        },
        revision_no=1,
        updated_at="2026-10-28T10:00:00",
    )
    service = FakeService([SWITCH_ACCOUNT, observer])

    outcome = prepare_login(
        service,
        synthesizer(ACCOUNT_PLAN),
        prompt="đăng nhập tài khoản 0999 rồi vào màn ngữ cảnh chạy khi ấn",
    )

    prompt = outcome.enhanced_prompt
    assert "Cách quan sát tài khoản đang đăng nhập" in prompt
    assert "- Bước 1: tap: Tab 'Menu' ở ngoài cùng bên phải" in prompt
    assert "Số điện thoại hoặc email ở khối tài khoản trên cùng màn Menu" in prompt
    assert "theo KB auth.current_account" in prompt
    assert failed_checks(outcome.checks) == []

