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

"""Tổng hợp sự kiện hiệu năng: bảng công đoạn, timeline, phát hiện bottleneck.

**Hai khái niệm phải tách bạch, nếu không bảng số liệu sẽ vô nghĩa:**

* **Inclusive** — tổng thời gian của một span *kể cả* các span con.  Một lượt Operator
  bao gồm lượt gọi LLM và các thao tác ADB bên trong nó.
* **Exclusive** — thời gian của riêng span đó, đã trừ các span con.

Bảng Performance cộng theo **exclusive**, nên không đếm trùng thời gian lồng nhau:
``Planner LLM`` là span lá nên exclusive bằng inclusive, còn ``Agent node`` chỉ còn phần
thời gian thật sự thuộc về nó.  ``% runtime`` lấy exclusive chia cho wall-clock của
session (chỉ tính phase ``run``), nên tổng các dòng không vượt quá 100%.
"""

from __future__ import annotations

import time
from collections import defaultdict
from statistics import mean
from typing import Any, Iterable, Sequence

from artemis.perf.models import (
    STAGE_LABELS,
    STATUS_SUCCESS,
    PerfEvent,
    PerfStage,
)
from artemis.perf.recorder import dedupe_events

#: Ngưỡng coi một công đoạn là "đáng chú ý" khi chiếm phần lớn thời gian chạy.
BOTTLENECK_SHARE = 0.25
#: Số lần một hành động lặp y hệt mà UI không đổi thì coi là nghi vấn vòng lặp.
REPEAT_THRESHOLD = 3
#: Số công đoạn tốn thời gian nhất đưa vào báo cáo.
TOP_STAGES = 5


def _ms(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


def _fmt_ms(value: float | None) -> str:
    """Chuỗi hiển thị; ``None`` ⇒ ``N/A`` (không có số đo, KHÔNG phải 0)."""
    if value is None:
        return "N/A"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def _exclusive_ms(events: Sequence[PerfEvent]) -> dict[str, float | None]:
    """``span_id`` → thời gian riêng (đã trừ span con); ``None`` khi span không có số đo."""
    children: dict[str, float] = defaultdict(float)
    by_id: dict[str, PerfEvent] = {}
    for event in events:
        by_id[event.span_id] = event
    for event in events:
        if event.parent_span_id and event.duration_ms is not None:
            children[event.parent_span_id] += float(event.duration_ms)

    exclusive: dict[str, float | None] = {}
    for event in events:
        if event.duration_ms is None:
            exclusive[event.span_id] = None
            continue
        own = float(event.duration_ms) - children.get(event.span_id, 0.0)
        exclusive[event.span_id] = max(0.0, own)
    return exclusive


def _is_run(event: PerfEvent) -> bool:
    return event.phase == "run"


#: Trạng thái *không* phải lỗi của một span (``completed`` là trạng thái kết thúc hợp lệ
#: của session, không phải sự cố).
OK_STATUSES: frozenset[str] = frozenset({STATUS_SUCCESS, "running", "completed"})


def counts_toward_stage_table(event: PerfEvent) -> bool:
    """Sự kiện này có được cộng vào bảng công đoạn không.

    Hai loại bị loại khỏi bảng (vẫn hiện trong timeline):

    * ``SESSION`` — span của cả session; nếu cộng vào thì nó luôn chiếm ~100% và che mất
      mọi công đoạn thật.
    * ``action_outcome`` — span *bao* đo độ trễ xác minh UI sau một hành động, nên thời
      gian của nó chứa cả settling/screenshot/hierarchy bên trong.  Cộng nó vào cùng các
      span con là đếm trùng, và ``% runtime`` sẽ vượt 100% một cách vô nghĩa.
    """
    if str(event.stage) == str(PerfStage.SESSION):
        return False
    return event.event_name != "action_outcome"


def build_stage_table(
    events: Sequence[PerfEvent],
    *,
    wall_clock_ms: float | None = None,
    include_prepare: bool = False,
) -> list[dict[str, Any]]:
    """Bảng công đoạn: calls / total / avg / max / errors / % runtime (theo exclusive)."""
    scope = [
        event
        for event in events
        if (include_prepare or _is_run(event)) and counts_toward_stage_table(event)
    ]
    exclusive = _exclusive_ms(scope)

    grouped: dict[str, list[PerfEvent]] = defaultdict(list)
    for event in scope:
        grouped[str(event.stage)].append(event)

    rows: list[dict[str, Any]] = []
    for stage, group in grouped.items():
        measured = [e for e in group if e.duration_ms is not None]
        exclusive_values = [
            exclusive[e.span_id] for e in measured if exclusive.get(e.span_id) is not None
        ]
        total_exclusive = sum(exclusive_values) if exclusive_values else None
        total_inclusive = (
            sum(float(e.duration_ms) for e in measured if e.duration_ms is not None)
            if measured
            else None
        )
        errors = sum(1 for e in group if e.status not in OK_STATUSES)
        share = (
            (total_exclusive / wall_clock_ms * 100.0)
            if (total_exclusive is not None and wall_clock_ms)
            else None
        )
        rows.append(
            {
                "stage": stage,
                "label": STAGE_LABELS.get(stage, stage),
                "calls": len(group),
                "measured_calls": len(measured),
                "total_ms": _ms(total_exclusive),
                "inclusive_ms": _ms(total_inclusive),
                "avg_ms": _ms(mean(exclusive_values)) if exclusive_values else None,
                "max_ms": _ms(max(exclusive_values)) if exclusive_values else None,
                "errors": errors,
                "runtime_share_pct": round(share, 2) if share is not None else None,
                "input_tokens": _sum_field(group, "input_tokens"),
                "output_tokens": _sum_field(group, "output_tokens"),
                "retry_count": _sum_field(group, "retry_count"),
            }
        )

    # Sắp theo thời gian giảm dần — dòng đáng nhìn nhất nằm trên cùng.
    rows.sort(key=lambda row: (row["total_ms"] is None, -(row["total_ms"] or 0.0)))
    return rows


def _sum_field(events: Iterable[PerfEvent], field: str) -> int | None:
    values = [getattr(event, field) for event in events if getattr(event, field) is not None]
    return int(sum(values)) if values else None


def build_timeline(events: Sequence[PerfEvent]) -> list[dict[str, Any]]:
    """Timeline theo thứ tự thực tế, kèm thời gian chờ giữa các bước."""
    ordered = sorted(
        events,
        key=lambda event: event.started_at if event.started_at is not None else 0.0,
    )
    step_numbers: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    previous_end: float | None = None

    for index, event in enumerate(ordered, start=1):
        if event.step_id:
            step_numbers.setdefault(str(event.step_id), len(step_numbers) + 1)
        gap_ms: float | None = None
        if previous_end is not None and event.started_at is not None:
            gap_ms = _ms(max(0.0, (event.started_at - previous_end) * 1000.0))
        if event.ended_at is not None:
            previous_end = max(previous_end or 0.0, event.ended_at)

        rows.append(
            {
                "index": index,
                "step": step_numbers.get(str(event.step_id)) if event.step_id else None,
                "step_id": event.step_id,
                "span_id": event.span_id,
                "parent_span_id": event.parent_span_id,
                "request_id": event.request_id,
                "correlation_id": event.correlation_id,
                "stage": event.stage,
                "stage_label": STAGE_LABELS.get(event.stage, event.stage),
                "event_name": event.event_name,
                "agent_role": event.agent_role,
                "action_type": event.action_type,
                "started_at": event.started_at,
                "duration_ms": _ms(event.duration_ms),
                "duration_text": _fmt_ms(event.duration_ms),
                "status": event.status,
                "model": event.model,
                "provider": event.provider,
                "endpoint": event.endpoint,
                "input_tokens": event.input_tokens,
                "output_tokens": event.output_tokens,
                "total_tokens": event.total_tokens,
                "retry_count": event.retry_count,
                "retry_wait_ms": _ms(event.retry_wait_ms),
                "ttft_ms": _ms(event.ttft_ms),
                "http_status": event.http_status,
                "error_type": event.error_type,
                "error_code": event.error_code,
                "ui_fingerprint": event.ui_fingerprint,
                "ui_changed": event.ui_changed,
                "iteration": event.iteration,
                "gap_before_ms": gap_ms,
                "detail": event.detail,
            }
        )
    return rows


def _merged_intervals(
    events: Sequence[PerfEvent], *, window_end: float | None = None
) -> list[tuple[float, float]]:
    """Hợp các khoảng thời gian của span gốc (không có cha) — cơ sở tính idle.

    Span gốc **đang mở** (``ended_at is None``, session vẫn đang chạy) được coi là kéo
    dài tới ``window_end``: nếu bỏ qua nó, toàn bộ thời gian đang chạy sẽ bị báo nhầm là
    ``Waiting/Idle``.
    """
    spans: list[tuple[float, float]] = []
    for event in events:
        if event.parent_span_id or event.started_at is None:
            continue
        end = event.ended_at if event.ended_at is not None else window_end
        if end is None or end < event.started_at:
            continue
        spans.append((event.started_at, end))
    spans.sort()
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def build_idle(
    events: Sequence[PerfEvent],
    *,
    window: tuple[float, float] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Thời gian không có span nào chạy (Waiting/Idle) + các khoảng chờ lớn nhất.

    Idle **không** đồng nghĩa với lỗi: chờ hợp lệ (màn hình đang tải, app đang khởi
    động ngoài tầm đo) cũng nằm ở đây.  Vì vậy nó được báo là số đo kèm vị trí, không
    phải kết luận.
    """
    scope = [
        event for event in events if _is_run(event) and counts_toward_stage_table(event)
    ]
    if not scope:
        return {"total_ms": None, "share_pct": None, "gaps": []}
    if window is None:
        starts = [e.started_at for e in scope if e.started_at is not None]
        ends = [e.ended_at for e in scope if e.ended_at is not None]
        if not starts or not ends:
            return {"total_ms": None, "share_pct": None, "gaps": []}
        window = (min(starts), max(ends))

    merged = _merged_intervals(scope, window_end=window[1])
    gaps: list[dict[str, Any]] = []
    cursor = window[0]
    for start, end in merged:
        if start > cursor:
            gaps.append(
                {
                    "started_at": cursor,
                    "ended_at": start,
                    "duration_ms": _ms((start - cursor) * 1000),
                }
            )
        cursor = max(cursor, end)
    if window[1] > cursor:
        gaps.append(
            {
                "started_at": cursor,
                "ended_at": window[1],
                "duration_ms": _ms((window[1] - cursor) * 1000),
            }
        )

    total_ms = sum(gap["duration_ms"] or 0.0 for gap in gaps)
    wall_ms = (window[1] - window[0]) * 1000.0
    gaps.sort(key=lambda gap: -(gap["duration_ms"] or 0.0))
    return {
        "total_ms": _ms(total_ms),
        "share_pct": round(total_ms / wall_ms * 100.0, 2) if wall_ms else None,
        "gaps": gaps[:limit],
        "gap_count": len(gaps),
    }


def _action_signature(event: PerfEvent) -> str | None:
    """Chữ ký của một hành động thiết bị: loại + toạ độ (làm tròn 10px) + đích."""
    if not event.is_device() or event.action_type is None:
        return None
    x = event.detail.get("x")
    y = event.detail.get("y")
    coords = ""
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        coords = f"@{int(round(x / 10.0)) * 10},{int(round(y / 10.0)) * 10}"
    target = event.detail.get("target") or event.detail.get("text")
    suffix = f" {target}" if isinstance(target, str) and target else ""
    return f"{event.action_type}{coords}{suffix}"


def detect_loops(
    events: Sequence[PerfEvent], *, threshold: int = REPEAT_THRESHOLD
) -> list[dict[str, Any]]:
    """Phát hiện chuỗi hành động lặp y hệt mà UI không đổi.

    Đây mới là bằng chứng "không có tiến triển" — khác hẳn một màn hình không đổi đơn
    lẻ (rất bình thường khi app đang tải).  Điều kiện: cùng chữ ký hành động, liên tiếp,
    và ``ui_changed is False``.

    Chỉ xét những sự kiện *có phán quyết về UI* (``ui_changed is not None``) — thường là
    sự kiện ``action_outcome`` phát ra ở lần quan sát kế tiếp.  Sự kiện hành động thô
    không mang phán quyết đó, nếu trộn vào sẽ cắt vụn chuỗi lặp và làm mất bằng chứng.
    """
    actions = [
        event
        for event in events
        if _action_signature(event) is not None and event.ui_changed is not None
    ]
    loops: list[dict[str, Any]] = []
    run: list[PerfEvent] = []

    def flush() -> None:
        if len(run) >= threshold:
            first, last = run[0], run[-1]
            span_ms = None
            if first.started_at is not None and last.ended_at is not None:
                span_ms = round((last.ended_at - first.started_at) * 1000.0, 3)
            loops.append(
                {
                    "signature": _action_signature(first),
                    "action_type": first.action_type,
                    "count": len(run),
                    "first_span_id": first.span_id,
                    "last_span_id": last.span_id,
                    "first_at": first.started_at,
                    "span_ms": span_ms,
                    "step_ids": sorted({str(e.step_id) for e in run if e.step_id}),
                }
            )
        run.clear()

    for event in sorted(actions, key=lambda item: item.started_at or 0.0):
        if (
            run
            and _action_signature(run[-1]) == _action_signature(event)
            and event.ui_changed is False
        ):
            run.append(event)
        else:
            flush()
            run.append(event)
    flush()
    return loops


def _action_events(events: Sequence[PerfEvent]) -> list[PerfEvent]:
    """Hành động thiết bị thật (không tính span ``action_outcome`` — đó là số đo độ trễ)."""
    return [
        event
        for event in events
        if event.stage == PerfStage.ADB_ACTION and event.event_name != "action_outcome"
    ]


def _outcome_events(events: Sequence[PerfEvent]) -> list[PerfEvent]:
    """Sự kiện đối chiếu UI sau hành động — nguồn duy nhất của ``ui_changed``."""
    return [event for event in events if event.event_name == "action_outcome"]


def detect_findings(
    events: Sequence[PerfEvent],
    *,
    stages: Sequence[dict[str, Any]],
    idle: dict[str, Any],
    wall_clock_ms: float | None,
    loops: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Nhận định dựa trên số đo.  Chỉ sinh nhận định khi có bằng chứng cụ thể."""
    findings: list[dict[str, Any]] = []
    run_events = [event for event in events if _is_run(event)]
    if not run_events:
        return findings

    measured_stages = [row for row in stages if row["total_ms"]]
    if measured_stages:
        top = measured_stages[:TOP_STAGES]
        findings.append(
            {
                "code": "top_stages",
                "severity": "info",
                "title": f"Top {len(top)} công đoạn tốn thời gian nhất",
                "evidence": [
                    f"{row['label']}: {_fmt_ms(row['total_ms'])}"
                    + (
                        f" ({row['runtime_share_pct']}% thời gian chạy)"
                        if row["runtime_share_pct"] is not None
                        else ""
                    )
                    + f" · {row['calls']} lần"
                    for row in top
                ],
                "metric": {row["stage"]: row["total_ms"] for row in top},
            }
        )

    for row in measured_stages:
        share = row["runtime_share_pct"]
        if share is not None and share >= BOTTLENECK_SHARE * 100.0:
            findings.append(
                {
                    "code": "stage_dominates",
                    "severity": "high",
                    "title": f"{row['label']} chiếm {share}% thời gian chạy",
                    "evidence": [
                        f"{row['calls']} lần, tổng {_fmt_ms(row['total_ms'])}, "
                        f"trung bình {_fmt_ms(row['avg_ms'])}, lớn nhất {_fmt_ms(row['max_ms'])}"
                    ],
                    "metric": {
                        "stage": row["stage"],
                        "share_pct": share,
                        "total_ms": row["total_ms"],
                    },
                }
            )

    retry_ms = sum(
        float(event.retry_wait_ms) for event in run_events if event.retry_wait_ms is not None
    )
    retries = sum(int(event.retry_count) for event in run_events if event.retry_count is not None)
    if retries or retry_ms:
        findings.append(
            {
                "code": "retry_overhead",
                "severity": "high" if retry_ms >= 5000 else "medium",
                "title": f"Mất {_fmt_ms(retry_ms)} vì retry/backoff",
                "evidence": [f"{retries} lần thử lại; tổng thời gian chờ {_fmt_ms(retry_ms)}"],
                "metric": {"retry_count": retries, "retry_wait_ms": round(retry_ms, 3)},
            }
        )

    for loop in loops:
        findings.append(
            {
                "code": "repeated_action_no_ui_change",
                "severity": "high",
                "title": (
                    f"{loop['count']} lần {loop['action_type']} cùng một chỗ "
                    f"({loop['signature']}) mà UI không đổi"
                ),
                "evidence": [
                    f"{_fmt_ms(loop['span_ms'])} trôi qua trong chuỗi lặp này",
                    f"bước liên quan: {', '.join(loop['step_ids']) or 'N/A'}",
                ],
                "metric": loop,
            }
        )

    unchanged = [event for event in _outcome_events(run_events) if event.ui_changed is False]
    if unchanged:
        findings.append(
            {
                "code": "unchanged_ui_actions",
                "severity": "info",
                "title": f"{len(unchanged)} hành động không làm UI thay đổi",
                "evidence": [
                    "Không phải lỗi tự thân: màn hình có thể đang tải, hoặc hành động đúng "
                    "nhưng UI không đổi cách hiển thị.",
                    "Chỉ đáng ngờ khi lặp lại cùng một chỗ — xem nhận định vòng lặp.",
                ],
                "metric": {"count": len(unchanged)},
            }
        )

    gaps = idle.get("gaps") or []
    if gaps and (gaps[0].get("duration_ms") or 0) >= 2000:
        biggest = gaps[0]
        findings.append(
            {
                "code": "idle_gap",
                "severity": "medium",
                "title": f"Chờ {_fmt_ms(biggest['duration_ms'])} không có span nào chạy",
                "evidence": [
                    f"tổng idle {_fmt_ms(idle.get('total_ms'))} "
                    f"({idle.get('share_pct')}% thời gian chạy), {idle.get('gap_count')} khoảng",
                    "Khoảng chờ lớn nhất nằm ngay trước sự kiện kế tiếp trong timeline.",
                ],
                "metric": {"idle_total_ms": idle.get("total_ms"), "gap": biggest},
            }
        )

    failures = [event for event in run_events if event.status not in OK_STATUSES]
    if failures:
        by_stage: dict[str, int] = defaultdict(int)
        for event in failures:
            by_stage[event.stage] += 1
        findings.append(
            {
                "code": "errors",
                "severity": "high",
                "title": f"{len(failures)} span kết thúc bằng lỗi/timeout",
                "evidence": [
                    f"{STAGE_LABELS.get(stage, stage)}: {count} lần"
                    for stage, count in sorted(by_stage.items(), key=lambda item: -item[1])
                ],
                "metric": dict(by_stage),
            }
        )

    device_stages = (
        PerfStage.SCREENSHOT,
        PerfStage.UI_HIERARCHY,
        PerfStage.SCREEN_SETTLING,
    )
    device_rows = [row for row in stages if row["stage"] in device_stages and row["total_ms"]]
    device_total = sum(row["total_ms"] or 0.0 for row in device_rows)
    if device_total and wall_clock_ms:
        share = device_total / wall_clock_ms * 100.0
        if share >= BOTTLENECK_SHARE * 100.0:
            findings.append(
                {
                    "code": "device_io_dominates",
                    "severity": "high",
                    "title": f"Quan sát thiết bị chiếm {round(share, 1)}% thời gian chạy",
                    "evidence": [
                        f"{row['label']}: {_fmt_ms(row['total_ms'])} · {row['calls']} lần"
                        for row in device_rows
                    ],
                    "metric": {"total_ms": round(device_total, 3), "share_pct": round(share, 2)},
                }
            )

    actions = _action_events(run_events)
    llm_events = [event for event in run_events if event.is_llm()]
    if actions:
        llm_calls = sum(1 for event in llm_events if event.duration_ms is not None)
        tokens = sum(
            int(event.total_tokens) for event in llm_events if event.total_tokens is not None
        )
        findings.append(
            {
                "code": "per_action_cost",
                "severity": "info",
                "title": f"Chi phí mỗi hành động ({len(actions)} hành động)",
                "evidence": [
                    f"LLM: {round(llm_calls / len(actions), 2)} lượt/hành động",
                    f"token: {round(tokens / len(actions), 1)}/hành động (tổng {tokens})",
                ],
                "metric": {
                    "actions": len(actions),
                    "llm_calls_per_action": round(llm_calls / len(actions), 3),
                    "tokens_per_action": round(tokens / len(actions), 2),
                },
            }
        )

    first_action = next(
        (
            event.started_at
            for event in sorted(run_events, key=lambda item: item.started_at or 0.0)
            if event.stage == PerfStage.ADB_ACTION and event.started_at is not None
        ),
        None,
    )
    starts = [event.started_at for event in run_events if event.started_at is not None]
    if first_action is not None and starts:
        ttfa = round((first_action - min(starts)) * 1000.0, 3)
        findings.append(
            {
                "code": "time_to_first_action",
                "severity": "info" if ttfa < 15000 else "medium",
                "title": f"Thời gian tới hành động đầu tiên: {_fmt_ms(ttfa)}",
                "evidence": [
                    "Gồm Planner/khởi động app/quan sát màn hình đầu — xem các dòng đầu "
                    "của timeline để biết nó nằm ở đâu.",
                ],
                "metric": {"ttfa_ms": ttfa},
            }
        )
    return findings


def build_report(
    events: Sequence[PerfEvent],
    *,
    session_id: str | None = None,
    session: dict[str, Any] | None = None,
    window: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Báo cáo hiệu năng hoàn chỉnh cho một session (JSON-serializable).

    ``session`` (tuỳ chọn) là metadata của session (``status.json``): có ``start_time``/
    ``end_time`` thì wall-clock lấy từ đó — chính xác hơn khoảng bao của các span, vì nó
    tính cả phần chờ trước span đầu tiên và sau span cuối cùng.
    """
    # Khử trùng phòng thủ: span gốc được ghi hai lần (running → hoàn tất).
    events = dedupe_events(list(events))
    run_events = [event for event in events if _is_run(event)]
    prepare_events = [event for event in events if not _is_run(event)]

    wall_clock_ms: float | None = None
    if session:
        start = session.get("start_time")
        end = session.get("end_time")
        if isinstance(start, (int, float)):
            window = (float(start), float(end) if isinstance(end, (int, float)) else None)  # type: ignore[assignment]
    if window and window[0] is not None:
        end = window[1]
        if end is None:
            ends = [event.ended_at for event in run_events if event.ended_at is not None]
            end = max(ends) if ends else None
        if end is not None and end >= window[0]:
            window = (window[0], end)
            wall_clock_ms = round((end - window[0]) * 1000.0, 3)
        else:
            window = None
    if window is None and run_events:
        starts = [event.started_at for event in run_events if event.started_at is not None]
        ends = [event.ended_at for event in run_events if event.ended_at is not None]
        if starts and ends:
            window = (min(starts), max(ends))
            wall_clock_ms = round((window[1] - window[0]) * 1000.0, 3)

    stages = build_stage_table(run_events, wall_clock_ms=wall_clock_ms)
    idle = build_idle(run_events, window=window)
    if idle.get("total_ms") is not None:
        stages = [
            *stages,
            {
                "stage": str(PerfStage.IDLE),
                "label": STAGE_LABELS[PerfStage.IDLE],
                "calls": idle.get("gap_count", 0),
                "measured_calls": idle.get("gap_count", 0),
                "total_ms": idle.get("total_ms"),
                "inclusive_ms": idle.get("total_ms"),
                "avg_ms": (
                    round(idle["total_ms"] / idle["gap_count"], 3)
                    if idle.get("gap_count")
                    else None
                ),
                "max_ms": (idle["gaps"][0]["duration_ms"] if idle.get("gaps") else None),
                "errors": 0,
                "runtime_share_pct": idle.get("share_pct"),
                "input_tokens": None,
                "output_tokens": None,
                "retry_count": None,
            },
        ]
        stages.sort(key=lambda row: (row["total_ms"] is None, -(row["total_ms"] or 0.0)))

    timeline = build_timeline(events)
    loops = detect_loops(run_events)
    findings = detect_findings(
        run_events, stages=stages, idle=idle, wall_clock_ms=wall_clock_ms, loops=loops
    )

    actions = _action_events(run_events)
    llm_events = [event for event in run_events if event.is_llm()]
    unchanged = [event for event in _outcome_events(run_events) if event.ui_changed is False]
    retry_ms = sum(
        float(event.retry_wait_ms) for event in run_events if event.retry_wait_ms is not None
    )
    retries = sum(int(event.retry_count) for event in run_events if event.retry_count is not None)
    measured = [event for event in run_events if event.duration_ms is not None]
    has_run = bool(run_events)

    covered_ms = round(
        sum(row["total_ms"] or 0.0 for row in stages if row["stage"] != str(PerfStage.IDLE)),
        3,
    )
    coverage_pct = (
        round(covered_ms / wall_clock_ms * 100.0, 2) if wall_clock_ms and covered_ms else None
    )
    parallel_note = ""
    if coverage_pct is not None and coverage_pct > 105.0:
        parallel_note = (
            f" Tổng exclusive ({covered_ms:.0f}ms) lớn hơn wall-clock "
            f"({wall_clock_ms:.0f}ms) nghĩa là có công đoạn chạy song song "
            "(ví dụ screenshot + UI hierarchy) — % runtime vì thế cộng lại có thể vượt 100%."
        )

    return {
        "session_id": session_id,
        "generated_at": time.time(),
        "session": {
            "status": (session or {}).get("status"),
            "start_time": window[0] if window else None,
            "end_time": window[1] if window else None,
            "wall_clock_ms": wall_clock_ms,
        },
        "summary": {
            "events": len(events),
            "run_events": len(run_events),
            "prepare_events": len(prepare_events),
            "measured_events": len(measured),
            "total_session_ms": wall_clock_ms,
            # Khi chưa có sự kiện nào của phase chạy thì các con số đếm là *không có số
            # đo* (None ⇒ N/A), không phải 0 — 0 sẽ bị đọc thành "đã đo và bằng không".
            "actions": len(actions) if has_run else None,
            "llm_calls": (
                sum(1 for event in llm_events if event.duration_ms is not None) if has_run else None
            ),
            "tokens_total": (
                sum(
                    int(event.total_tokens)
                    for event in llm_events
                    if event.total_tokens is not None
                )
                or None
            )
            if has_run
            else None,
            "retry_count": (retries or None) if has_run else None,
            "retry_wait_ms": (round(retry_ms, 3) if retries or retry_ms else None)
            if has_run
            else None,
            "actions_without_ui_change": len(unchanged) if actions else None,
            "idle_ms": idle.get("total_ms"),
            "idle_share_pct": idle.get("share_pct"),
            "loops": len(loops) if has_run else None,
            "covered_ms": covered_ms or None,
            "coverage_pct": coverage_pct,
        },
        "stages": stages,
        "timeline": timeline,
        "idle": idle,
        "loops": loops,
        "findings": findings,
        "prepare": {
            "stages": build_stage_table(prepare_events, wall_clock_ms=None, include_prepare=True)
            if prepare_events
            else [],
            "events": [event.model_dump() for event in prepare_events],
        },
        "coverage_note": (
            "Cột Total/% runtime tính theo exclusive time (đã trừ span con) nên không cộng "
            "trùng; idle là phần wall-clock không có span nào chạy. Công đoạn không đo được "
            "hiển thị N/A, không phải 0." + parallel_note
        ),
    }
