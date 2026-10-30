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

"""Số đo hiệu năng theo từng lượt Operator/bước — **không** gọi thêm LLM, chi phí ~0.

**Vì sao.** Phân tích session ``d110b1ef`` phải dựng lại timeline bằng script đọc
``perf_events.jsonl``; mỗi lần như vậy tốn công và dễ sai. Module này gom số đo **tại chỗ**: Operator
biết số LLM call / token / gate bounce / self-heal; Validator biết thời gian validation và kết quả
từng action. Kết quả ghi thành một event **có cấu trúc** vào ``perf_events.jsonl`` (``type="metrics"``,
``name="step_metrics"``) và một dòng log dễ đọc ``[step-metrics] …``.

**Chỉ đếm, không quyết định.** Module không đổi luồng, không thêm lượt LLM, không đọc/ghi KB.

Phân loại action: ``dispatched`` (thực thi xong) · ``already_applied`` (màn hình đã đổi đúng hướng,
không thực thi) · ``failed`` (lỗi / bị safety net chặn) · ``blocked`` (bị gate chặn, không thực thi) ·
``none`` (lượt không phát action). ``no_progress`` = lượt không tạo tiến triển.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

#: Tên event có cấu trúc ghi vào perf.
METRICS_EVENT_NAME = "step_metrics"

#: **Phạm vi** của ``llm_calls`` / token trong payload: số **response LLM mà các lượt Operator tiêu
#: thụ** (mỗi vòng tool-calling trong một lượt là một response). Đây KHÁC với kênh perf ``llm_call``
#: (một span cho mỗi lượt gọi provider **đã hoàn tất của mọi vai trò**: planner/operator/khác) — và
#: các lần thử lại nằm ở event ``llm_retry`` (``retry_count`` + ``retry_wait_ms``). Ghi rõ phạm vi để
#: hai kênh không bị đọc như cùng một phép đếm (đo trên máy thật 30/10/2026: 148 response của lượt
#: Operator vs 103 span ``llm_call`` trong cùng một phiên).
LLM_COUNT_SCOPE = "operator_turn_responses"
TOKEN_COUNT_SCOPE = "operator_turn_responses"

#: Bản rời của pha operator (một bước có thể có bản rời + đúng một bản tổng hợp).
METRICS_TURN_EVENT_NAME = "step_metrics_turn"


@dataclass
class StepMetrics:
    """Bộ đếm của **một** lượt/bước. Mọi trường đều cộng dồn được (trừ ``action``/``no_progress``)."""

    turn: int = 0
    wall_ms: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    gate_bounces: int = 0
    plan_self_heals: int = 0
    validation_ms: float = 0.0
    pixel_validation_ms: float = 0.0
    actions_dispatched: int = 0
    actions_already_applied: int = 0
    actions_failed: int = 0
    actions_blocked_by_gate: int = 0
    #: Bị **retry guard** chặn vì lặp lại hành động đã thất bại thật (không phải failed).
    actions_repeat_blocked: int = 0
    no_progress: bool = False
    action: str = "none"
    phase: str = "operator"
    emitted: bool = False
    started_at: float = field(default_factory=time.time)

    def add_llm_usage(self, response) -> None:
        """Cộng **một** lượt gọi LLM (mỗi response đúng một lần — không đếm hai lần).

        Đọc ``usage_metadata`` của LangChain; thiếu trường nào thì bỏ qua trường đó — đo hiệu năng
        không bao giờ được làm hỏng lượt chạy.
        """
        self.llm_calls += 1
        usage = getattr(response, "usage_metadata", None) or {}
        if not isinstance(usage, dict):
            return
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        cached = usage.get("input_token_details") or {}
        if isinstance(cached, dict):
            self.cached_tokens += int(cached.get("cache_read") or 0)

    def add_validation(self, *, total_ms: float, pixel_ms: float = 0.0) -> None:
        self.validation_ms += max(0.0, float(total_ms))
        self.pixel_validation_ms += max(0.0, float(pixel_ms))

    def as_detail(self) -> dict:
        """Bản ghi có cấu trúc cho ``perf_events.jsonl``."""
        data = asdict(self)
        data["wall_ms"] = round(self.wall_ms, 1)
        data["validation_ms"] = round(self.validation_ms, 1)
        data["pixel_validation_ms"] = round(self.pixel_validation_ms, 1)
        data["llm_calls_scope"] = LLM_COUNT_SCOPE
        data["tokens_scope"] = TOKEN_COUNT_SCOPE
        return data

    def log_line(self, step: int | str = "-") -> str:
        """Dòng log dễ đọc (một dòng, một lượt)."""
        return (
            f"[step-metrics] step={step} phase={self.phase} wall_ms={self.wall_ms:.0f} "
            f"llm_calls={self.llm_calls} in_tok={self.input_tokens} out_tok={self.output_tokens} "
            f"cached={self.cached_tokens} bounces={self.gate_bounces} "
            f"self_heals={self.plan_self_heals} validation_ms={self.validation_ms:.0f} "
            f"pixel_ms={self.pixel_validation_ms:.0f} dispatched={self.actions_dispatched} "
            f"already_applied={self.actions_already_applied} failed={self.actions_failed} "
            f"blocked={self.actions_blocked_by_gate} repeat_blocked={self.actions_repeat_blocked} "
            f"action={self.action} "
            f"no_progress={int(self.no_progress)}"
        )


class _Fallback:
    """Nơi giữ bộ đếm khi không có data engine (test/đường tắt) — vẫn cần sống qua nhiều lần gọi."""

    step_metrics: StepMetrics | None = None
    current_step_number: int | None = None


_FALLBACK = _Fallback()


def holder_for(ctx=None, state=None):
    """Nơi giữ bộ đếm của lượt: **data engine**.

    Vì sao không dùng ``state``: ``artemis.graph.state.State`` là **pydantic BaseModel** — set
    attribute lạ sẽ bị từ chối (đã đo trên phiên thật: mọi bộ đếm cross-call về 0). Data engine là
    đối tượng **dùng chung** giữa Operator và Validator trong cùng một lượt, và nó cũng biết
    ``current_step_number`` nên dòng log có đúng số bước.
    """
    engine = getattr(ctx, "data_engine", None)
    return engine if engine is not None else _FALLBACK


def metrics_for(holder) -> StepMetrics:
    """Bộ đếm của lượt hiện tại (tạo mới nếu chưa có). Không bao giờ ném lỗi."""
    current = getattr(holder, "step_metrics", None)
    if isinstance(current, StepMetrics):
        return current
    current = StepMetrics()
    try:
        holder.step_metrics = current
    except Exception as exc:  # noqa: BLE001 - holder lạ thì vẫn trả bộ đếm cục bộ
        # **Không nuốt im lặng**: bộ đếm không gắn được vào holder là lỗi thật (đã xảy ra với
        # `State` của pydantic) — phải thấy được trong log, không chỉ thấy số 0 vô nghĩa.
        logger.warning(
            f"step-metrics: không gắn được bộ đếm vào {type(holder).__name__} ({exc});"
            " số đo lượt này sẽ không tích luỹ được."
        )
        return current
    return current


def reset_turn(ctx=None, state=None) -> StepMetrics:
    """Bắt đầu lượt mới: bộ đếm về 0."""
    holder = holder_for(ctx, state)
    fresh = StepMetrics()
    try:
        holder.step_metrics = fresh
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"step-metrics: không reset được bộ đếm trên {type(holder).__name__} ({exc})")
        return fresh
    return fresh


def add_llm_usage(ctx, response) -> None:
    """Cộng một lượt gọi LLM vào bộ đếm của lượt hiện tại."""
    metrics_for(holder_for(ctx)).add_llm_usage(response)


def count_gate_bounce(ctx, *, blocked: bool = True) -> None:
    """Một lần gate chặn action (``blocked=False`` cho bounce của *state evidence*)."""
    metrics = metrics_for(holder_for(ctx))
    metrics.gate_bounces += 1
    if blocked:
        metrics.actions_blocked_by_gate += 1


def count_plan_self_heal(ctx) -> None:
    metrics_for(holder_for(ctx)).plan_self_heals += 1


def add_validation(ctx, *, total_ms: float, pixel_ms: float = 0.0) -> None:
    metrics_for(holder_for(ctx)).add_validation(total_ms=total_ms, pixel_ms=pixel_ms)


def finish_operator_turn(ctx, *, action: str, force: bool = False) -> StepMetrics:
    """Chốt lượt Operator: ``wall_ms`` + loại action; phát log + event.

    ``action`` ∈ ``dispatched`` | ``already_applied`` | ``failed`` | ``blocked`` | ``none``.
    Lượt bị gate chặn **thoát sớm** nên phải gọi ngay tại chỗ chặn; ``emitted`` giữ cho lượt chỉ
    được chốt **một lần** (không nhân đôi dòng log/event).
    """
    metrics = metrics_for(holder_for(ctx))
    if metrics.emitted and not force:
        return metrics
    metrics.phase = "operator"
    metrics.action = action
    metrics.wall_ms = (time.time() - metrics.started_at) * 1000.0
    metrics.no_progress = action in ("none", "blocked")
    # Lượt bị gate chặn kết thúc luôn ở pha operator ⇒ bản này **là** bản tổng hợp của bước.
    _emit(ctx, metrics, summary=action == "blocked")
    return metrics


def finish_execution(
    ctx,
    *,
    dispatched: int,
    already_applied: int,
    failed: int,
    repeat_blocked: int = 0,
) -> StepMetrics:
    """Chốt pha thực thi: cộng kết quả từng action rồi phát log/event (bản đầy đủ nhất)."""
    metrics = metrics_for(holder_for(ctx))
    metrics.phase = "execution"
    metrics.actions_dispatched += max(0, int(dispatched))
    metrics.actions_already_applied += max(0, int(already_applied))
    metrics.actions_failed += max(0, int(failed))
    metrics.actions_repeat_blocked += max(0, int(repeat_blocked))
    metrics.wall_ms = (time.time() - metrics.started_at) * 1000.0
    if metrics.actions_dispatched:
        metrics.action = "dispatched"
    elif metrics.actions_already_applied:
        metrics.action = "already_applied"
    elif metrics.actions_repeat_blocked:
        metrics.action = "repeat_blocked"
    elif metrics.actions_failed:
        metrics.action = "failed"
    elif metrics.actions_blocked_by_gate:
        metrics.action = "blocked"
    else:
        metrics.action = "none"
    metrics.no_progress = metrics.action != "dispatched"
    _emit(ctx, metrics, summary=True)
    return metrics


def _emit(ctx, metrics: StepMetrics, *, summary: bool) -> None:
    """Ghi một dòng log dễ đọc + **một** event perf. Hỏng thì im lặng (chỉ cảnh báo)."""
    holder = holder_for(ctx)
    raw_step = getattr(holder, "current_step_number", None)
    step: int | str = "-" if raw_step is None else raw_step  # 0 là số bước HỢP LỆ, không phải "-"
    logger.info(metrics.log_line(step=step))
    metrics.emitted = True
    # Mỗi bước đúng **một** event tổng hợp ``step_metrics``: lượt Operator chỉ ghi bản rời
    # ``step_metrics_turn``, trừ lượt bị gate chặn (kết thúc luôn ở pha operator nên bản đó là
    # bản tổng hợp của bước).
    emit_structured(
        ctx,
        metrics,
        event_name=METRICS_EVENT_NAME if summary else METRICS_TURN_EVENT_NAME,
        step=step,
    )


def emit_structured(
    ctx,
    metrics: StepMetrics | None = None,
    *,
    event_name: str = METRICS_EVENT_NAME,
    step: int | str | None = None,
) -> None:
    """Ghi event có cấu trúc vào ``perf_events.jsonl`` qua **perf recorder của hệ thống**.

    Vì sao không dùng ``DataEngine.record_trace``: hàm đó chỉ ghi bảng SQLite ``traces`` và publish
    sự kiện IPC — nó **không** đi tới ``perf_events.jsonl`` (đã đo: phiên d6a68202 có 82 dòng
    ``[step-metrics]`` nhưng 0 event trong 740 dòng perf). Đường ghi đúng là
    ``artemis.perf.spans.record_event`` → ``PerfRecorder`` (không tạo cơ chế ghi file song song).
    """
    holder = holder_for(ctx)
    metrics = metrics or metrics_for(holder)
    engine = getattr(ctx, "data_engine", None)
    if step is None:
        raw_step = getattr(holder, "current_step_number", None)
        step = "-" if raw_step is None else raw_step
    try:
        from artemis.perf import PerfStage, record_event

        raw_step_id = getattr(engine, "current_step_id", None)
        detail = metrics.as_detail()
        detail["type"] = "metrics"
        if step is not None:
            detail["step"] = step
        record_event(
            PerfStage.AGENT,
            event_name,
            duration_ms=metrics.wall_ms or None,
            status="success",
            step_id=str(raw_step_id) if raw_step_id else None,
            iteration=int(step) if isinstance(step, int) else None,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 - metrics không được làm gãy lượt chạy
        logger.warning(f"step-metrics: không ghi được event có cấu trúc ({event_name}): {exc}")
