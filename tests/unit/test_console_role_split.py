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

"""``CONSOLE_ROLE`` — which endpoints exist on which machine.

This is a security boundary, not a convenience flag.  ``POST /api/run`` spawns
``artemis run`` and ``POST /api/stop`` kills processes; either one reachable from
the internet is a command-execution hole.  The shared VPS instance therefore must
not merely *refuse* those routes -- it must not register them at all, so a later
middleware mistake cannot expose them.

``/api/kb/*`` is in the same category for a different reason: it edits and deletes
workflows and carries no access key of its own.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

console_server = importlib.import_module("apps.test_console.server")

#: Endpoint điều khiển thiết bị — tuyệt đối không được có trên máy chủ dùng chung.
DEVICE_PATHS = [
    "/api/devices",
    "/api/packages",
    "/api/screenshot",
    "/api/status",
    "/api/tasks",
    "/api/run",
    "/api/stop",
    "/api/budget",
    "/api/budget/reset",
    "/api/llm-test",
]


@pytest.fixture
def load_role(monkeypatch):
    """Nạp lại module console dưới một vai trò, rồi trả về vai trò mặc định."""

    def _load(role: str):
        monkeypatch.setenv("CONSOLE_ROLE", role)
        return importlib.reload(console_server)

    yield _load
    monkeypatch.delenv("CONSOLE_ROLE", raising=False)
    importlib.reload(console_server)


def paths(module) -> set[str]:
    return set(module.app.openapi()["paths"])


# ── server: máy chủ dùng chung ───────────────────────────────────────────
@pytest.mark.parametrize("path", DEVICE_PATHS)
def test_the_server_role_has_no_device_endpoint(load_role, path):
    module = load_role("server")
    assert path not in paths(module)


def test_the_server_role_serves_the_ai_proxy_and_the_rag_api(load_role):
    module = load_role("server")
    registered = paths(module)
    assert "/api/knowledge/context" in registered
    assert any(p.startswith("/llm/v1") for p in registered)


def test_the_server_role_does_not_expose_kb_administration(load_role):
    """/api/kb/* deletes workflows and has no access key of its own."""
    module = load_role("server")
    assert [p for p in paths(module) if p.startswith("/api/kb")] == []


def test_the_server_role_has_no_console_ui(load_role):
    """The console UI is a device-control UI; on a VPS every panel would break."""
    module = load_role("server")
    body = TestClient(module.app).get("/").text
    assert "máy chủ dùng chung" in body
    assert "Các bước" not in body  # the console UI's first tab


# ── edge: máy cắm điện thoại ─────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/api/devices", "/api/run", "/api/stop", "/api/status"])
def test_the_edge_role_has_the_device_endpoints(load_role, path):
    assert path in paths(load_role("edge"))


def test_the_edge_role_does_not_serve_the_proxy_or_the_rag_api(load_role):
    """A tester machine holds no LLM key and no database -- those go to the server."""
    registered = paths(load_role("edge"))
    assert "/api/knowledge/context" not in registered
    assert not any(p.startswith("/llm/v1") for p in registered)


def test_the_edge_role_keeps_the_console_ui(load_role):
    module = load_role("edge")
    assert TestClient(module.app).get("/").status_code == 200


# ── all: máy dev chạy một mình ───────────────────────────────────────────
def test_the_all_role_keeps_everything(load_role):
    """Mặc định phải giữ nguyên hành vi cũ cho máy dev."""
    registered = paths(load_role("all"))
    assert "/api/run" in registered
    assert "/api/knowledge/context" in registered
    assert any(p.startswith("/llm/v1") for p in registered)
    assert any(p.startswith("/api/kb") for p in registered)


def test_the_default_role_is_all(monkeypatch):
    monkeypatch.delenv("CONSOLE_ROLE", raising=False)
    module = importlib.reload(console_server)
    assert module.CONSOLE_ROLE == "all"
    importlib.reload(console_server)


def test_a_nonsense_role_falls_back_to_all(load_role):
    assert load_role("quantri").CONSOLE_ROLE == "all"


# ── /api/health không rò rỉ thông tin trên máy chủ ───────────────────────
def test_health_reports_the_role(load_role):
    module = load_role("server")
    assert TestClient(module.app).get("/api/health").json()["role"] == "server"


def test_health_on_the_server_does_not_describe_the_filesystem(load_role):
    """A shared host should not advertise its adb path or ARTEMIS checkout."""
    body = TestClient(load_role("server").app).get("/api/health").json()
    assert "adb_path" not in body
    assert "artemis_dir" not in body
    assert "devices" not in body


def test_health_on_a_device_host_still_lists_the_devices(load_role):
    body = TestClient(load_role("all").app).get("/api/health").json()
    assert "devices" in body and "adb_path" in body
