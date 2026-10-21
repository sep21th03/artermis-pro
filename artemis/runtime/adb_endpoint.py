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

"""Immutable ADB endpoint and task target primitives.

The selected endpoint is a user preference. An :class:`AdbTarget` is an
execution snapshot. Keeping those concepts separate prevents a queued or
running task from silently moving to another ADB server when the preference
changes in the Admin Console.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
import os
import re
import subprocess
from typing import Any, Mapping, MutableMapping, Sequence

from artemis.config import settings
from artemis.config.constants import DEFAULT_ADB_HOST, DEFAULT_ADB_PORT
from artemis.toolchain import toolchain

logger = logging.getLogger(__name__)

ADB_ENDPOINT_ID_ENV = "ARTEMIS_ADB_ENDPOINT_ID"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_HOST_PATTERN = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")


class InvalidAdbEndpoint(ValueError):
    """Raised when an ADB server endpoint is malformed."""


@dataclass(frozen=True, slots=True)
class AdbEndpoint:
    """Network address of one ADB server."""

    host: str
    port: int

    @classmethod
    def create(cls, host: str, port: int) -> AdbEndpoint:
        clean_host = str(host).strip()
        if clean_host.startswith("[") and clean_host.endswith("]"):
            clean_host = clean_host[1:-1]
        if not clean_host:
            raise InvalidAdbEndpoint("ADB server host cannot be empty.")
        if len(clean_host) > 253 or not _SAFE_HOST_PATTERN.fullmatch(clean_host):
            raise InvalidAdbEndpoint(
                "Enter a valid IP address or host name without a URL scheme or path."
            )
        if not 1 <= int(port) <= 65535:
            raise InvalidAdbEndpoint("ADB server port must be between 1 and 65535.")
        return cls(host=clean_host, port=int(port))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AdbEndpoint:
        return cls.create(str(value.get("host", "")), int(value.get("port", 0)))

    @classmethod
    def local(cls) -> AdbEndpoint:
        return cls(DEFAULT_ADB_HOST, DEFAULT_ADB_PORT)

    @property
    def socket(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"tcp:{host}:{self.port}"

    @property
    def identity(self) -> str:
        """Stable identity used for task snapshots and device-lock scoping."""
        return self.socket.lower()

    @property
    def is_local_default(self) -> bool:
        return self.host.lower() in _LOCAL_HOSTS and self.port == DEFAULT_ADB_PORT

    @property
    def mode(self) -> str:
        return "local" if self.is_local_default else "remote"

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "socket": self.socket,
            "identity": self.identity,
            "mode": self.mode,
            "is_local_default": self.is_local_default,
        }

    def apply_to_environment(
        self,
        environment: MutableMapping[str, str] | None = None,
    ) -> MutableMapping[str, str]:
        target = environment if environment is not None else os.environ
        target["ADB_HOST"] = self.host
        target["ADB_PORT"] = str(self.port)
        target["ADB_SERVER_SOCKET"] = self.socket
        # adbutils (and therefore uiautomator2) only reads these two; ADB_HOST /
        # ADB_PORT alone would silently leave it on 127.0.0.1:5037.
        target["ANDROID_ADB_SERVER_HOST"] = self.host
        target["ANDROID_ADB_SERVER_PORT"] = str(self.port)
        target[ADB_ENDPOINT_ID_ENV] = self.identity
        return target


@dataclass(frozen=True, slots=True)
class AdbTarget:
    """A device serial bound to the ADB endpoint that discovered it."""

    endpoint: AdbEndpoint
    serial: str | None = None

    @property
    def lock_scope(self) -> str:
        return self.endpoint.identity

    @property
    def lock_key(self) -> str:
        return f"{self.lock_scope}/{self.serial or 'pending'}"

    def to_dict(self) -> dict[str, Any]:
        return {"endpoint": self.endpoint.to_dict(), "serial": self.serial}


class AdbSession:
    """Execute ADB commands against one explicit, immutable endpoint."""

    def __init__(self, endpoint: AdbEndpoint, adb_path: str | None = None) -> None:
        self.endpoint = endpoint
        self.adb_path = adb_path or toolchain.resolve("adb")

    def command(self, arguments: Sequence[str]) -> list[str]:
        if not self.adb_path:
            raise FileNotFoundError("Android Platform Tools (adb) could not be found.")
        return [
            self.adb_path,
            "-H",
            self.endpoint.host,
            "-P",
            str(self.endpoint.port),
            *arguments,
        ]

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        environment = dict(base if base is not None else os.environ)
        self.endpoint.apply_to_environment(environment)
        return environment

    def run(self, arguments: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        kwargs.setdefault("env", self.environment())
        return subprocess.run(self.command(arguments), **kwargs)


def current_adb_endpoint() -> AdbEndpoint:
    """Return the process preference as an immutable endpoint snapshot."""
    host = settings.ADB_HOST or os.environ.get("ADB_HOST") or DEFAULT_ADB_HOST
    port = settings.ADB_PORT or os.environ.get("ADB_PORT") or DEFAULT_ADB_PORT
    return AdbEndpoint.create(str(host), int(port))


def configure_adbutils(endpoint: AdbEndpoint | None = None) -> AdbEndpoint:
    """Point adbutils (and uiautomator2) at the configured ADB server.

    ``adbutils.adb`` is a module-level client built at import time from
    ``ANDROID_ADB_SERVER_HOST`` / ``ANDROID_ADB_SERVER_PORT``. Rebind it so a
    remote ``ADB_HOST``/``ADB_PORT`` is honoured by every ``u2.connect`` call
    instead of falling back to loopback. Missing adbutils is not an error: the
    subprocess-based paths keep working.
    """
    endpoint = endpoint or current_adb_endpoint()
    try:
        import adbutils
    except ImportError:  # pragma: no cover - adbutils is a hard dependency
        return endpoint

    try:
        previous = getattr(adbutils, "adb", None)
        # Mutate the existing client too: anything that captured a reference
        # before this call keeps talking to the same endpoint.
        if previous is not None:
            for attr, value in (
                ("_BaseClient__host", endpoint.host),
                ("_BaseClient__port", endpoint.port),
            ):
                try:
                    setattr(previous, attr, value)
                except Exception:  # pragma: no cover - defensive
                    pass
        adbutils.adb = adbutils.AdbClient(host=endpoint.host, port=endpoint.port)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not rebind adbutils to {endpoint.socket}: {exc}")
    return endpoint


def apply_current_endpoint(
    environment: MutableMapping[str, str] | None = None,
) -> AdbEndpoint:
    """Export the configured endpoint and rebind adbutils for this process."""
    endpoint = current_adb_endpoint()
    endpoint.apply_to_environment(environment)
    configure_adbutils(endpoint)
    return endpoint


def adb_command(arguments: Sequence[str]) -> list[str]:
    """Resolve the ADB binary and add the configured server's host and port."""
    return AdbSession(current_adb_endpoint()).command(arguments)
