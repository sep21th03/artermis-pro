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

"""Tổng hợp số liệu hiệu năng: exclusive time, % runtime, idle, bottleneck.

Điều quan trọng nhất ở tầng này là **không nói dối**: cộng theo exclusive để không đếm
trùng span lồng nhau, để ``None`` (N/A) cho công đoạn không đo được, và chỉ kết luận
bottleneck khi có bằng chứng số.
"""

from __future__ import annotations

from artemis.perf import (
    PerfEvent,
    PerfStage,
    build_idle,
    build_report,
    build_stage_table,
    build_timeline,
    detect_findings,
    detect_loops,
    redact,
)


def ev(
    span_id: str,
    stage,
    duration_ms: float | None,
    *,
    started_at: float = 1000.0,
    parent: str | None = None,
    status: str = "success",
    **kwargs,
) -> PerfEvent:
    return PerfEvent(
        session_id="s",
        span_id=span_id,
        parent_span_id=parent,
        event_name=kwargs.pop("event_name", "e"),
        stage=str(stage),
        started_at=started_at,
        ended_at=(started_at + duration_ms / 1000.0) if duration_ms is not None else None,
        duration_ms=duration_ms,
        status=status,
        **kwargs,
    )


# ── Bảng công đoạn ───────────────────────────────────────────────────────
def test_exclusive_time_does_not_double_count_nested_spans():
    """Operator LLM nằm trong node Operator: cột Total phải trừ phần con."""
    events = [
        ev("node", PerfStage.AGENT, 1000.0),
        ev("llm", PerfStage.OPERATOR_LLM, 700.0, parent="node"),
        ev("tap", PerfStage.ADB_ACTION, 200.0, parent="node"),
    ]

    rows = {row["stage"]: row for row in build_stage_table(events, wall_clock_ms=1000.0)}

    assert rows[str(PerfStage.AGENT)]["total_ms"] == 100.0  # 1000 - 700 - 200
    assert rows[str(PerfStage.OPERATOR_LLM)]["total_ms"] == 700.0
    assert rows[str(PerfStage.ADB_ACTION)]["total_ms"] == 200.0
    assert rows[str(PerfStage.OPERATOR_LLM)]["runtime_share_pct"] == 70.0


def test_a_stage_without_measurements_reports_na_not_zero():
    events = [ev("x", PerfStage.PLANNER_LLM, None, status="running")]

    row = build_stage_table(events, wall_clock_ms=1000.0)[0]

    assert row["calls"] == 1
    assert row["total_ms"] is None and row["avg_ms"] is None and row["max_ms"] is None
    assert row["runtime_share_pct"] is None


def test_the_session_and_action_outcome_spans_are_not_stage_rows():
    """Hai loại span *bao* này nếu cộng vào bảng sẽ đếm trùng và che công đoạn thật."""
    events = [
        ev("sess", PerfStage.SESSION, 90000.0, started_at=1000.0, event_name="session_end"),
        ev("root", PerfStage.AGENT, 89000.0, started_at=1000.0),
        ev(
            "outcome",
            PerfStage.ADB_ACTION,
            800.0,
            started_at=1000.0,
            parent="root",
            event_name="action_outcome",
        ),
        ev("tap", PerfStage.ADB_ACTION, 120.0, started_at=1000.0, parent="root"),
    ]

    rows = {row["stage"]: row for row in build_stage_table(events, wall_clock_ms=90000.0)}

    assert str(PerfStage.SESSION) not in rows
    # action_outcome bị loại khỏi bảng, nhưng tap thì vẫn được cộng.
    assert rows[str(PerfStage.ADB_ACTION)]["calls"] == 1
    assert rows[str(PerfStage.ADB_ACTION)]["total_ms"] == 120.0
    assert rows[str(PerfStage.AGENT)]["total_ms"] == 88880.0


def test_errors_are_counted_per_stage():
    events = [
        ev("a", PerfStage.ADB_ACTION, 10.0, status="error"),
        ev("b", PerfStage.ADB_ACTION, 20.0),
    ]

    row = build_stage_table(events, wall_clock_ms=100.0)[0]

    assert row["calls"] == 2 and row["errors"] == 1


# ── Idle ─────────────────────────────────────────────────────────────────
def test_idle_is_the_wall_clock_not_covered_by_any_root_span():
    events = [
        ev("a", PerfStage.SCREENSHOT, 1000.0, started_at=1000.0),
        ev("b", PerfStage.OPERATOR_LLM, 1000.0, started_at=1004.0),
    ]

    idle = build_idle(events, window=(1000.0, 1006.0))

    assert idle["total_ms"] == 4000.0  # 3s giữa hai span + 1s sau span cuối
    assert idle["gap_count"] == 2
    assert idle["gaps"][0]["duration_ms"] == 3000.0


def test_an_open_root_span_prevents_fake_idle_while_the_session_runs():
    """Đang chạy: span gốc chưa đóng vẫn phải bao trùm thời gian đã trôi qua.

    Không có quy tắc này thì mọi giây của session đang chạy đều bị báo là
    ``Waiting/Idle`` — đúng cái nhìn sai lệch mà lớp đo sinh ra để xoá.
    """
    running = PerfEvent(
        session_id="s",
        span_id="root",
        event_name="FlashRunner",
        stage=str(PerfStage.AGENT),
        started_at=1000.0,
        ended_at=None,
        duration_ms=None,
        status="running",
    )
    child = ev("llm", PerfStage.OPERATOR_LLM, 1000.0, started_at=1000.5, parent="root")

    idle = build_idle([running, child], window=(1000.0, 1002.0))

    assert idle["total_ms"] == 0.0


def test_nested_spans_do_not_create_fake_idle():
    events = [
        ev("node", PerfStage.AGENT, 500.0, started_at=1000.0),
        ev("llm", PerfStage.OPERATOR_LLM, 500.0, started_at=1000.0, parent="node"),
    ]

    idle = build_idle(events, window=(1000.0, 1000.5))

    assert idle["total_ms"] == 0.0


# ── Vòng lặp & "không tiến triển" ────────────────────────────────────────
def test_repeated_identical_taps_without_ui_change_are_a_loop():
    events = [
        ev(
            f"tap{i}",
            PerfStage.ADB_ACTION,
            100.0,
            started_at=1000.0 + i,
            action_type="tap",
            ui_changed=False,
            detail={"x": 540, "y": 1200},
        )
        for i in range(3)
    ]

    loops = detect_loops(events)

    assert len(loops) == 1
    assert loops[0]["count"] == 3
    assert loops[0]["action_type"] == "tap"


def test_a_single_unchanged_screen_is_not_a_loop():
    """Màn hình không đổi một lần là bình thường (app đang tải) — không phải lỗi."""
    events = [
        ev(
            "tap1",
            PerfStage.ADB_ACTION,
            100.0,
            action_type="tap",
            ui_changed=False,
            detail={"x": 10, "y": 10},
        )
    ]

    assert detect_loops(events) == []


def test_a_ui_change_breaks_the_loop_chain():
    events = [
        ev(
            f"tap{i}",
            PerfStage.ADB_ACTION,
            100.0,
            started_at=1000.0 + i,
            action_type="tap",
            ui_changed=(i == 2),
            detail={"x": 5, "y": 5},
        )
        for i in range(4)
    ]

    assert detect_loops(events) == []


# ── Nhận định ────────────────────────────────────────────────────────────
def test_findings_quote_measured_numbers_only():
    events = [
        ev("llm", PerfStage.OPERATOR_LLM, 9000.0, model="m", total_tokens=100),
        ev("tap", PerfStage.ADB_ACTION, 100.0, action_type="tap", ui_changed=True),
    ]
    stages = build_stage_table(events, wall_clock_ms=10000.0)
    idle = build_idle(events, window=(1000.0, 1010.0))

    findings = detect_findings(events, stages=stages, idle=idle, wall_clock_ms=10000.0, loops=[])
    codes = {finding["code"] for finding in findings}

    assert "top_stages" in codes
    assert "stage_dominates" in codes  # 90% thời gian nằm ở Operator LLM
    dominates = next(f for f in findings if f["code"] == "stage_dominates")
    assert dominates["metric"]["share_pct"] == 90.0


def test_retry_time_is_reported_as_a_finding_with_evidence():
    events = [
        ev("llm", PerfStage.OPERATOR_LLM, 20000.0, retry_count=3, retry_wait_ms=18000.0),
    ]
    stages = build_stage_table(events, wall_clock_ms=20000.0)

    findings = detect_findings(
        events,
        stages=stages,
        idle={"gaps": [], "total_ms": 0.0, "share_pct": 0.0, "gap_count": 0},
        wall_clock_ms=20000.0,
        loops=[],
    )

    retry = next(f for f in findings if f["code"] == "retry_overhead")
    assert retry["metric"]["retry_wait_ms"] == 18000.0
    assert retry["severity"] == "high"


def test_no_findings_without_data():
    assert detect_findings([], stages=[], idle={}, wall_clock_ms=None, loops=[]) == []


# ── Báo cáo & timeline ───────────────────────────────────────────────────
def test_report_counts_are_na_when_there_are_no_run_events():
    report = build_report([])

    assert report["summary"]["actions"] is None
    assert report["summary"]["llm_calls"] is None
    assert report["summary"]["total_session_ms"] is None
    assert report["stages"] == []


def test_report_separates_console_prepare_events_from_the_run():
    prep = ev("prep", PerfStage.RAG_PREPARE, 800.0)
    prep.phase = "prepare"
    events = [prep, ev("run", PerfStage.OPERATOR_LLM, 500.0, started_at=2000.0)]

    report = build_report(events, session_id="s")

    assert report["summary"]["run_events"] == 1
    assert report["summary"]["prepare_events"] == 1
    measured = [row["stage"] for row in report["stages"] if row["total_ms"]]
    assert measured == [str(PerfStage.OPERATOR_LLM)]
    assert report["prepare"]["stages"]


def test_timeline_orders_by_start_and_shows_the_gap_before_each_row():
    events = [
        ev("b", PerfStage.ADB_ACTION, 100.0, started_at=1002.0, action_type="tap"),
        ev("a", PerfStage.OPERATOR_LLM, 1000.0, started_at=1000.0, model="m"),
    ]

    rows = build_timeline(events)

    assert [row["span_id"] for row in rows] == ["a", "b"]
    assert rows[0]["gap_before_ms"] is None
    assert rows[1]["gap_before_ms"] == 1000.0


def test_report_notes_parallel_coverage_instead_of_hiding_it():
    """Hai span gốc chạy song song: % runtime cộng lại có thể vượt 100%, phải nói rõ."""
    events = [
        ev("shot", PerfStage.SCREENSHOT, 1000.0, started_at=1000.0),
        ev("hier", PerfStage.UI_HIERARCHY, 1000.0, started_at=1000.0),
    ]

    report = build_report(events, window=(1000.0, 1000.6))

    assert report["summary"]["coverage_pct"] is not None
    assert report["summary"]["coverage_pct"] > 105
    assert "song song" in report["coverage_note"]


# ── Che bí mật ───────────────────────────────────────────────────────────
def test_redaction_removes_secrets_and_long_text():
    payload = {
        "api_key": "sk-live-123",
        "Authorization": "Bearer abc",
        "password": "hunter2",
        "prompt": "x" * 500,
        "nested": {"access_token": "t0ken", "count": 3},
    }

    cleaned = redact(payload)

    assert cleaned["api_key"] == "<redacted>"
    assert cleaned["Authorization"] == "<redacted>"
    assert cleaned["password"] == "<redacted>"
    assert cleaned["prompt"].startswith("<str len=")
    assert cleaned["nested"]["access_token"] == "<redacted>"
    assert cleaned["nested"]["count"] == 3
