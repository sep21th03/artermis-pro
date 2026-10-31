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

"""Tab "Các bước" và dòng "đang chờ gì" của console.

Hai lỗi thật được khoá ở đây:

1. **Bước nhiều hành động làm sập tab.** Backend trả ``action_taken`` dạng ``list`` cho
   bước nhiều hành động (burst), còn code cũ giả định ``dict`` ⇒
   ``AttributeError: 'list' object has no attribute 'get'`` ⇒ endpoint 500 và tab trắng
   trơn dù agent đã tap/swipe thật.
2. **Không ai nói được agent đang chờ gì.** ``/api/tasks/{id}/now`` trả span đang mở, thời
   gian im lặng và lượt LLM gần nhất, để câu "đứng yên một màn cả phút" có câu trả lời
   bằng số đo thay vì phải đoán.
"""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

console_server = importlib.import_module("apps.test_console.server")
perf_module = importlib.import_module("artemis.perf")

SESSION = "0f26a494-2f88-4e8f-9d5b-3331c9908142"


# ── Bước: ba dạng action_taken ───────────────────────────────────────────
@pytest.mark.parametrize(
    ("action_taken", "expected"),
    [
        (None, []),
        ({"action": "tap"}, [{"action": "tap"}]),
        ([{"action": "tap"}, {"action": "swipe"}], [{"action": "tap"}, {"action": "swipe"}]),
        ([{"action": "tap"}, "rác", None], [{"action": "tap"}]),
        ("chuỗi lạ", []),
    ],
)
def test_step_actions_normalises_every_shape(action_taken, expected):
    assert console_server._step_actions({"action_taken": action_taken}) == expected


def test_a_burst_step_renders_one_row_per_action(monkeypatch):
    task = console_server.Task(id="t1", goal="g", profile="flash", package="p", serial="s", test_name="t")
    backend_steps = [
        {"step_id": "pre-planning", "step_type": "planning", "step_number": 0, "action_taken": None},
        {
            "step_number": 1,
            "action_taken": [
                {"action": "tap", "coordinates": [648, 1506]},
                {"action": "swipe", "coordinates": [432, 1046, 432, 489]},
            ],
            "last_execution_result": {"status": "success", "result": "ok"},
            "operator_raw_thinking": "bấm Đăng xuất rồi cuộn",
            "total_tokens": 123,
        },
    ]
    monkeypatch.setattr(console_server, "_resolve_session", lambda _task: SESSION)
    monkeypatch.setattr(console_server, "_http_json", lambda url, timeout=10: backend_steps)

    steps = console_server._fetch_steps(task)

    # Bước planning vẫn hiện (đó là thời gian agent đã tiêu), bước burst thành 2 dòng.
    assert [row["number"] for row in steps] == [0, 1, 1]
    assert [row["action"] for row in steps] == ["", "tap", "swipe"]
    assert steps[-1]["thinking"] == "bấm Đăng xuất rồi cuộn"


def test_a_broken_step_does_not_lose_the_whole_tab(monkeypatch):
    task = console_server.Task(id="t1", goal="g", profile="flash", package="p", serial="s", test_name="t")
    backend_steps = [
        "rác",
        {"step_number": 1, "action_taken": {"action": "tap"}},
        {"step_number": 2, "action_taken": {"action": ["lạ"]}},
    ]
    monkeypatch.setattr(console_server, "_resolve_session", lambda _task: SESSION)
    monkeypatch.setattr(console_server, "_http_json", lambda url, timeout=10: backend_steps)

    steps = console_server._fetch_steps(task)

    assert len(steps) >= 1  # bước hợp lệ vẫn hiện


def test_a_dict_response_is_not_iterated(monkeypatch):
    task = console_server.Task(id="t1", goal="g", profile="flash", package="p", serial="s", test_name="t")
    monkeypatch.setattr(console_server, "_resolve_session", lambda _task: SESSION)
    monkeypatch.setattr(
        console_server, "_http_json", lambda url, timeout=10: {"error": "không có session"}
    )

    assert console_server._fetch_steps(task) == []


# ── Endpoint "đang chờ gì" ───────────────────────────────────────────────
@pytest.fixture
def perf_session(tmp_path, monkeypatch):
    """File perf có một span gốc còn đang mở + một lượt LLM và một hành động đã xong."""
    monkeypatch.setattr(console_server, "ARTEMIS_DIR", tmp_path)
    path = tmp_path / "traces" / SESSION / perf_module.PERF_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "session_id": SESSION,
            "span_id": "root",
            "event_name": "operator",
            "stage": "agent",
            "started_at": 1_000.0,
            "ended_at": None,
            "status": "running",
        },
        {
            "session_id": SESSION,
            "span_id": "llm",
            "event_name": "llm_call",
            "stage": "operator_llm",
            "started_at": 1_000.0,
            "ended_at": 1_018.8,
            "duration_ms": 18800.0,
            "status": "success",
            "ttft_ms": 18780.0,
            "input_tokens": 16624,
            "output_tokens": 161,
            # Model thật của lượt gọi: sau khi model chính cạn quota, đây là chỗ duy nhất tester
            # thấy được nó đã xoay sang model nào.
            "model": "gemini-3.1-flash-lite",
            "provider": "google",
            "endpoint": "google:gemini-3.1-flash-lite",
        },
        {
            "session_id": SESSION,
            "span_id": "tap",
            "event_name": "adb_tap",
            "stage": "adb_action",
            "action_type": "tap",
            "started_at": 1_019.0,
            "ended_at": 1_019.1,
            "duration_ms": 120.0,
            "status": "success",
            "ui_changed": True,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


def test_now_reports_what_the_agent_is_waiting_on(perf_session, monkeypatch):
    task = console_server.Task(
        id="t1", goal="g", profile="flash", package="p", serial="s", test_name="t", session_id=SESSION
    )
    task.session_source = "stdout"
    monkeypatch.setattr(console_server.runner, "get_task", lambda _id: task)

    data = TestClient(console_server.app).get("/api/tasks/t1/now").json()

    assert data["ready"] is True
    assert data["in_flight"]["stage"] == "agent"
    assert data["in_flight"]["event_name"] == "operator"
    assert data["in_flight"]["elapsed_ms"] is not None
    assert data["last_llm"]["ttft_ms"] == 18780.0
    assert data["last_llm"]["input_tokens"] == 16624
    # "Chạy model nào" phải nhìn thấy được: đây là model THẬT của lượt gọi vừa xong.
    assert data["last_llm"]["model"] == "gemini-3.1-flash-lite"
    assert data["last_llm"]["endpoint"] == "google:gemini-3.1-flash-lite"
    assert data["last_action"]["action_type"] == "tap"
    assert data["last_events"][0]["event_name"] == "adb_tap"


def test_now_says_so_when_there_is_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr(console_server, "ARTEMIS_DIR", tmp_path)
    task = console_server.Task(
        id="t1", goal="g", profile="flash", package="p", serial="s", test_name="t", session_id=SESSION
    )
    task.session_source = "stdout"
    monkeypatch.setattr(console_server.runner, "get_task", lambda _id: task)

    data = TestClient(console_server.app).get("/api/tasks/t1/now").json()

    assert data["ready"] is False
    assert data["note"]
