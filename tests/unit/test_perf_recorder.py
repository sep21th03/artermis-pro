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

"""Lớp đo hiệu năng: ghi span, tổng hợp, phát hiện bottleneck.

Bốn tính chất được khoá ở đây:

1. **Không đo được thì là N/A, không phải 0** — một số 0 giả sẽ chảy vào bảng tổng hợp
   và vào kết luận bottleneck.
2. **Exclusive time** — span lồng nhau không bị cộng hai lần.
3. **Bí mật không vào log** — API key, mật khẩu, prompt dài, nội dung đã gõ.
4. **Không làm gãy task** — mọi lỗi đo đều bị nuốt, và khi tắt thì không có overhead.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from artemis.perf import (
    PerfEvent,
    PerfRecorder,
    PerfStage,
    build_idle,
    build_report,
    build_stage_table,
    build_timeline,
    detect_loops,
    iter_new_events,
    perf_span,
    perf_span_sync,
    read_events,
    record_event,
    redact,
    set_recorder,
)
from artemis.perf import context as perf_context


@pytest.fixture
def recorder(tmp_path: Path) -> PerfRecorder:
    rec = PerfRecorder("sess-1", path=tmp_path / "sess-1" / "perf_events.jsonl", enabled=True)
    set_recorder(rec)
    yield rec
    set_recorder(None)


def make_event(**overrides) -> PerfEvent:
    base = dict(
        session_id="sess-1",
        span_id=overrides.pop("span_id", "span-1"),
        event_name="unit",
        stage=str(PerfStage.ADB_ACTION),
        started_at=1000.0,
        ended_at=1000.5,
        duration_ms=500.0,
    )
    base.update(overrides)
    return PerfEvent(**base)


# ── Ghi & đọc ────────────────────────────────────────────────────────────
def test_a_span_is_written_to_jsonl_with_a_measured_duration(recorder, tmp_path):
    async def run():
        async with perf_span(PerfStage.OPERATOR_LLM, "llm_call", role="operator") as span:
            span.set(model="m", input_tokens=10, output_tokens=2, total_tokens=12)
            await asyncio.sleep(0.01)

    asyncio.run(run())

    events = read_events(recorder.path)
    assert len(events) == 1
    event = events[0]
    assert event.stage == PerfStage.OPERATOR_LLM
    assert event.agent_role == "operator"
    assert event.duration_ms is not None and event.duration_ms >= 10
    assert event.started_at is not None and event.ended_at is not None
    assert event.total_tokens == 12


def test_events_are_child_of_the_open_span(recorder):
    async def run():
        async with perf_span(PerfStage.AGENT, "operator_node", role="operator") as outer:
            record_event(PerfStage.ADB_ACTION, "adb_tap", duration_ms=5.0, action_type="tap")
            return outer

    outer = asyncio.run(run())

    events = {event.event_name: event for event in recorder.snapshot()}
    # Sự kiện rời nằm trong span đang mở là span con; nhờ đó thời gian retry/backoff
    # không bị tính hai lần khi tổng hợp theo exclusive time.
    assert events["adb_tap"].parent_span_id == outer.event.span_id
    assert events["operator_node"].parent_span_id is None


def test_a_span_that_raises_is_recorded_as_failed(recorder):
    async def run():
        async with perf_span(PerfStage.ADB_ACTION, "adb_tap"):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(run())

    event = recorder.snapshot()[-1]
    assert event.status == "error"
    assert event.error_type == "RuntimeError"
    assert event.duration_ms is not None


def test_no_recorder_means_no_overhead_and_no_crash():
    set_recorder(None)

    async def run():
        async with perf_span(PerfStage.ADB_ACTION, "adb_tap") as span:
            assert span is None
            return "ok"

    assert asyncio.run(run()) == "ok"
    assert record_event(PerfStage.ADB_ACTION, "adb_tap", duration_ms=1.0) is None


def test_disabled_recorder_writes_nothing(tmp_path):
    rec = PerfRecorder("s", path=tmp_path / "p.jsonl", enabled=False)
    set_recorder(rec)
    try:
        with perf_span_sync(PerfStage.ADB_ACTION, "adb_tap"):
            pass
    finally:
        set_recorder(None)
    assert rec.written == 0
    assert not rec.path.exists()


def test_a_broken_path_never_breaks_the_caller(tmp_path):
    rec = PerfRecorder("s", path=tmp_path / "khong-ton-tai" / "sub" / "p.jsonl", enabled=True)
    rec.path = tmp_path / "p.jsonl"
    rec._handle = None
    # Thư mục cha là một *file* → không tạo được thư mục; lớp đo phải nuốt lỗi.
    (tmp_path / "blocked").write_text("x", encoding="utf-8")
    rec.path = tmp_path / "blocked" / "p.jsonl"
    set_recorder(rec)
    try:
        record_event(PerfStage.ADB_ACTION, "adb_tap", duration_ms=1.0)
    finally:
        set_recorder(None)
    assert rec.written == 0


def test_iter_new_events_resumes_from_an_offset(recorder):
    record_event(PerfStage.ADB_ACTION, "adb_tap", duration_ms=1.0)
    first, offset = iter_new_events(recorder.path, 0)
    assert len(first) == 1

    record_event(PerfStage.ADB_ACTION, "adb_swipe", duration_ms=2.0)
    second, offset2 = iter_new_events(recorder.path, offset)
    assert [event.event_name for event in second] == ["adb_swipe"]
    assert offset2 > offset

    third, _ = iter_new_events(recorder.path, offset2)
    assert third == []
