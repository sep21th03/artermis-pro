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

"""Schema sự kiện hiệu năng (performance event) cho ARTEMIS.

Một sự kiện = một span đã hoàn tất, có số đo thật.  Nguyên tắc quan trọng nhất của
module này:

**Không đo được thì để ``None`` (hiển thị ``N/A``), tuyệt đối không gán 0.**  Một
``0`` giả sẽ chảy vào bảng tổng hợp, vào "% runtime" và vào kết luận bottleneck —
nghĩa là hệ thống đo sẽ nói dối đúng lúc người đọc cần nó nhất.  Vì vậy mọi trường
số đo đều là optional và ``None`` mang nghĩa "không có số đo".
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PerfStage(StrEnum):
    """Các công đoạn xuất hiện trên bảng Performance.

    Giá trị là khoá ổn định (dùng cho JSON/UI); nhãn hiển thị nằm ở
    :data:`STAGE_LABELS` để đổi chữ mà không phá log đã ghi.
    """

    PLANNER_LLM = "planner_llm"
    OPERATOR_LLM = "operator_llm"
    CHECKER_LLM = "checker_llm"
    SUMMARIZER_LLM = "summarizer_llm"
    LLM_OTHER = "llm_other"
    AGENT = "agent"
    TOOL = "tool"
    VALIDATION = "validation"
    MEMORY = "memory"
    RAG_PREPARE = "rag_prepare"
    RAG_RETRIEVE = "rag_retrieve"
    RAG_EMBEDDING = "rag_embedding"
    RAG_SEARCH = "rag_search"
    SCREENSHOT = "screenshot"
    UI_HIERARCHY = "ui_hierarchy"
    SCREEN_SETTLING = "screen_settling"
    ADB_ACTION = "adb_action"
    COMPRESSION = "compression"
    IDLE = "idle"
    SESSION = "session"


#: Nhãn hiển thị trên bảng Performance (giữ đúng thứ tự mong muốn của UI).
STAGE_LABELS: dict[str, str] = {
    PerfStage.PLANNER_LLM: "Planner LLM",
    PerfStage.OPERATOR_LLM: "Operator LLM",
    PerfStage.CHECKER_LLM: "Checker LLM",
    PerfStage.SUMMARIZER_LLM: "Summarizer",
    PerfStage.LLM_OTHER: "LLM khác (validator/explorer)",
    PerfStage.AGENT: "Agent node (bao gồm cả LLM)",
    PerfStage.TOOL: "Tool",
    PerfStage.VALIDATION: "Action validation",
    PerfStage.MEMORY: "Memory (note/transcript)",
    PerfStage.RAG_PREPARE: "RAG — chuẩn bị prompt",
    PerfStage.RAG_RETRIEVE: "RAG — truy xuất",
    PerfStage.RAG_EMBEDDING: "RAG — embedding",
    PerfStage.RAG_SEARCH: "RAG — vector search",
    PerfStage.SCREENSHOT: "Screenshot",
    PerfStage.UI_HIERARCHY: "UI Hierarchy",
    PerfStage.SCREEN_SETTLING: "Đợi màn hình ổn định",
    PerfStage.ADB_ACTION: "ADB Actions",
    PerfStage.COMPRESSION: "Nén ngữ cảnh (compression)",
    PerfStage.IDLE: "Waiting/Idle",
    PerfStage.SESSION: "Cả session",
}

#: Trạng thái kết thúc của một span.
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_RETRY = "retry"
STATUS_RUNNING = "running"
STATUS_CANCELLED = "cancelled"


class PerfEvent(BaseModel):
    """Một span hiệu năng đã hoàn tất (một dòng trong ``perf_events.jsonl``)."""

    model_config = ConfigDict(extra="forbid")

    # ── Định danh & tương quan ──────────────────────────────────────────
    session_id: str | None = Field(default=None, description="Session/trace id của lần chạy")
    step_id: str | None = Field(default=None, description="Bước agent đang chạy khi span xảy ra")
    request_id: str | None = Field(default=None, description="Id một lượt gọi LLM (gồm retry)")
    span_id: str = Field(..., description="Id của span này")
    parent_span_id: str | None = Field(default=None, description="Span cha (để tính exclusive)")
    correlation_id: str | None = Field(default=None, description="Id xuyên suốt một hành động")

    # ── Nội dung ────────────────────────────────────────────────────────
    event_name: str = Field(..., description="Tên sự kiện, ví dụ 'llm_call', 'adb_tap'")
    stage: str = Field(..., description="Công đoạn (xem PerfStage)")
    phase: str = Field(default="run", description="'run' (agent chạy) hoặc 'prepare' (console)")
    agent_role: str | None = Field(default=None, description="planner/operator/checker/summarizer")

    # ── Thời gian ───────────────────────────────────────────────────────
    started_at: float | None = Field(default=None, description="Timestamp thực (epoch giây)")
    ended_at: float | None = Field(default=None, description="Timestamp thực (epoch giây)")
    duration_ms: float | None = Field(default=None, description="Đo bằng time.perf_counter()")

    # ── Kết quả ─────────────────────────────────────────────────────────
    status: str = Field(default=STATUS_SUCCESS, description="success/error/timeout/retry/running")
    error_type: str | None = Field(default=None, description="Loại lỗi (class/exception)")
    error_code: str | None = Field(default=None, description="Mã lỗi của provider/SDK nếu có")
    http_status: int | None = Field(default=None, description="HTTP status nếu biết chắc")

    # ── LLM ─────────────────────────────────────────────────────────────
    model: str | None = None
    provider: str | None = None
    endpoint: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    retry_count: int | None = Field(default=None, description="Số lần thử lại trong span này")
    retry_wait_ms: float | None = Field(default=None, description="Tổng thời gian chờ backoff")
    ttft_ms: float | None = Field(default=None, description="Time to first token (streaming)")

    # ── Thiết bị / UI ───────────────────────────────────────────────────
    action_type: str | None = Field(default=None, description="tap/swipe/input/back/…")
    ui_fingerprint: str | None = Field(default=None, description="Hash trạng thái UI sau span")
    ui_changed: bool | None = Field(
        default=None, description="UI có đổi so với lần quan sát trước (None = chưa đo được)"
    )
    iteration: int | None = Field(default=None, description="Lượt suy luận/bước thứ mấy")

    #: Chi tiết phụ, **không** chứa prompt/ảnh/khoá bí mật.
    detail: dict[str, Any] = Field(default_factory=dict)

    def is_llm(self) -> bool:
        return self.stage.endswith("_llm") or self.stage == PerfStage.LLM_OTHER

    def is_device(self) -> bool:
        return self.stage in (
            PerfStage.ADB_ACTION,
            PerfStage.SCREENSHOT,
            PerfStage.UI_HIERARCHY,
            PerfStage.SCREEN_SETTLING,
        )

    def measured_ms(self) -> float | None:
        """Duration đã đo, hoặc ``None`` khi span không có số đo (hiển thị N/A)."""
        return self.duration_ms


#: Từ khoá bị coi là bí mật — gặp là loại khỏi payload trước khi ghi log.
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "passwd",
        "secret",
        "access_token",
        "refresh_token",
        "bearer",
        "cookie",
        "set_cookie",
        "credential",
        "private_key",
        "session_key",
    }
)

#: Trần độ dài chuỗi giữ lại trong payload.  Prompt, ảnh base64 và XML hierarchy đều
#: vượt xa ngưỡng này, nên chúng không bao giờ lọt vào performance log.
MAX_STRING_CHARS = 200


def redact(payload: Any, *, _depth: int = 0) -> Any:
    """Bỏ mọi trường trông như bí mật và mọi chuỗi dài khỏi payload.

    Performance log **không** được chứa API key, mật khẩu hay prompt đầy đủ.  Hàm này
    là chốt chặn cuối ở tầng ghi log: nó chạy trên mọi payload trước khi serialize,
    kể cả payload do call site truyền vào, nên một call site lơ đễnh cũng không làm
    rò dữ liệu.
    """
    if _depth > 6:
        return "<deep>"
    if isinstance(payload, dict):
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            if str(key).strip().lower().replace("-", "_") in SECRET_KEYS:
                cleaned[key] = "<redacted>"
            else:
                cleaned[key] = redact(value, _depth=_depth + 1)
        return cleaned
    if isinstance(payload, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in payload[:50]]
    if isinstance(payload, str):
        text = payload.strip()
        if len(text) > MAX_STRING_CHARS:
            return f"<str len={len(text)}>"
        return text
    if isinstance(payload, (int, float, bool)) or payload is None:
        return payload
    return str(payload)[:MAX_STRING_CHARS]
