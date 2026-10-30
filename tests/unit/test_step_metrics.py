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

"""Số đo hiệu năng theo lượt (P1-4).

Yêu cầu: **không** gọi thêm LLM để đo, **không** đổi hành vi agent; phân loại rõ action bị gate chặn
/ đã áp dụng từ trước / dispatch thành công / dispatch thất bại / lượt không tiến triển.
"""

from types import SimpleNamespace

from artemis.utils.step_metrics import (
    StepMetrics,
    add_llm_usage,
    count_gate_bounce,
    count_plan_self_heal,
    emit_structured,
    finish_execution,
    finish_operator_turn,
    metrics_for,
)


def _state(**extra) -> SimpleNamespace:
    return SimpleNamespace(step_metrics=None, **extra)


def _response(input_tokens: int, output_tokens: int, cached: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_token_details": {"cache_read": cached},
        }
    )


# ── token: không đếm hai lần ────────────────────────────────────────────
def test_usage_is_counted_once_per_llm_response():
    state = _state()

    add_llm_usage(state, _response(1000, 50, 400))

    assert state.step_metrics.llm_calls == 1
    assert state.step_metrics.input_tokens == 1000
    assert state.step_metrics.output_tokens == 50
    assert state.step_metrics.cached_tokens == 400


def test_finishing_a_step_does_not_add_tokens_again():
    state = _state()
    add_llm_usage(state, _response(1000, 50, 400))

    finish_execution(state, dispatched=1, already_applied=0, failed=0)

    # Chốt bước chỉ cộng **kết quả action**, không đụng token.
    assert state.step_metrics.input_tokens == 1000
    assert state.step_metrics.output_tokens == 50
    assert state.step_metrics.cached_tokens == 400
    assert state.step_metrics.llm_calls == 1


def test_a_turn_with_many_llm_calls_sums_every_call():
    state = _state()

    for _ in range(3):
        add_llm_usage(state, _response(17_000, 300, 14_000))

    metrics = state.step_metrics
    assert metrics.llm_calls == 3
    assert metrics.input_tokens == 51_000
    assert metrics.output_tokens == 900
    assert metrics.cached_tokens == 42_000


def test_a_response_without_usage_metadata_still_counts_the_call():
    state = _state()

    add_llm_usage(state, SimpleNamespace())

    assert state.step_metrics.llm_calls == 1
    assert state.step_metrics.input_tokens == 0


# ── gate bounce / self-heal / phân loại action ──────────────────────────
def test_gate_bounce_and_self_heal_are_counted_separately():
    state = _state()

    count_gate_bounce(state)
    count_gate_bounce(state)
    count_plan_self_heal(state)
    finish_operator_turn(state, action="blocked")

    metrics = state.step_metrics
    assert metrics.gate_bounces == 2
    assert metrics.actions_blocked_by_gate == 2
    assert metrics.plan_self_heals == 1
    assert metrics.action == "blocked"
    assert metrics.no_progress is True


def test_already_applied_is_never_counted_as_failed():
    state = _state()

    finish_execution(state, dispatched=0, already_applied=1, failed=0)

    metrics = state.step_metrics
    assert metrics.actions_already_applied == 1
    assert metrics.actions_failed == 0
    assert metrics.action == "already_applied"
    assert metrics.no_progress is True  # không tiến triển mới, nhưng KHÔNG phải lỗi


def test_dispatched_action_is_progress():
    state = _state()

    finish_execution(state, dispatched=1, already_applied=0, failed=0)

    assert state.step_metrics.action == "dispatched"
    assert state.step_metrics.no_progress is False


def test_failed_action_is_reported_as_failed():
    state = _state()

    finish_execution(state, dispatched=0, already_applied=0, failed=1)

    assert state.step_metrics.action == "failed"
    assert state.step_metrics.actions_failed == 1
    assert state.step_metrics.no_progress is True


# ── an toàn: đo không được đổi hành vi ──────────────────────────────────
def test_metrics_never_raise_even_with_a_broken_engine():
    class Boom:
        def record_trace(self, **_kwargs):
            raise RuntimeError("engine down")

    ctx = SimpleNamespace(data_engine=Boom())
    state = _state(current_step_id=None)

    emit_structured(ctx, state)  # không được ném


def test_structured_event_carries_every_required_field():
    captured: dict = {}

    class Engine:
        def record_trace(self, **kwargs):
            captured.update(kwargs)
            return "trace-id"

    ctx = SimpleNamespace(data_engine=Engine())
    state = _state(current_step_id=None)
    add_llm_usage(state, _response(1200, 30, 800))
    count_gate_bounce(state)

    emit_structured(ctx, state)

    assert captured["type"] == "metrics"
    assert captured["name"] == "step_metrics"
    payload = captured["payload"]
    for field in (
        "wall_ms",
        "llm_calls",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "gate_bounces",
        "plan_self_heals",
        "validation_ms",
        "pixel_validation_ms",
        "actions_dispatched",
        "actions_already_applied",
        "actions_failed",
        "no_progress",
        "action",
    ):
        assert field in payload, field
    assert payload["llm_calls"] == 1 and payload["cached_tokens"] == 800


def test_log_line_is_readable_and_complete():
    metrics = StepMetrics(
        llm_calls=3,
        input_tokens=51_000,
        cached_tokens=42_000,
        gate_bounces=2,
        plan_self_heals=1,
        pixel_validation_ms=5200,
        actions_dispatched=1,
        wall_ms=12_345,
    )

    line = metrics.log_line(step=23)

    assert line.startswith("[step-metrics] step=23")
    for token in (
        "wall_ms=12345",
        "llm_calls=3",
        "in_tok=51000",
        "cached=42000",
        "bounces=2",
        "self_heals=1",
        "pixel_ms=5200",
        "action=",
    ):
        assert token in line, token


def test_metrics_object_is_per_turn():
    """Mỗi lượt một bộ đếm: `metrics_for` tạo mới khi state chưa có, giữ nguyên khi đã có."""
    state = _state()

    first = metrics_for(state)
    add_llm_usage(state, _response(10, 1))
    assert metrics_for(state) is first

    state.step_metrics = StepMetrics()  # lượt mới
    assert metrics_for(state).llm_calls == 0


def test_a_bounced_turn_is_emitted_once_even_when_closed_early():
    """Lượt bị gate chặn thoát sớm: chốt tại chỗ chặn, và cuối lượt không phát lại lần hai."""
    state = _state(current_step_number=7)

    count_gate_bounce(state)
    finish_operator_turn(state, action="blocked")  # tại chỗ chặn
    again = finish_operator_turn(state, action="blocked")  # cuối lượt: bỏ qua

    assert again.emitted is True
    assert state.step_metrics.gate_bounces == 1
    assert state.step_metrics.action == "blocked"
