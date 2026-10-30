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

"""Integration test cho step_metrics (P1-4) — chạy trên **DataEngine thật**.

Vì sao cần lớp test này (chứ không chỉ unit test): phiên thật ``d6a68202`` cho thấy unit test
"xanh" mà số đo vẫn hỏng — bộ đếm gắn vào ``State`` (pydantic) bị từ chối nên mọi số cross-call về 0.
Lớp test này đi qua đúng holder thật (``ctx.data_engine``), đúng session thật, và kiểm cả file
``perf_events.jsonl`` — nơi dữ liệu có cấu trúc phải thực sự hạ cánh.
"""

from __future__ import annotations

import json
import logging

import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from artemis.context import ArtemisContext
from artemis.data_engine.engine import DataEngine
from artemis.utils.step_metrics import (
    add_llm_usage,
    add_validation,
    count_gate_bounce,
    count_plan_self_heal,
    finish_execution,
    finish_operator_turn,
    metrics_for,
    reset_turn,
)

METRICS_TAG = "[step-metrics]"


def _response(input_tokens: int, output_tokens: int, cached: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_token_details": {"cache_read": cached},
        }
    )


def _live_engine(tmp_path: Path) -> tuple[SimpleNamespace, DataEngine]:
    """Ctx + DataEngine **thật** với session thật (không LLM, không thiết bị)."""
    ctx = MagicMock(spec=ArtemisContext)
    setup = MagicMock()
    setup.traces_path = str(tmp_path / "traces")
    ctx.execution_setup = setup
    ctx.device = None
    engine = DataEngine(ctx)
    engine.start_session(goal="kiểm chứng số đo hiệu năng")
    ctx.data_engine = engine  # holder thật của lượt (đây chính là thứ phiên thật dùng)
    return ctx, engine


def _lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if METRICS_TAG in r.getMessage()]


def _perf_text(engine: DataEngine) -> str:
    files = list(Path(engine.global_base_dir).rglob("perf_events.jsonl"))
    return "\n".join(f.read_text(errors="replace") for f in files)


def test_operator_then_validator_share_one_holder_and_do_not_double_count(tmp_path, caplog):
    """Operator (3 lượt LLM) → Validator (validation + thực thi) trên **cùng** bộ đếm."""
    caplog.set_level(logging.INFO)
    ctx, engine = _live_engine(tmp_path)

    reset_turn(ctx)
    for _ in range(3):  # một lượt có nhiều LLM call
        add_llm_usage(ctx, _response(17_000, 300, 14_000))
    finish_operator_turn(ctx, action="dispatched")
    add_validation(ctx, total_ms=1200.0, pixel_ms=800.0)
    finish_execution(ctx, dispatched=1, already_applied=0, failed=0)

    metrics = metrics_for(engine)
    assert metrics.llm_calls == 3
    assert metrics.input_tokens == 51_000  # không nhân đôi
    assert metrics.output_tokens == 900
    assert metrics.cached_tokens == 42_000
    assert metrics.validation_ms == 1200.0 and metrics.pixel_validation_ms == 800.0
    assert metrics.actions_dispatched == 1 and metrics.action == "dispatched"
    assert metrics.no_progress is False


def test_step_number_and_wall_ms_come_from_the_engine(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    ctx, engine = _live_engine(tmp_path)

    reset_turn(ctx)
    finish_operator_turn(ctx, action="dispatched")

    line = _lines(caplog)[-1]
    assert f"step={engine.current_step_number}" in line
    assert "step=-" not in line
    assert metrics_for(engine).wall_ms > 0


def test_blocked_turn_emits_exactly_one_line_and_counts_the_bounce(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    ctx, engine = _live_engine(tmp_path)

    reset_turn(ctx)
    count_gate_bounce(ctx)
    count_plan_self_heal(ctx)
    finish_operator_turn(ctx, action="blocked")  # lượt thoát sớm: chốt tại chỗ chặn
    finish_operator_turn(ctx, action="blocked")  # cuối lượt: không phát lại

    assert len(_lines(caplog)) == 1
    metrics = metrics_for(engine)
    assert metrics.gate_bounces == 1 and metrics.actions_blocked_by_gate == 1
    assert metrics.plan_self_heals == 1 and metrics.action == "blocked"
    assert metrics.no_progress is True


def test_reset_turn_clears_counters_without_losing_previous_lines(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    ctx, engine = _live_engine(tmp_path)

    reset_turn(ctx)
    add_llm_usage(ctx, _response(1000, 10, 0))
    finish_operator_turn(ctx, action="dispatched")
    reset_turn(ctx)  # lượt mới

    assert metrics_for(engine).llm_calls == 0
    add_llm_usage(ctx, _response(2000, 20, 0))
    finish_operator_turn(ctx, action="dispatched")

    lines = _lines(caplog)
    assert len(lines) == 2  # không mất dòng nào, cũng không nhân đôi
    assert "in_tok=1000" in lines[0] and "in_tok=2000" in lines[1]


@pytest.mark.xfail(
    reason=(
        "BUG P1-4b đã đo được: record_trace(type=metrics, name=step_metrics) không hạ cánh trong"
        " perf_events.jsonl — phiên thật d6a68202 có 82 dòng [step-metrics] nhưng 0 event metrics"
        " trong 740 event. Dòng log người-đọc được vẫn đúng; kênh có cấu trúc thì chưa. Test này sẽ"
        " XPASS khi kênh đó được nối."
    ),
    strict=False,
)
def test_structured_event_lands_in_the_perf_file(tmp_path):
    from artemis.perf import recorder as perf_recorder

    if not perf_recorder.perf_enabled():
        pytest.skip("perf đang tắt trong môi trường test (ARTEMIS_PERF_LEVEL)")
    ctx, engine = _live_engine(tmp_path)

    reset_turn(ctx)
    add_llm_usage(ctx, _response(1200, 30, 800))
    finish_execution(ctx, dispatched=1, already_applied=0, failed=0)
    rec = perf_recorder.get_recorder()
    if rec is None:
        # Recorder chỉ được cài khi app/console chạy. Đường gọi `record_trace` đã được unit test
        # phủ (test_structured_event_carries_every_required_field); ở đây chỉ kiểm bản ghi thật.
        pytest.skip("không có perf recorder trong môi trường test")
    rec.close()

    perf = _perf_text(engine)
    assert "step_metrics" in perf
    payloads = [
        json.loads(line)["detail"]
        for line in perf.splitlines()
        if line.strip() and "step_metrics" in line and '"detail"' in line
    ]
    assert payloads, "event step_metrics phải có payload cấu trúc"
    assert any(p.get("llm_calls") == 1 and p.get("cached_tokens") == 800 for p in payloads)


def test_metrics_init_failure_is_logged_not_swallowed(caplog):
    """Holder từ chối gắn bộ đếm (đúng lỗi đã xảy ra với `State` pydantic) ⇒ phải có cảnh báo."""
    caplog.set_level(logging.INFO)

    class Frozen:
        __slots__ = ()

    metrics = metrics_for(Frozen())

    assert metrics.llm_calls == 0
    assert any("không gắn được bộ đếm" in r.getMessage() for r in caplog.records)
