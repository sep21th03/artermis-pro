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

"""Instrumentation thực tế: ADB driver, LLM wrapper, và overhead khi tắt/bật.

Test chạy trên đối tượng thật (driver ADB với device giả, wrapper LLM với model giả) để
bảo đảm span được phát ra ở đúng chỗ — không phải chỉ đúng ở tầng thư viện.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from artemis.perf import PerfRecorder, PerfStage, build_report, set_recorder


def driver_perf_path() -> Path:
    return Path(tempfile.mkdtemp()) / "perf_events.jsonl"


@pytest.fixture
def recorder(tmp_path):
    rec = PerfRecorder("sess-adb", path=tmp_path / "perf_events.jsonl", enabled=True)
    set_recorder(rec)
    yield rec
    set_recorder(None)


class FakeDevice:
    """Device ADB tối thiểu: ghi lại lệnh shell và trả kết quả rỗng."""

    def __init__(self):
        self.commands: list[str] = []

    def shell(self, command: str):
        self.commands.append(command)
        time.sleep(0.005)
        return ""

    def screenshot(self):
        class _Img:
            size = (1080, 2400)

            def save(self, buf, format="PNG"):
                buf.write(b"\x89PNG\r\n")

        return _Img()


class FakeUiClient:
    def __init__(self, hierarchy: str = "<hierarchy><node text='A'/></hierarchy>"):
        self.hierarchy = hierarchy

    def get_hierarchy(self):
        return self.hierarchy


def make_driver(monkeypatch, ui_client=None):
    from artemis.drivers.android import adb_driver as module

    driver = module.AndroidAdbDriver("SERIAL", adb_client=None, ui_adb_client=ui_client)
    fake = FakeDevice()
    monkeypatch.setattr(type(driver), "device", property(lambda self: fake), raising=False)
    return driver, fake


# ── ADB ──────────────────────────────────────────────────────────────────
def test_tap_emits_an_action_span_with_coordinates(recorder, monkeypatch):
    driver, _ = make_driver(monkeypatch)

    assert asyncio.run(driver.tap(540, 1200)) is True

    event = recorder.snapshot()[-1]
    assert event.stage == PerfStage.ADB_ACTION
    assert event.action_type == "tap"
    assert event.detail["x"] == 540 and event.detail["y"] == 1200
    assert event.duration_ms is not None and event.duration_ms >= 5


def test_swipe_and_press_key_are_measured(recorder, monkeypatch):
    driver, _ = make_driver(monkeypatch)

    asyncio.run(driver.swipe(10, 20, 30, 40))
    asyncio.run(driver.press_key("back"))

    kinds = [event.action_type for event in recorder.snapshot()]
    assert kinds == ["swipe", "press_key"]


def test_a_failing_action_is_recorded_as_error(recorder, monkeypatch):
    driver, fake = make_driver(monkeypatch)

    def boom(_command):
        raise RuntimeError("adb offline")

    monkeypatch.setattr(fake, "shell", boom)

    assert asyncio.run(driver.tap(1, 2)) is False

    event = recorder.snapshot()[-1]
    assert event.status == "error"
    assert event.error_type == "RuntimeError"


def test_input_text_never_records_the_typed_text(recorder, monkeypatch):
    """Mật khẩu có thể được gõ ở đây: log chỉ được có độ dài."""
    driver, _ = make_driver(monkeypatch)

    asyncio.run(driver.input_text("matkhau-sieu-bi-mat", clear_existing=False))

    event = recorder.snapshot()[-1]
    assert event.action_type == "input_text"
    assert event.detail["length"] == len("matkhau-sieu-bi-mat")
    assert "matkhau" not in str(event.detail)


def test_screen_observation_splits_into_settling_screenshot_and_hierarchy(recorder, monkeypatch):
    driver, _ = make_driver(monkeypatch, ui_client=FakeUiClient())

    asyncio.run(driver.get_screen_data())

    stages = [event.stage for event in recorder.snapshot()]
    assert PerfStage.SCREEN_SETTLING in stages
    assert PerfStage.SCREENSHOT in stages
    assert PerfStage.UI_HIERARCHY in stages


def test_the_next_observation_reports_whether_the_action_changed_the_ui(recorder, monkeypatch):
    """Chuỗi hành động → quan sát là cách duy nhất biết UI có đổi hay không."""
    driver, _ = make_driver(monkeypatch, ui_client=FakeUiClient("<hierarchy/>"))

    asyncio.run(driver.tap(10, 10))
    asyncio.run(driver.get_screen_data())
    asyncio.run(driver.tap(10, 10))
    asyncio.run(driver.get_screen_data())  # cùng hierarchy → UI không đổi

    outcomes = [event for event in recorder.snapshot() if event.event_name == "action_outcome"]
    assert len(outcomes) == 2
    assert outcomes[0].ui_changed is None  # lần đầu chưa có mốc so sánh
    assert outcomes[1].ui_changed is False
    assert outcomes[1].ui_fingerprint
    assert outcomes[1].duration_ms is not None


def test_a_changed_hierarchy_is_reported_as_a_ui_change(recorder, monkeypatch):
    ui = FakeUiClient("<hierarchy><node text='A'/></hierarchy>")
    driver, _ = make_driver(monkeypatch, ui_client=ui)

    asyncio.run(driver.get_screen_data())
    asyncio.run(driver.tap(5, 5))
    ui.hierarchy = "<hierarchy><node text='B'/></hierarchy>"
    asyncio.run(driver.get_screen_data())

    outcome = [e for e in recorder.snapshot() if e.event_name == "action_outcome"][-1]
    assert outcome.ui_changed is True


def test_repeated_taps_are_detected_end_to_end(recorder, monkeypatch):
    driver, _ = make_driver(monkeypatch, ui_client=FakeUiClient("<hierarchy/>"))

    for _ in range(4):
        asyncio.run(driver.tap(540, 1200))
        asyncio.run(driver.get_screen_data())

    report = build_report(recorder.snapshot(), session_id="sess-adb")

    assert report["loops"], "tap lặp cùng chỗ mà UI không đổi phải bị phát hiện"
    assert report["loops"][0]["count"] >= 3
    assert report["summary"]["actions_without_ui_change"] >= 3


def test_monitoring_overhead_is_small_compared_to_no_monitoring(monkeypatch):
    """Overhead = chênh lệch giữa có và không có lớp đo, không phải tổng thời gian.

    Đo cả hai chế độ trên cùng một device giả (không sleep) để tách chi phí của lớp đo
    khỏi chi phí thật của ADB.
    """
    driver, fake = make_driver(monkeypatch)
    monkeypatch.setattr(fake, "shell", lambda command: "")

    async def hammer(count: int) -> None:
        for _ in range(count):
            await driver.tap(10, 10)

    set_recorder(None)
    start = time.perf_counter()
    asyncio.run(hammer(300))
    without_ms = (time.perf_counter() - start) * 1000.0 / 300

    rec = PerfRecorder("overhead", path=driver_perf_path(), enabled=True)
    set_recorder(rec)
    try:
        start = time.perf_counter()
        asyncio.run(hammer(300))
        with_ms = (time.perf_counter() - start) * 1000.0 / 300
    finally:
        set_recorder(None)

    assert rec.written == 300
    assert with_ms - without_ms < 2.0, (
        f"lớp đo thêm {with_ms - without_ms:.2f}ms mỗi lần tap "
        f"(có: {with_ms:.2f}ms, không: {without_ms:.2f}ms)"
    )
