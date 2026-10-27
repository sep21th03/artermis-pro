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

"""Trạng thái ngữ cảnh của lớp đo hiệu năng.

Mọi thứ ở đây là ``ContextVar`` để **không** phải truyền tham số xuyên qua các lớp
agent (Planner → Operator → LLM → ADB).  Đó là điều kiện để instrumentation không
làm thay đổi hành vi của Planner/Operator: call site chỉ đọc context, không sửa gì.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - chỉ để tránh import vòng
    from artemis.perf.recorder import PerfRecorder

#: Recorder của session đang chạy trong tiến trình này.
CURRENT_RECORDER: contextvars.ContextVar[PerfRecorder | None] = contextvars.ContextVar(
    "perf_recorder", default=None
)

#: Ngăn xếp span đang mở, dùng để suy ra ``parent_span_id`` mà không cần tham số.
SPAN_STACK: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "perf_span_stack", default=()
)

#: Bước (step) hiện tại của agent — gắn mọi span vào đúng bước để hiển thị timeline.
CURRENT_STEP_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "perf_step_id", default=None
)

#: Vai trò agent đang chạy (planner/operator/checker/summarizer).  Lượt gọi LLM đọc
#: giá trị này để biết mình thuộc công đoạn nào — không phải đoán theo tên model.
CURRENT_AGENT_ROLE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "perf_agent_role", default=None
)

#: Id tương quan của một hành động (LLM quyết định → ADB thực thi → quan sát lại).
CURRENT_CORRELATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "perf_correlation_id", default=None
)

#: Lượt suy luận thứ mấy trong session (turn của Flash, node call của Pro).
CURRENT_ITERATION: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "perf_iteration", default=None
)

#: Cấu hình đo — đọc từ settings khi có, mặc định bật.
DETAIL_LEVEL: contextvars.ContextVar[str] = contextvars.ContextVar(
    "perf_detail_level", default="full"
)


def current_recorder() -> PerfRecorder | None:
    """Recorder đang hoạt động, hoặc ``None`` khi lớp đo tắt/không có session."""
    return CURRENT_RECORDER.get()


def current_parent_span() -> str | None:
    """``span_id`` của span đang mở trong cùng task asyncio (None nếu là span gốc)."""
    stack = SPAN_STACK.get()
    return stack[-1] if stack else None


def current_agent_role() -> str | None:
    return CURRENT_AGENT_ROLE.get()


def current_step_id() -> str | None:
    return CURRENT_STEP_ID.get()


def current_correlation_id() -> str | None:
    return CURRENT_CORRELATION_ID.get()


def current_iteration() -> int | None:
    return CURRENT_ITERATION.get()


def detail_level() -> str:
    return DETAIL_LEVEL.get()
