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

"""The console must show the agent's real output, not just the CLI's.

``artemis run`` hands the work to the Artemis Daemon and prints a handful of
scheduling lines, then waits.  Measured on a real run: **12 log lines for an
entire task**, none of them about an LLM call or an action.  Everything real --
including every call to the shared server -- goes to
``traces/<session>/stdout.log``, which the console used to never read.  The
symptom a tester sees is a task that "hangs" with an empty log.
"""

from __future__ import annotations

import importlib
import threading
import time

import pytest

console_server = importlib.import_module("apps.test_console.server")


@pytest.fixture
def runner(monkeypatch, tmp_path):
    """A Runner whose traces live in ``tmp_path`` so nothing real is touched."""
    monkeypatch.setattr(console_server, "ARTEMIS_DIR", tmp_path)
    return console_server.Runner()


def _task(session_id: str) -> object:
    return console_server.Task(
        id="t1",
        goal="g",
        profile="pro",
        package="p",
        serial="",
        test_name="t",
        max_seconds=60,
        session_id=session_id,
    )


def _texts(runner) -> str:
    with runner._lock:
        return "\n".join(line["text"] for line in runner._lines)


def test_the_trace_log_reaches_the_task_log(runner, tmp_path):
    session = "abc12345-0000-0000-0000-000000000000"
    trace = tmp_path / "traces" / session
    trace.mkdir(parents=True)
    (trace / "stdout.log").write_text(
        "ℹ Planner nhận knowledge context: 3726 ký tự\n"
        "HTTP Request: POST https://sep21th03.com/artemis/api/knowledge/context\n",
        encoding="utf-8",
    )
    task = _task(session)

    thread = threading.Thread(target=runner._tail_trace, args=(task,), daemon=True)
    thread.start()
    deadline = time.time() + 5
    while "knowledge context" not in _texts(runner) and time.time() < deadline:
        time.sleep(0.1)
    task.ended_at = time.time()
    thread.join(timeout=5)

    logged = _texts(runner)
    assert "knowledge context" in logged
    assert "sep21th03.com/artemis/api/knowledge/context" in logged


def test_lines_written_after_the_tail_started_are_picked_up(runner, tmp_path):
    """The log grows while the task runs; the tail must follow it."""
    session = "abc12345-0000-0000-0000-000000000000"
    trace = tmp_path / "traces" / session
    trace.mkdir(parents=True)
    log = trace / "stdout.log"
    log.write_text("dong dau\n", encoding="utf-8")
    task = _task(session)

    thread = threading.Thread(target=runner._tail_trace, args=(task,), daemon=True)
    thread.start()
    deadline = time.time() + 5
    while "dong dau" not in _texts(runner) and time.time() < deadline:
        time.sleep(0.1)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("dong sau\n")
    while "dong sau" not in _texts(runner) and time.time() < deadline:
        time.sleep(0.1)
    task.ended_at = time.time()
    thread.join(timeout=5)

    assert "dong sau" in _texts(runner)


def test_a_missing_trace_file_is_not_an_error(runner):
    """The session may never produce a trace; the tail must just give up quietly."""
    task = _task("khong-co-trace-nay")
    task.ended_at = time.time()
    runner._tail_trace(task)  # không được raise
    assert "khong-co-trace-nay" not in _texts(runner)


def test_the_log_says_where_it_came_from(runner, tmp_path):
    """A tester reading the log must know which file it is."""
    session = "abc12345-0000-0000-0000-000000000000"
    trace = tmp_path / "traces" / session
    trace.mkdir(parents=True)
    (trace / "stdout.log").write_text("x\n", encoding="utf-8")
    task = _task(session)

    thread = threading.Thread(target=runner._tail_trace, args=(task,), daemon=True)
    thread.start()
    deadline = time.time() + 5
    while "stdout.log" not in _texts(runner) and time.time() < deadline:
        time.sleep(0.1)
    task.ended_at = time.time()
    thread.join(timeout=5)

    assert "traces/<session>/stdout.log" in _texts(runner)
    assert session[:8] in _texts(runner)
