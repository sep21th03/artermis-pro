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

"""Health and device inventory for the configured ADB server endpoint.

One place that answers "can this machine reach the ADB server, and what is
attached to it" for the CLI (``artemis adb``), the readiness probe
(``artemis doctor``) and the Admin Console. Everything goes through
:func:`adb_command`, so a remote ``ADB_HOST``/``ADB_PORT`` is honoured and no
code path silently falls back to the local server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import socket
import subprocess
from typing import Any

from artemis.runtime.adb_endpoint import AdbEndpoint, adb_command, current_adb_endpoint

#: ADB states that mean "a human must act on the machine with the USB cable".
UNAUTHORIZED_STATES = frozenset({"unauthorized", "authorizing"})
OFFLINE_STATES = frozenset({"offline", "connecting", "recovery", "sideload", "bootloader"})


@dataclass(frozen=True)
class AdbDeviceInfo:
    """One device as reported by ``adb devices -l``."""

    serial: str
    state: str
    model: str | None = None
    product: str | None = None
    transport_id: str | None = None

    @property
    def usable(self) -> bool:
        return self.state == "device"

    def to_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "state": self.state,
            "model": self.model,
            "product": self.product,
            "transport_id": self.transport_id,
            "usable": self.usable,
        }


@dataclass
class AdbEndpointStatus:
    """Result of probing one ADB server endpoint."""

    endpoint: AdbEndpoint
    reachable: bool
    error: str | None = None
    devices: list[AdbDeviceInfo] = field(default_factory=list)
    raw: str = ""

    @property
    def ready_devices(self) -> list[AdbDeviceInfo]:
        return [device for device in self.devices if device.usable]

    @property
    def unauthorized(self) -> list[AdbDeviceInfo]:
        return [device for device in self.devices if device.state in UNAUTHORIZED_STATES]

    @property
    def offline(self) -> list[AdbDeviceInfo]:
        return [device for device in self.devices if device.state in OFFLINE_STATES]

    @property
    def ok(self) -> bool:
        return self.reachable and self.error is None and bool(self.ready_devices)

    def hint(self) -> str | None:
        """One actionable sentence for the operator, or None when healthy."""
        if not self.reachable:
            return (
                "Cannot reach the ADB server. On the tester machine run "
                "`adb -a -P 5037 nodaemon server`, then allow TCP 5037 from the "
                "DEV machine (LAN only - never expose it to the internet)."
            )
        if self.error:
            return self.error
        if self.unauthorized:
            serials = ", ".join(device.serial for device in self.unauthorized)
            return (
                f"Device {serials} is unauthorized: accept the 'Allow USB debugging' "
                "prompt on the tester machine, then retry."
            )
        if self.offline:
            serials = ", ".join(device.serial for device in self.offline)
            return f"Device {serials} is offline: reconnect the USB cable on the tester machine."
        if not self.devices:
            if self.endpoint.is_local_default:
                return (
                    "The ADB server is reachable but no device is attached. Plug in the phone "
                    "and accept the 'Allow USB debugging' prompt."
                )
            return "The ADB server is reachable but no device is attached to the tester machine."
        return None

    def summary(self) -> str:
        if not self.reachable:
            return f"Unreachable ({self.endpoint.host}:{self.endpoint.port})"
        if self.error:
            return f"ADB error: {self.error}"
        if not self.devices:
            return "Reachable, no devices"
        states = ", ".join(f"{device.serial}={device.state}" for device in self.devices)
        return f"{len(self.ready_devices)}/{len(self.devices)} usable ({states})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint.to_dict(),
            "reachable": self.reachable,
            "error": self.error,
            "ok": self.ok,
            "summary": self.summary(),
            "hint": self.hint(),
            "devices": [device.to_dict() for device in self.devices],
        }


def tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """TCP-level reachability of the ADB server (works for LAN hosts too)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parse_devices(output: str) -> list[AdbDeviceInfo]:
    """Parse ``adb devices -l`` output into device records."""
    devices: list[AdbDeviceInfo] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = product = transport_id = None
        for token in parts[2:]:
            key, _, value = token.partition(":")
            if key == "model":
                model = value.replace("_", " ")
            elif key == "product":
                product = value
            elif key == "transport_id":
                transport_id = value
        devices.append(
            AdbDeviceInfo(
                serial=serial,
                state=state,
                model=model,
                product=product,
                transport_id=transport_id,
            )
        )
    return devices


def probe_adb_endpoint(
    endpoint: AdbEndpoint | None = None,
    timeout: float = 5.0,
) -> AdbEndpointStatus:
    """Check reachability and enumerate devices on the configured endpoint."""
    endpoint = endpoint or current_adb_endpoint()
    status = AdbEndpointStatus(endpoint=endpoint, reachable=False)

    # A local endpoint needs no TCP precheck: the adb client starts the local
    # server on demand, so a closed port is not an error yet. Remote endpoints
    # have no such fallback, so prove reachability before reporting "no devices".
    if not endpoint.is_local_default and not tcp_reachable(endpoint.host, endpoint.port):
        return status

    status.reachable = True
    try:
        result = subprocess.run(
            adb_command(["devices", "-l"]),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        status.error = "The adb binary was not found (install Android platform-tools)."
        return status
    except subprocess.TimeoutExpired:
        status.error = f"`adb devices` timed out after {timeout:.0f}s."
        return status
    except OSError as exc:
        status.error = f"Could not run adb: {exc}"
        return status

    status.raw = result.stdout or ""
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        status.error = detail[0] if detail else f"adb exited with {result.returncode}."
        return status

    status.devices = parse_devices(result.stdout or "")
    return status

