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

"""Budget guard of the Test Console proxy.

The CLI talks to the proxy as one synthetic "external" task, so a stale 480s
timer used to reject every later request with `429 budget_exceeded` until the
console was restarted.
"""

import importlib
import time

console_server = importlib.import_module("apps.test_console.server")

_CHAT_BODY = {"messages": [{"role": "user", "content": "ping"}]}


def _budget(monkeypatch, tmp_path):
    monkeypatch.setattr(console_server, "BUDGET_FILE", tmp_path / "budget.json")
    budget = console_server.Budget()
    budget.max_task_seconds = 480
    budget.external_idle_reset = 120.0
    return budget


def test_reset_clears_the_block_reason(monkeypatch, tmp_path):
    budget = _budget(monkeypatch, tmp_path)
    budget.start_task("external")
    budget._exceeded = "Task chạy quá 480s."

    budget.reset()

    assert budget.snapshot()["exceeded"] is None
    assert budget.check(_CHAT_BODY, 10) is None


def test_external_task_resets_after_an_idle_gap(monkeypatch, tmp_path):
    budget = _budget(monkeypatch, tmp_path)
    budget.start_task("external")
    budget._exceeded = "Task chạy quá 480s."
    budget._last_request_at = time.time() - budget.external_idle_reset - 1

    assert budget.check(_CHAT_BODY, 10) is None
    assert budget.snapshot()["exceeded"] is None


def test_external_task_stays_blocked_without_an_idle_gap(monkeypatch, tmp_path):
    budget = _budget(monkeypatch, tmp_path)
    budget.start_task("external")
    budget._exceeded = "Task chạy quá 480s."
    budget._last_request_at = time.time()

    assert budget.check(_CHAT_BODY, 10) == "Task chạy quá 480s."
