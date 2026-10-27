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

"""Ghi sự kiện hiệu năng ra JSONL theo session, và đọc lại để phân tích.

Vì sao là JSONL trong ``traces/<session_id>/``:

* Tiến trình thực thi là **Artemis Daemon**, còn console chỉ đọc lại.  File trong thư
  mục trace của session là nguồn duy nhất đúng cả khi CLI đã thoát mà daemon vẫn chạy,
  và không thể lẫn log của session khác (mỗi session một thư mục).
* JSONL append-only đọc được từng dòng, nên console hiển thị số liệu **trong lúc chạy**
  mà không cần đợi session kết thúc.
* Mất/đứt stream chỉ cần đọc lại từ offset cũ — không có trạng thái trong RAM nào phải
  đồng bộ giữa hai tiến trình.

Lớp này **không bao giờ raise**: đo hiệu năng là quan sát, không được làm gãy task.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from artemis.perf.context import CURRENT_RECORDER, DETAIL_LEVEL
from artemis.perf.models import STATUS_RUNNING, PerfEvent, redact

logger = logging.getLogger(__name__)

#: Tên file trong thư mục trace của session.
PERF_FILENAME = "perf_events.jsonl"

#: Trần số sự kiện giữ trong RAM (chỉ để đọc nhanh; file vẫn giữ tất cả).
DEFAULT_MAX_EVENTS = 20000


def _settings_value(name: str, default: Any) -> Any:
    try:
        from artemis.config.settings import settings

        return getattr(settings, name, default)
    except Exception:  # noqa: BLE001 - thiếu config thì dùng mặc định
        return default


def perf_enabled() -> bool:
    """``ARTEMIS_PERF_ENABLED`` (mặc định bật)."""
    return bool(_settings_value("ARTEMIS_PERF_ENABLED", True))


def perf_level() -> str:
    """``ARTEMIS_PERF_LEVEL``: ``full`` | ``summary`` | ``off``."""
    level = str(_settings_value("ARTEMIS_PERF_LEVEL", "full") or "full").strip().lower()
    return level if level in ("full", "summary", "off") else "full"


def traces_dir() -> Path:
    """Thư mục ``traces/`` của repo (nơi mọi session ghi dữ liệu)."""
    try:
        from artemis.config.paths import get_traces_dir

        return Path(get_traces_dir())
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parents[2] / "traces"


def session_perf_path(session_id: str, *, base: Path | None = None) -> Path:
    """Đường dẫn file perf của một session."""
    return (base or traces_dir()) / str(session_id) / PERF_FILENAME


class PerfRecorder:
    """Ghi ``PerfEvent`` xuống JSONL của một session."""

    def __init__(
        self,
        session_id: str,
        *,
        path: Path | None = None,
        enabled: bool | None = None,
        level: str | None = None,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> None:
        self.session_id = str(session_id)
        self.path = path or session_perf_path(self.session_id)
        self.enabled = perf_enabled() if enabled is None else bool(enabled)
        self.level = level or perf_level()
        self.max_events = max(1, int(max_events))
        self.events: list[PerfEvent] = []
        self._lock = threading.Lock()
        self._handle = None
        self._written = 0

    # ── Ghi ─────────────────────────────────────────────────────────────
    def record(self, event: PerfEvent) -> PerfEvent | None:
        """Ghi một sự kiện; trả về sự kiện đã ghi, hoặc ``None`` khi lớp đo tắt."""
        if not self.enabled or self.level == "off":
            return None
        if event.session_id is None:
            event.session_id = self.session_id
        if event.detail:
            event.detail = redact(event.detail)
        with self._lock:
            self._append_memory(event)
            self._write_line(event)
        return event

    def _append_memory(self, event: PerfEvent) -> None:
        self.events.append(event)
        if len(self.events) > self.max_events:
            del self.events[: len(self.events) - self.max_events]

    def _write_line(self, event: PerfEvent) -> None:
        try:
            if self._handle is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = open(self.path, "a", encoding="utf-8")
            self._handle.write(json.dumps(event.model_dump(), ensure_ascii=False) + "\n")
            self._handle.flush()
            self._written += 1
        except Exception as exc:  # noqa: BLE001 - đo hiệu năng không được làm gãy task
            logger.debug("Không ghi được perf event: %s", exc)
            self._handle = None

    def mark_running(self, event: PerfEvent) -> None:
        """Ghi nhận span vừa **bắt đầu** (dòng ``running``) rồi ghi ra file.

        Vì sao phải ghi ra file ngay: console đọc file để hiển thị số liệu *trong lúc*
        agent đang chạy.  Nếu span gốc của session chỉ được ghi lúc kết thúc thì suốt
        thời gian chạy không có khoảng thời gian nào bao trùm session, và mọi thứ đều bị
        tính là ``Waiting/Idle`` — đúng cái nhìn sai lệch mà lớp đo này sinh ra để xoá.
        Dòng hoàn tất sau đó ghi lại cùng ``span_id``; khi đọc, bản ghi cuối thắng
        (:func:`dedupe_events`).
        """
        if not self.enabled or self.level == "off":
            return
        event.status = STATUS_RUNNING
        event.started_at = event.started_at or time.time()
        with self._lock:
            self._append_memory(event)
            self._write_line(event)

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.close()
                except Exception:  # noqa: BLE001
                    pass
                self._handle = None

    def snapshot(self) -> list[PerfEvent]:
        with self._lock:
            return list(self.events)

    @property
    def written(self) -> int:
        return self._written


def set_recorder(recorder: PerfRecorder | None) -> None:
    """Đặt recorder cho task/tiến trình hiện tại."""
    CURRENT_RECORDER.set(recorder)
    DETAIL_LEVEL.set(recorder.level if recorder else "full")


def get_recorder() -> PerfRecorder | None:
    """Recorder đang hoạt động trong context hiện tại."""
    return CURRENT_RECORDER.get()


def read_events(path: Path) -> list[PerfEvent]:
    """Đọc toàn bộ sự kiện từ một file JSONL (bỏ qua dòng hỏng/đang ghi dở)."""
    events: list[PerfEvent] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(PerfEvent.model_validate(json.loads(line)))
                except Exception:  # noqa: BLE001 - dòng cuối có thể đang được ghi
                    continue
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug("Không đọc được %s: %s", path, exc)
    return dedupe_events(events)


def dedupe_events(events: list[PerfEvent]) -> list[PerfEvent]:
    """Giữ bản ghi **cuối** cho mỗi ``span_id``.

    Một span gốc được ghi hai lần: một dòng ``running`` lúc bắt đầu (để thấy được thời
    gian đang chạy) và một dòng hoàn tất lúc kết thúc.  Không khử trùng thì thời gian của
    nó bị cộng hai lần.
    """
    latest: dict[str, PerfEvent] = {}
    order: list[str] = []
    for event in events:
        if event.span_id not in latest:
            order.append(event.span_id)
        latest[event.span_id] = event
    return [latest[span_id] for span_id in order]


def iter_new_events(path: Path, offset: int) -> tuple[list[PerfEvent], int]:
    """Đọc phần **mới** của file kể từ ``offset`` byte; trả ``(events, offset mới)``.

    Đây là cơ chế "đồng bộ lại sau khi stream bị ngắt": console giữ offset đã đọc,
    lần poll sau chỉ lấy phần thêm vào — không mất sự kiện, không đọc lại từ đầu.
    """
    events: list[PerfEvent] = []
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:  # file bị ghi lại từ đầu → đọc lại
        offset = 0
    if size == offset:
        return [], offset
    try:
        with open(path, encoding="utf-8") as handle:
            handle.seek(offset)
            for line in handle:
                if not line.endswith("\n"):
                    break  # dòng đang được ghi dở: để lần poll sau
                offset += len(line.encode("utf-8"))
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(PerfEvent.model_validate(json.loads(line)))
                except Exception:  # noqa: BLE001
                    continue
    except OSError:
        return [], offset
    return events, offset


def iter_sessions_with_perf(*, base: Path | None = None) -> Iterator[tuple[str, Path, int]]:
    """Duyệt các session có file perf; trả ``(session_id, path, size_bytes)``."""
    root = base or traces_dir()
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        path = entry / PERF_FILENAME
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 0:
            yield entry.name, path, size
