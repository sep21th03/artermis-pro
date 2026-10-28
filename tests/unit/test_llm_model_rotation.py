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

"""Xoay model khi cạn quota — cơ chế, không phải giao diện.

Vì sao cần: quota của nhà cung cấp là **theo model**.  Đo trên máy thật 28/10/2026, bốn vai
trò (planner/operator/explorer/checker) cùng trỏ ``google:gemini-3.5-flash-lite``; quota Free
Tier của Google là 500 request/ngày/**mỗi model**, cạn trong một buổi chạy, và vì chỉ có một
model nên mọi lượt sau 429 cho tới khi hết ngày (``retryDelay`` 14269s ≈ 4 giờ).

Sáu tính chất được khoá ở đây:

1. Đọc được thời gian hồi mà nhà cung cấp nói (ba dạng đã gặp thật).
2. Quota ngày ≠ rate limit theo phút: chờ 60 giây hay chờ 4 giờ là hai quyết định khác nhau.
3. Lỗi quota mở mạch **ngay** với đúng thời gian đó, không cần đủ 3 lần liên tiếp.
4. Ứng viên đang nghỉ bị bỏ qua, lượt sau dùng model kế trong chuỗi.
5. Chuỗi đọc từ ``ARTEMIS_LLM_CHAIN_<VAI TRÒ>``; mục không ghi provider thì giữ provider của
   vai trò, nên ``_PROVIDER=google`` là cả chuỗi chạy trên Google.
6. Hết ứng viên khoẻ mạnh thì **không** chặn tác vụ — chọn cái sớm hồi nhất và ghi cảnh báo.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from artemis.llm.reliability import (
    DEFAULT_DAILY_COOLDOWN_S,
    DEFAULT_RATE_LIMIT_COOLDOWN_S,
    MAX_QUOTA_COOLDOWN_S,
    CircuitBreaker,
    Failure,
    FailureCategory,
    is_daily_quota,
    parse_retry_delay,
    quota_cooldown,
)
from artemis.llm.router import ModelEndpoint, ModelProvider
from artemis.services import llm as llm_service

#: Thân lỗi thật của Google (rút gọn) — đúng thứ đã làm ARTEMIS đứng ở 10:02 ngày 28/10/2026.
GOOGLE_DAILY_QUOTA = (
    "429 Too Many Requests. {'message': '{\"error\": {\"code\": 429, \"status\": "
    "\"RESOURCE_EXHAUSTED\", \"details\": [{\"quotaMetric\": "
    "\"generativelanguage.googleapis.com/generate_content_free_tier_requests\", "
    "\"quotaId\": \"GenerateRequestsPerDayPerProjectPerModel-FreeTier\", "
    "\"quotaValue\": \"500\"}, {\"retryDelay\": \"14269s\"}]}}'}"
)


def test_the_provider_reported_delay_is_read_from_the_error_text():
    assert parse_retry_delay(GOOGLE_DAILY_QUOTA) == 14269.0
    assert parse_retry_delay("Please retry in 3h57m49.580814965s.") == pytest.approx(
        14269.58, abs=0.1
    )
    assert parse_retry_delay("retry-after: 30") == 30.0
    # Không đọc được thì trả None để bên gọi dùng mặc định — không bịa một con số.
    assert parse_retry_delay("connection reset by peer") is None


def test_a_daily_quota_is_not_the_same_as_a_per_minute_rate_limit():
    assert is_daily_quota(GOOGLE_DAILY_QUOTA) is True
    assert is_daily_quota("429 rate limit exceeded, try again later") is False


def test_the_cooldown_uses_the_reported_delay_and_falls_back_sensibly():
    assert quota_cooldown(GOOGLE_DAILY_QUOTA) == 14269.0
    # Quota ngày mà không có thời gian hồi: nghỉ một giờ, không phải một phút.
    assert quota_cooldown("429 quota exceeded, requests per day limit") == DEFAULT_DAILY_COOLDOWN_S
    # Rate limit theo phút: nghỉ một phút.
    assert quota_cooldown("429 rate limit, slow down") == DEFAULT_RATE_LIMIT_COOLDOWN_S
    # Không phải lỗi quota thì không nghỉ: xoay model không cứu được lỗi sai key.
    assert quota_cooldown("401 Incorrect API key provided") is None
    assert quota_cooldown("400 invalid argument") is None


def test_a_reported_delay_is_capped_so_a_typo_cannot_park_a_model_for_days():
    huge = '429 RESOURCE_EXHAUSTED: quota exceeded, {"retryDelay": "999999s"}'
    assert quota_cooldown(huge) == MAX_QUOTA_COOLDOWN_S



def test_a_quota_error_opens_the_circuit_immediately():
    """Đếm đủ 3 lần mới mở mạch là sai: hai lần đầu rơi vào đúng cái quota vừa cạn."""
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=30.0)
    failure = Failure(FailureCategory.RATE_LIMIT, True, True)

    breaker.record_failure("google:gemini-3.5-flash-lite", failure, open_for=14269.0)

    assert breaker.allow("google:gemini-3.5-flash-lite") is False
    assert breaker.open_remaining("google:gemini-3.5-flash-lite") == pytest.approx(
        14269.0, abs=1.0
    )


def test_without_a_reported_delay_the_threshold_still_applies():
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=30.0)
    failure = Failure(FailureCategory.TIMEOUT, True, True)

    breaker.record_failure("google:x", failure)
    breaker.record_failure("google:x", failure)
    assert breaker.allow("google:x") is True

    breaker.record_failure("google:x", failure)
    assert breaker.allow("google:x") is False


def test_a_non_transient_error_never_opens_the_circuit():
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=30.0)
    failure = Failure(FailureCategory.AUTHENTICATION, False, True)

    breaker.record_failure("google:x", failure, open_for=3600.0)

    assert breaker.allow("google:x") is True


# ── Chọn ứng viên trong chuỗi ─────────────────────────────────────────────
def endpoint(model: str, provider: ModelProvider = ModelProvider.GOOGLE) -> ModelEndpoint:
    return ModelEndpoint(provider=provider, model_name=model)


def _rest(key: str, seconds: float) -> None:
    """Cho model ``key`` vào thời gian nghỉ, đúng như một lần 429 quota gây ra."""
    llm_service._ENDPOINT_BREAKER.record_failure(
        key, Failure(FailureCategory.RATE_LIMIT, True, True), open_for=seconds
    )


@pytest.fixture(autouse=True)
def _clean_breaker(monkeypatch):
    """Mạch điện là biến toàn cục; mỗi test dùng một cái sạch để không phụ thuộc thứ tự."""
    monkeypatch.setattr(
        llm_service, "_ENDPOINT_BREAKER", CircuitBreaker(threshold=3, cooldown_seconds=30.0)
    )


def test_the_first_healthy_candidate_is_used():
    candidates = [endpoint("a"), endpoint("b")]

    chosen = llm_service._pick_healthy(candidates, start=0, role="operator")

    assert chosen.model_name == "a"


def test_a_model_that_just_ran_out_of_quota_is_skipped():
    """Đây là cả tính năng: hết quota thì lượt sau dùng model kế, không chờ 4 giờ."""
    _rest("google:a", 14269.0)
    candidates = [endpoint("a"), endpoint("b"), endpoint("c")]

    chosen = llm_service._pick_healthy(candidates, start=0, role="operator")

    assert chosen.model_name == "b"


def test_the_fallback_path_starts_after_the_primary():
    candidates = [endpoint("a"), endpoint("b")]

    chosen = llm_service._pick_healthy(candidates, start=1, role="operator")

    assert chosen.model_name == "b"


def test_when_every_candidate_is_resting_the_soonest_one_is_used():
    """Không chặn tác vụ: rate limit 60 giây không được biến thành tác vụ chết."""
    _rest("google:a", 3600.0)
    _rest("google:b", 60.0)
    candidates = [endpoint("a"), endpoint("b")]

    chosen = llm_service._pick_healthy(candidates, start=0, role="operator")

    assert chosen.model_name == "b"



# ── Ghép chuỗi từ .env ────────────────────────────────────────────────────
def test_the_chain_is_read_from_the_role_env_var(monkeypatch):
    monkeypatch.setenv(
        "ARTEMIS_LLM_CHAIN_OPERATOR", "gemini-2.5-flash, google:gemini-3.1-flash-lite "
    )
    monkeypatch.setenv("ARTEMIS_LLM_CHAIN_PLANNER", "")

    assert llm_service._chain_specs("operator") == [
        "gemini-2.5-flash",
        "google:gemini-3.1-flash-lite",
    ]
    assert llm_service._chain_specs("planner") == []


def test_chain_entries_keep_the_role_provider_unless_they_say_otherwise(monkeypatch):
    """``_PROVIDER=google`` là cả chuỗi chạy Google — "ưu tiên Google" không phải viết lại."""
    monkeypatch.setenv(
        "ARTEMIS_LLM_CHAIN_OPERATOR", "gemini-2.5-flash,openai:ts/gemini-3.5-flash-lite"
    )
    ctx = SimpleNamespace(
        llm_config=SimpleNamespace(
            get_agent=lambda name: SimpleNamespace(
                provider="google", model="gemini-3.5-flash-lite", temperature=0.0, fallback=None
            )
        )
    )

    candidates = llm_service._endpoint_candidates(ctx, "operator", is_utils=False)

    assert [(str(item.provider), item.model_name) for item in candidates] == [
        ("google", "gemini-3.5-flash-lite"),
        ("google", "gemini-2.5-flash"),
        ("openai", "ts/gemini-3.5-flash-lite"),
    ]


def test_the_config_fallback_stays_last_and_duplicates_are_dropped(monkeypatch):
    monkeypatch.setenv("ARTEMIS_LLM_CHAIN_OPERATOR", "gemini-2.5-flash")
    ctx = SimpleNamespace(
        llm_config=SimpleNamespace(
            get_agent=lambda name: SimpleNamespace(
                provider="google",
                model="gemini-3.5-flash-lite",
                temperature=0.0,
                fallback=SimpleNamespace(provider="google", model="gemini-2.5-flash"),
            )
        )
    )

    candidates = llm_service._endpoint_candidates(ctx, "operator", is_utils=False)

    # `gemini-2.5-flash` khai ở cả chuỗi env và fallback config ⇒ chỉ còn một mục.
    assert [item.model_name for item in candidates] == ["gemini-3.5-flash-lite", "gemini-2.5-flash"]


def test_a_role_without_any_other_candidate_still_reports_no_fallback(monkeypatch):
    # `.env` thật có khai chuỗi cho operator; test này nói về vai trò *không* có ứng viên nào
    # khác, nên phải tự lập với môi trường.
    monkeypatch.delenv("ARTEMIS_LLM_CHAIN_OPERATOR", raising=False)
    ctx = SimpleNamespace(
        llm_config=SimpleNamespace(
            get_agent=lambda name: SimpleNamespace(
                provider="google", model="gemini-3.5-flash-lite", temperature=0.0, fallback=None
            )
        )
    )

    with pytest.raises(ValueError, match="has no fallback"):
        llm_service._resolve_endpoint(ctx, "operator", use_fallback=True)


def test_the_endpoint_key_matches_the_one_used_at_call_time():
    """Lệch khoá thì mạch mở cho model này mà lượt gọi ghi vào model khác — xoay không xảy ra."""
    from artemis.services.llm import RobustChatModelWrapper

    target = endpoint("gemini-2.5-flash")
    wrapper = RobustChatModelWrapper(base_model=None, ctx=None, endpoint=target)

    assert llm_service._endpoint_key_of(target) == wrapper._endpoint_key()
