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

"""API Performance của Mobile Test Console.

Ba tính chất được khoá:

1. **Đọc đúng session.** Số liệu nằm trong ``traces/<session_id>/perf_events.jsonl``;
   console phải đọc file của *chính* session đang chạy, nên không thể lẫn với session
   khác — kể cả khi CLI đã thoát mà daemon vẫn chạy.
2. **Trả lời được trong lúc đang chạy.** Endpoint đọc lại file mỗi lần gọi, nên số liệu
   hiện ra ngay khi có span mới, không cần chờ session kết thúc.
3. **Xuất được JSON** cho báo cáo từng session.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

console_server = importlib.import_module("apps.test_console.server")
perf_module = importlib.import_module("artemis.perf")

SESSION = "11111111-2222-3333-4444-555555555555"


def write_perf(session_id: str, *, base: Path) -> Path:
    """Ghi một file perf tối thiểu: một lượt LLM + một lần tap + một outcome."""
    path = base / session_id / perf_module.PERF_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "session_id": session_id,
            "span_id": "span-llm",
            "event_name": "llm_call",
            "stage": "operator_llm",
            "agent_role": "operator",
            "started_at": 1000.0,
            "ended_at": 1003.0,
            "duration_ms": 3000.0,
            "status": "success",
            "model": "ts/gemini-3.5-flash-lite",
            "input_tokens": 1200,
            "output_tokens": 90,
            "total_tokens": 1290,
            "retry_count": 1,
            "retry_wait_ms": 900.0,
            "ttft_ms": 700.0,
        },
        {
            "session_id": session_id,
            "span_id": "span-tap",
            "event_name": "adb_tap",
            "stage": "adb_action",
            "action_type": "tap",
            "started_at": 1003.0,
            "ended_at": 1003.1,
            "duration_ms": 100.0,
            "status": "success",
            "detail": {"x": 540, "y": 1200},
        },
        {
            "session_id": session_id,
            "span_id": "span-outcome",
            "event_name": "action_outcome",
            "stage": "adb_action",
            "action_type": "tap",
            "started_at": 1003.1,
            "ended_at": 1004.0,
            "duration_ms": 900.0,
            "status": "success",
            "ui_fingerprint": "abc123",
            "ui_changed": False,
            "detail": {"x": 540, "y": 1200},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def traces(tmp_path, monkeypatch):
    """Trỏ console vào một thư mục traces tạm."""
    root = tmp_path / "traces"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(console_server, "ARTEMIS_DIR", tmp_path)
    return root


@pytest.fixture
def client():
    return TestClient(console_server.app)


# ── Route tồn tại & đọc đúng session ─────────────────────────────────────
def test_perf_routes_are_registered():
    registered = set(console_server.app.openapi()["paths"])

    assert "/api/perf/sessions" in registered
    assert "/api/perf/session/{session_id}" in registered
    assert "/api/tasks/{task_id}/performance" in registered
    assert "/api/tasks/{task_id}/performance.json" in registered


def test_a_session_report_contains_the_stage_table_and_the_timeline(traces, client):
    write_perf(SESSION, base=traces)

    response = client.get(f"/api/perf/session/{SESSION}")

    assert response.status_code == 200
    report = response.json()
    stages = {row["stage"]: row for row in report["stages"]}
    assert stages["operator_llm"]["total_ms"] == 3000.0
    assert stages["operator_llm"]["retry_count"] == 1
    assert report["summary"]["llm_calls"] == 1
    assert report["summary"]["tokens_total"] == 1290
    assert [row["event_name"] for row in report["timeline"]][:2] == ["llm_call", "adb_tap"]
    assert report["ready"] is True


def test_retry_time_becomes_a_finding_with_numbers(traces, client):
    write_perf(SESSION, base=traces)

    report = client.get(f"/api/perf/session/{SESSION}").json()

    retry = next(f for f in report["findings"] if f["code"] == "retry_overhead")
    assert retry["metric"]["retry_wait_ms"] == 900.0


def test_an_unknown_session_reports_na_instead_of_zeros(traces, client):
    report = client.get("/api/perf/session/khong-co-session-nay").json()

    assert report["summary"]["actions"] is None
    assert report["summary"]["llm_calls"] is None
    assert report["summary"]["total_session_ms"] is None
    assert report["stages"] == []
    assert report["perf_file_exists"] is False


def test_a_malformed_session_id_is_rejected(traces, client):
    response = client.get("/api/perf/session/..%2F..%2Fetc")

    assert response.status_code in (400, 404)


def test_sessions_are_listed_only_when_they_have_perf_data(traces, client):
    write_perf(SESSION, base=traces)
    (traces / "session-khong-co-du-lieu").mkdir()

    rows = client.get("/api/perf/sessions").json()

    assert [row["session_id"] for row in rows] == [SESSION]
