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

"""Remote ADB server support: endpoint plumbing, device listing and fallbacks.

Every test here pins the endpoint to a non-loopback host and asserts that the
command actually carries ``-H``/``-P`` (i.e. nothing silently falls back to the
local ADB server). No device or emulator is required.
"""

import asyncio
import importlib
import os
import subprocess

import pytest

from artemis.clients import screen_client_factory
from artemis.config import settings
from artemis.runtime.adb_endpoint import (
    AdbEndpoint,
    adb_command,
    apply_current_endpoint,
    configure_adbutils,
    current_adb_endpoint,
)
from artemis.runtime.adb_status import (
    AdbEndpointStatus,
    parse_devices,
    probe_adb_endpoint,
)
from artemis.runtime.device_pool import DevicePool

device_pool_module = importlib.import_module("artemis.runtime.device_pool")
adb_status_module = importlib.import_module("artemis.runtime.adb_status")

REMOTE_HOST = "10.20.30.40"
REMOTE_PORT = 5037
_ENDPOINT_ENV_KEYS = (
    "ADB_HOST",
    "ADB_PORT",
    "ADB_SERVER_SOCKET",
    "ANDROID_ADB_SERVER_HOST",
    "ANDROID_ADB_SERVER_PORT",
    "ARTEMIS_ADB_ENDPOINT_ID",
)


@pytest.fixture
def remote_endpoint(monkeypatch):
    """Pin the process preference to a remote ADB server for one test."""
    monkeypatch.setattr(settings, "ADB_HOST", REMOTE_HOST, raising=False)
    monkeypatch.setattr(settings, "ADB_PORT", REMOTE_PORT, raising=False)
    monkeypatch.delenv("ARTEMIS_ALLOW_REMOTE_HELPER", raising=False)
    for key in _ENDPOINT_ENV_KEYS:
        monkeypatch.setenv(key, "sentinel")
    return AdbEndpoint.create(REMOTE_HOST, REMOTE_PORT)


@pytest.fixture(autouse=True)
def restore_adbutils_binding():
    """Leave the shared adbutils client on the real endpoint after each test."""
    yield
    configure_adbutils()


def test_environment_exports_adbutils_keys():
    """ADB_HOST/ADB_PORT alone are not enough: adbutils reads ANDROID_* names."""
    env: dict[str, str] = {}
    AdbEndpoint.create(REMOTE_HOST, REMOTE_PORT).apply_to_environment(env)

    assert env["ADB_HOST"] == REMOTE_HOST
    assert env["ADB_PORT"] == str(REMOTE_PORT)
    assert env["ADB_SERVER_SOCKET"] == f"tcp:{REMOTE_HOST}:{REMOTE_PORT}"
    assert env["ANDROID_ADB_SERVER_HOST"] == REMOTE_HOST
    assert env["ANDROID_ADB_SERVER_PORT"] == str(REMOTE_PORT)
    assert env["ARTEMIS_ADB_ENDPOINT_ID"] == f"tcp:{REMOTE_HOST}:{REMOTE_PORT}"


def test_adb_command_targets_configured_endpoint(remote_endpoint):
    command = adb_command(["devices", "-l"])

    assert command[1:5] == ["-H", REMOTE_HOST, "-P", str(REMOTE_PORT)]
    assert command[-2:] == ["devices", "-l"]
    assert current_adb_endpoint().mode == "remote"


def test_apply_current_endpoint_updates_process_and_adbutils(remote_endpoint):
    endpoint = apply_current_endpoint()

    assert os.environ["ANDROID_ADB_SERVER_HOST"] == REMOTE_HOST
    assert os.environ["ANDROID_ADB_SERVER_PORT"] == str(REMOTE_PORT)
    assert endpoint.is_local_default is False

    adbutils = pytest.importorskip("adbutils")
    assert getattr(adbutils.adb, "_BaseClient__host") == REMOTE_HOST
    assert getattr(adbutils.adb, "_BaseClient__port") == REMOTE_PORT


def test_device_pool_queries_configured_endpoint(remote_endpoint, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001 - mirrors subprocess.run
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            "List of devices attached\nABC123\tdevice product:husky model:Pixel_8_Pro\n",
            "",
        )

    monkeypatch.setattr(device_pool_module.subprocess, "run", fake_run)
    raw = DevicePool()._query_adb_devices_sync(timeout=1.0)

    assert raw == [("ABC123", "device", "Pixel 8 Pro", "husky")]
    assert calls, "device enumeration must run adb"
    assert calls[0][1:5] == ["-H", REMOTE_HOST, "-P", str(REMOTE_PORT)]


def test_device_pool_skips_start_server_for_remote(remote_endpoint, monkeypatch):
    started: list[tuple] = []

    async def fake_exec(*command, **kwargs):  # noqa: ANN001
        started.append(command)
        raise AssertionError("adb start-server must not run for a remote endpoint")

    monkeypatch.setattr(
        device_pool_module.asyncio, "create_subprocess_exec", fake_exec
    )
    asyncio.run(DevicePool()._start_adb_server(timeout=1.0))

    assert started == []


def test_helper_backend_is_skipped_for_remote_endpoint(remote_endpoint, monkeypatch):
    """`adb forward` binds on the ADB server host, so the helper cannot be used."""
    created: dict[str, str] = {}

    class DummyUiautomator:
        def __init__(self, device_id=None, **kwargs):  # noqa: ANN001
            created["uiautomator"] = device_id

    class DummyHelper:
        def __init__(self, device_id=None, **kwargs):  # noqa: ANN001
            created["helper"] = device_id

    monkeypatch.setattr(screen_client_factory, "UIAutomatorClient", DummyUiautomator)
    monkeypatch.setattr(screen_client_factory, "AccessibilityClient", DummyHelper)

    assert screen_client_factory.remote_endpoint_blocks_helper() is True
    client = screen_client_factory.create_screen_client("SERIAL-1")
    assert isinstance(client, DummyUiautomator)
    assert created == {"uiautomator": "SERIAL-1"}

    monkeypatch.setenv("ARTEMIS_ALLOW_REMOTE_HELPER", "1")
    assert screen_client_factory.remote_endpoint_blocks_helper() is False
    assert isinstance(
        screen_client_factory.create_screen_client("SERIAL-2", backend="helper"), DummyHelper
    )


def test_parse_devices_reports_states_and_hints():
    devices = parse_devices(
        "List of devices attached\n"
        "S1\tunauthorized usb:1-1\n"
        "S2\toffline\n"
        "S3\tdevice product:husky model:Pixel_8_Pro transport_id:5\n"
    )

    assert [device.state for device in devices] == ["unauthorized", "offline", "device"]
    status = AdbEndpointStatus(
        endpoint=AdbEndpoint.create(REMOTE_HOST, REMOTE_PORT),
        reachable=True,
        devices=devices,
    )
    assert status.unauthorized[0].serial == "S1"
    assert status.offline[0].serial == "S2"
    assert [device.serial for device in status.ready_devices] == ["S3"]
    assert status.ok is True
    assert "Allow USB debugging" in (status.hint() or "")


def test_probe_reports_unreachable_remote_endpoint(monkeypatch):
    monkeypatch.setattr(adb_status_module, "tcp_reachable", lambda *a, **k: False)

    status = probe_adb_endpoint(AdbEndpoint.create("192.0.2.10", 5037))

    assert status.reachable is False
    assert status.ok is False
    assert "nodaemon server" in (status.hint() or "")
    payload = status.to_dict()
    assert payload["endpoint"]["mode"] == "remote"
    assert payload["devices"] == []


def test_probe_enumerates_devices_through_configured_endpoint(remote_endpoint, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command, 0, "List of devices attached\nXYZ789\tdevice model:Pixel_9\n", ""
        )

    monkeypatch.setattr(adb_status_module, "tcp_reachable", lambda *a, **k: True)
    monkeypatch.setattr(adb_status_module.subprocess, "run", fake_run)

    status = probe_adb_endpoint()

    assert calls[0][1:5] == ["-H", REMOTE_HOST, "-P", str(REMOTE_PORT)]
    assert [device.serial for device in status.ready_devices] == ["XYZ789"]
    assert status.hint() is None
