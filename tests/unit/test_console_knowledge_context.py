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

"""``POST /api/knowledge/context`` -- the one call a tester machine makes.

The flow: the tester types the **original** prompt, this endpoint embeds it,
finds the nearest workflows in PostgreSQL, and answers with the finished
prompt.  The tester machine then hands that prompt to the agent -- it needs
ARTEMIS and a phone, and nothing else: no PostgreSQL, no embedding key, no LLM
key.

Two things must hold: the endpoint needs the access key (it hands out internal
knowledge over LAN/VPS), and a KB that is missing or empty must return the
original prompt rather than failing.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

console_server = importlib.import_module("apps.test_console.server")
service_module = importlib.import_module("artemis.knowledge.service")

KEY = "khoa-thu-nghiem"


@dataclass
class FakeHit:
    workflow: object
    trusted: bool = True


@dataclass
class FakeWorkflow:
    id: str


@dataclass
class FakeResult:
    found: bool
    hits: list = field(default_factory=list)


class FakeService:
    """Just enough of ``KnowledgeService`` for the endpoint's contract."""

    def __init__(self, found: bool = True, block: str = "[KIẾN THỨC] ...") -> None:
        self.available = True
        self.degraded_reason = ""
        self._found = found
        self._block = block
        self.calls: list[tuple] = []
        self._store = self

    def find_app_by_package(self, package: str) -> str:
        return "hunonic"

    def context_for(self, query, app_id, run_id=None):
        self.calls.append((query, app_id, run_id))
        hits = [FakeHit(FakeWorkflow("auth.logout"))] if self._found else []
        return FakeResult(found=self._found, hits=hits), (self._block if self._found else "")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CONSOLE_ACCESS_KEY", KEY)
    monkeypatch.setattr(console_server, "ACCESS_TOKENS_CACHE", None)
    return TestClient(console_server.app)


@pytest.fixture
def server_client(monkeypatch):
    """Console ở vai trò ``server`` — vai trò duy nhất đòi khoá truy cập."""
    monkeypatch.setenv("CONSOLE_ACCESS_KEY", KEY)
    monkeypatch.setenv("CONSOLE_ROLE", "server")
    monkeypatch.setattr(console_server, "ACCESS_TOKENS_CACHE", None)
    module = importlib.reload(console_server)
    yield TestClient(module.app)
    monkeypatch.delenv("CONSOLE_ROLE", raising=False)
    importlib.reload(console_server)


@pytest.fixture
def auth():
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def fake_service(monkeypatch):
    def _install(**kwargs) -> FakeService:
        service = FakeService(**kwargs)
        monkeypatch.setattr(service_module, "get_service", lambda: service)
        return service

    return _install


# ── the access key (chỉ ở vai trò server) ────────────────────────────────
def test_the_server_role_refuses_a_request_without_a_key(server_client):
    response = server_client.post("/api/knowledge/context", json={"goal": "Test đăng xuất"})
    assert response.status_code == 401


def test_the_server_role_refuses_a_wrong_key(server_client):
    response = server_client.post(
        "/api/knowledge/context",
        json={"goal": "Test đăng xuất"},
        headers={"Authorization": "Bearer sai"},
    )
    assert response.status_code == 401


def test_the_server_role_accepts_the_key(server_client, fake_service):
    fake_service()
    response = server_client.post(
        "/api/knowledge/context",
        json={"goal": "Test đăng xuất"},
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert response.status_code == 200


def test_a_local_console_needs_no_key(client, fake_service):
    """Vai trò all/edge là công cụ tại chỗ; phần còn lại của /api/* cũng không khoá.

    Nếu bắt khoá thì UI trình duyệt phải nhúng khoá vào trang, tệ hơn là không bắt.
    """
    fake_service()
    response = client.post("/api/knowledge/context", json={"goal": "Test đăng xuất"})
    assert response.status_code == 200


def test_the_key_can_come_from_the_x_api_key_header(client, fake_service):
    fake_service()
    response = client.post(
        "/api/knowledge/context",
        json={"goal": "Test đăng xuất"},
        headers={"x-api-key": KEY},
    )
    assert response.status_code == 200


# ── the enhanced prompt ──────────────────────────────────────────────────
def test_a_found_workflow_is_appended_to_the_original_prompt(client, auth, fake_service):
    """The tester's words come first, the guidance after -- never replaced."""
    fake_service(found=True, block="[KIẾN THỨC VỀ CHỨC NĂNG]\n• auth.logout — Đăng xuất")

    body = client.post(
        "/api/knowledge/context",
        json={"goal": "Test đăng xuất", "app_package": "com.iot.hunonic"},
        headers=auth,
    ).json()

    assert body["prompt"].startswith("Test đăng xuất")
    assert "auth.logout" in body["prompt"]
    assert body["found"] is True
    assert body["kb"]["trusted"] == ["auth.logout"]


def test_the_package_and_run_id_reach_the_service(client, auth, fake_service):
    service = fake_service()

    client.post(
        "/api/knowledge/context",
        json={"goal": "Test đăng xuất", "app_package": "com.iot.hunonic", "run_id": "r7"},
        headers=auth,
    )

    assert service.calls == [("Test đăng xuất", "hunonic", "r7")]


def test_no_match_returns_the_original_prompt_untouched(client, auth, fake_service):
    fake_service(found=False)

    body = client.post(
        "/api/knowledge/context",
        json={"goal": "Test đăng xuất", "app_package": "com.iot.hunonic"},
        headers=auth,
    ).json()

    assert body["prompt"] == "Test đăng xuất"
    assert body["found"] is False
    assert body["kb_prompt"] == ""


def test_an_unregistered_app_returns_the_original_prompt(client, auth, monkeypatch):
    """An app missing from the KB is a normal answer, not an error."""
    service = FakeService()
    monkeypatch.setattr(service, "find_app_by_package", lambda package: None)
    monkeypatch.setattr(service_module, "get_service", lambda: service)

    body = client.post(
        "/api/knowledge/context",
        json={"goal": "Test đăng xuất", "app_package": "com.la.hoac"},
        headers=auth,
    ).json()

    assert body["prompt"] == "Test đăng xuất"
    assert body["kb"]["app_id"] is None


def test_no_package_means_no_lookup_at_all(client, auth, fake_service):
    service = fake_service()

    body = client.post(
        "/api/knowledge/context", json={"goal": "Test đăng xuất"}, headers=auth
    ).json()

    assert service.calls == []
    assert body["prompt"] == "Test đăng xuất"


# ── degradation ──────────────────────────────────────────────────────────
def test_an_unavailable_kb_still_answers(client, auth, monkeypatch):
    """The tester must always get a prompt back, KB or no KB."""
    monkeypatch.setattr(service_module, "get_service", lambda: service_module.KnowledgeService())

    body = client.post(
        "/api/knowledge/context",
        json={"goal": "Test đăng xuất", "app_package": "com.iot.hunonic"},
        headers=auth,
    ).json()

    assert body["prompt"] == "Test đăng xuất"
    assert body["found"] is False
    assert body["kb"]["degraded"] is True


def test_a_service_that_raises_still_answers(client, auth, monkeypatch):
    def explode():
        raise RuntimeError("database mất kết nối")

    monkeypatch.setattr(service_module, "get_service", explode)

    response = client.post(
        "/api/knowledge/context",
        json={"goal": "Test đăng xuất", "app_package": "com.iot.hunonic"},
        headers=auth,
    )

    assert response.status_code == 200
    assert response.json()["prompt"] == "Test đăng xuất"
    assert "database mất kết nối" in response.json()["kb"]["reason"]


def test_an_empty_goal_is_not_a_crash(client, auth, fake_service):
    fake_service()
    assert client.post("/api/knowledge/context", json={}, headers=auth).status_code == 200


def test_an_overlong_goal_is_rejected_by_validation(client, auth):
    response = client.post("/api/knowledge/context", json={"goal": "x" * 5000}, headers=auth)
    assert response.status_code == 422
