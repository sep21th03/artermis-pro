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

"""``flash.step_summarizer.enabled`` must actually switch the service off.

``FlashRunner`` always honoured the flag.  Every other profile did not: the
composition root in ``artemis/memory`` read ``.model`` and never looked at
``.enabled``, so a Pro run built the summarizer anyway and paid for it.

Measured on a real Pro run (trace ``232d2382``): 15 steps x 4 attempts =
**60 vision calls, every one a 404** — ``45x "Retrying" + 15x "Giving up after
4 failed attempts"``.  The service calls Google directly (``get_google_llm``)
with the configured ``ts/gemini-3.5-flash-lite``, a name that endpoint does not
know.  Each attempt ships two screenshots, so the waste is bandwidth as well as
time, and because the call bypasses the console proxy it is neither metered nor
visible in the proxy log.
"""

from __future__ import annotations

import pytest

from artemis.config.agent import StepSummarizerConfig
from artemis.memory import ensure_step_memory


class FakeContext:
    """Minimal stand-in: the composition root only reads/writes ``step_memory``."""


@pytest.fixture
def disabled(monkeypatch):
    def _install(enabled: bool, model: str = "ts/gemini-3.5-flash-lite"):
        class _Cfg:
            step_summarizer = StepSummarizerConfig(enabled=enabled, model=model)

        class _Agent:
            flash = _Cfg()

        monkeypatch.setattr("artemis.config.load_agent_config", lambda: _Agent())

    return _install


def test_a_disabled_summarizer_returns_none(disabled):
    disabled(enabled=False)
    assert ensure_step_memory(FakeContext()) is None


def test_a_disabled_summarizer_does_not_publish_itself_on_the_context(disabled):
    """Publishing a half-built service would let later callers use it anyway."""
    disabled(enabled=False)
    ctx = FakeContext()
    ensure_step_memory(ctx)
    assert getattr(ctx, "step_memory", None) is None


def test_an_enabled_summarizer_is_still_built(disabled):
    disabled(enabled=True)
    assert ensure_step_memory(FakeContext()) is not None


def test_an_existing_service_is_reused_whatever_the_flag_says(disabled):
    """A co-resident consumer that already built one keeps sharing it."""
    disabled(enabled=False)
    ctx = FakeContext()
    existing = object()
    ctx.step_memory = existing
    assert ensure_step_memory(ctx) is existing


def test_the_shipped_config_has_it_off():
    """The repository ships ``enabled: false``; this test fails if that drifts."""
    from artemis.config import load_agent_config

    assert load_agent_config().flash.step_summarizer.enabled is False
