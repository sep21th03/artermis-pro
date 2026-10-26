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

"""Session resolution and the agent-activity list of the Test Console.

``_resolve_session`` used to accept any session with the same goal text within
±180s and cache it. Re-running a prompt therefore showed the PREVIOUS run's
session — which meant zero steps on screen (the old session had none) while the
new run was tapping away. Two runs of the same prompt ended up sharing one
session id.

``_activity`` exists because a task can finish with no tap/swipe step at all
(the agent only observes and reports), leaving the "Các bước" tab empty even
though time and money were spent.
"""

import importlib

console_server = importlib.import_module("apps.test_console.server")


def _task(**overrides):
    base = {
        "id": "t1",
        "goal": "gạt app đi và vào lại",
        "profile": "flash",
        "package": "com.iot.hunonic",
        "serial": "SERIAL",
        "test_name": "smoke",
    }
    base.update(overrides)
    return console_server.Task(**base)


def _sessions(monkeypatch, sessions):
    monkeypatch.setattr(console_server, "_http_json", lambda url, timeout=10: sessions)
    monkeypatch.setattr(console_server.runner, "history", lambda limit=30: [])


def test_a_session_from_an_earlier_run_is_not_reused(monkeypatch):
    """The old code matched by goal text and ±180s, so it grabbed the previous run."""
    task = _task(started_at=1000.0)
    _sessions(monkeypatch, [{"session_id": "old", "initial_goal": task.goal, "start_time": 900.0}])
    assert console_server._resolve_session(task) == ""
    assert task.session_id == ""


def test_the_session_that_started_after_the_task_wins(monkeypatch):
    task = _task(started_at=1000.0)
    _sessions(
        monkeypatch,
        [
            {"session_id": "old", "initial_goal": task.goal, "start_time": 900.0},
            {"session_id": "mine", "initial_goal": task.goal, "start_time": 1002.0},
        ],
    )
    assert console_server._resolve_session(task) == "mine"
    assert task.session_source == "guess"


def test_a_session_another_task_owns_is_skipped(monkeypatch):
    task = _task(started_at=1000.0)
    monkeypatch.setattr(
        console_server,
        "_http_json",
        lambda url, timeout=10: [
            {"session_id": "taken", "initial_goal": task.goal, "start_time": 1001.0}
        ],
    )
    monkeypatch.setattr(
        console_server.runner, "history", lambda limit=30: [{"id": "other", "session_id": "taken"}]
    )
    assert console_server._resolve_session(task) == ""


def test_session_read_from_stdout_is_authoritative(monkeypatch):
    task = _task(started_at=1000.0, session_id="from-stdout", session_source="stdout")
    asked = []
    monkeypatch.setattr(
        console_server, "_http_json", lambda url, timeout=10: asked.append(url) or []
    )
    assert console_server._resolve_session(task) == "from-stdout"
    assert asked == [], "session đọc từ stdout không được tra lại danh sách"


TRACE = """\
ℹ Starting initial app launch for package: com.iot.hunonic
ℹ App com.iot.hunonic is already in foreground
ℹ --- Artemis Flash Turn 1/25 ---
ℹ Executing Flash tool: press_key({'key': 'HOME'})
ℹ Recorded step 1 for session abc
                    INFO     HTTP Request: POST                  _client.py:1923
ℹ Delaying 0.4s before capturing screen state...
❌ Timeout waiting for com.example.app to load after 15s
⚠ Attempt 1 failed: Timeout waiting for com.example.app to load after 15s
ℹ LLM usage for session abc: 1 calls, 100 prompt tokens
"""


def test_activity_keeps_agent_work_and_drops_infra_noise(monkeypatch):
    monkeypatch.setattr(console_server, "_trace_text", lambda task: TRACE)
    items = console_server._activity(_task())
    assert [item["kind"] for item in items] == ["info", "info", "turn", "tool", "error", "warn"]
    assert items[2]["label"] == "Lượt 1/25"
    assert items[3]["label"] == "Nhấn phím"
    assert "HOME" in items[3]["detail"]
    labels = " ".join(item["label"] for item in items)
    assert "HTTP Request" not in labels
    assert "Recorded step" not in labels
    assert "LLM usage" not in labels


def test_activity_is_empty_without_a_trace(monkeypatch):
    monkeypatch.setattr(console_server, "_trace_text", lambda task: "")
    assert console_server._activity(_task()) == []


def test_activity_captures_the_failure_reason(monkeypatch):
    trace = (
        "ℹ --- Artemis Flash Turn 1/25 ---\n"
        "⚠ [web_1] FlashRunner failed: no login button visible\n"
    )
    monkeypatch.setattr(console_server, "_trace_text", lambda task: trace)
    items = console_server._activity(_task())
    assert items[-1]["kind"] == "error"
    assert "no login button visible" in items[-1]["label"]
