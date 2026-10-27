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

"""Lớp đo hiệu năng của ARTEMIS: đo — ghi JSONL — tổng hợp — hiển thị.

Phạm vi **chỉ quan sát**: không đổi thuật toán Planner/Operator, không gọi thêm LLM,
không thêm bước vào task.  Xem ``artemis/perf/README.md``.
"""

from artemis.perf.aggregate import (
    build_idle,
    build_report,
    build_stage_table,
    build_timeline,
    detect_findings,
    detect_loops,
)
from artemis.perf.context import (
    CURRENT_AGENT_ROLE,
    CURRENT_CORRELATION_ID,
    CURRENT_ITERATION,
    CURRENT_STEP_ID,
)
from artemis.perf.models import (
    STAGE_LABELS,
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_RETRY,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    PerfEvent,
    PerfStage,
    redact,
)
from artemis.perf.recorder import (
    PERF_FILENAME,
    PerfRecorder,
    get_recorder,
    iter_new_events,
    iter_sessions_with_perf,
    perf_enabled,
    perf_level,
    read_events,
    session_perf_path,
    set_recorder,
    traces_dir,
)
from artemis.perf.spans import (
    PerfSpan,
    new_correlation_id,
    perf_span,
    perf_span_sync,
    perf_traced,
    record_event,
)

__all__ = [
    "CURRENT_AGENT_ROLE",
    "CURRENT_CORRELATION_ID",
    "CURRENT_ITERATION",
    "CURRENT_STEP_ID",
    "PERF_FILENAME",
    "STAGE_LABELS",
    "STATUS_CANCELLED",
    "STATUS_ERROR",
    "STATUS_RETRY",
    "STATUS_RUNNING",
    "STATUS_SUCCESS",
    "STATUS_TIMEOUT",
    "PerfEvent",
    "PerfRecorder",
    "PerfSpan",
    "PerfStage",
    "build_report",
    "build_stage_table",
    "build_timeline",
    "detect_findings",
    "detect_loops",
    "get_recorder",
    "iter_new_events",
    "iter_sessions_with_perf",
    "new_correlation_id",
    "perf_enabled",
    "perf_level",
    "perf_span",
    "perf_span_sync",
    "perf_traced",
    "read_events",
    "record_event",
    "redact",
    "session_perf_path",
    "set_recorder",
    "traces_dir",
]
