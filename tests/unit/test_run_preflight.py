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

"""Fail-fast preflights for `artemis run` (LLM endpoint + device presence).

Both exist because the CLI waits up to 1800s for a daemon task: a dead local
LLM endpoint or a missing device used to read as a hang.
"""

import importlib

import pytest
from rich.console import Console

from artemis.runtime.adb_status import AdbDeviceInfo, AdbEndpoint, AdbEndpointStatus

run_module = importlib.import_module("artemis.interfaces.cli.commands.run")
console = Console()


def _status(devices, host="127.0.0.1", port=5037, reachable=True):
    return AdbEndpointStatus(
        endpoint=AdbEndpoint.create(host, port), reachable=reachable, devices=devices
    )


def test_llm_preflight_skips_unset_and_remote_endpoints(monkeypatch):
    monkeypatch.setattr(run_module.settings, "OPENAI_BASE_URL", None, raising=False)
    run_module._preflight_llm_endpoint(console)

    monkeypatch.setattr(
        run_module.settings, "OPENAI_BASE_URL", "https://api.vilao.ai/v1", raising=False
    )
    run_module._preflight_llm_endpoint(console)


def test_llm_preflight_fails_fast_for_dead_loopback_endpoint(monkeypatch):
    monkeypatch.setattr(
        run_module.settings, "OPENAI_BASE_URL", "http://127.0.0.1:65533/v1", raising=False
    )
    monkeypatch.setattr(run_module, "tcp_reachable", lambda *a, **k: False)

    with pytest.raises(SystemExit) as excinfo:
        run_module._preflight_llm_endpoint(console)
    assert excinfo.value.code == 2


def test_device_preflight_passes_with_a_ready_device(monkeypatch):
    devices = [AdbDeviceInfo(serial="SERIAL-1", state="device")]
    monkeypatch.setattr(run_module, "probe_adb_endpoint", lambda: _status(devices))
    monkeypatch.delenv("ARTEMIS_ALLOW_QUEUE_WITHOUT_DEVICE", raising=False)

    run_module._preflight_device(console, None)
    run_module._preflight_device(console, "SERIAL-1")


def test_device_preflight_rejects_unknown_serial(monkeypatch):
    devices = [AdbDeviceInfo(serial="SERIAL-1", state="device")]
    monkeypatch.setattr(run_module, "probe_adb_endpoint", lambda: _status(devices))

    with pytest.raises(SystemExit) as excinfo:
        run_module._preflight_device(console, "OTHER-SERIAL")
    assert excinfo.value.code == 2


def test_device_preflight_fails_without_device_and_honours_queue_override(monkeypatch):
    monkeypatch.setattr(run_module, "probe_adb_endpoint", lambda: _status([]))
    monkeypatch.delenv("ARTEMIS_ALLOW_QUEUE_WITHOUT_DEVICE", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        run_module._preflight_device(console, None)
    assert excinfo.value.code == 2

    monkeypatch.setenv("ARTEMIS_ALLOW_QUEUE_WITHOUT_DEVICE", "1")
    run_module._preflight_device(console, None)


def test_device_preflight_reports_unauthorized(monkeypatch):
    devices = [AdbDeviceInfo(serial="SERIAL-1", state="unauthorized")]
    monkeypatch.setattr(run_module, "probe_adb_endpoint", lambda: _status(devices))
    monkeypatch.delenv("ARTEMIS_ALLOW_QUEUE_WITHOUT_DEVICE", raising=False)

    with pytest.raises(SystemExit):
        run_module._preflight_device(console, None)
