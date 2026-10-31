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

"""Đăng nhập quản trị KB: đọc vẫn mở bằng khoá truy cập, **ghi** phải có token admin.

Yêu cầu nghiệp vụ: một tài khoản admin (mặc định ``admin@admin`` / ``admin``) có quyền thêm/sửa/xoá
workflow; mọi người khác chỉ đọc được. Bộ test này khoá đúng ranh giới đó — nếu ai bỏ dependency khỏi
một route ghi, test đổ ngay.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from artemis.knowledge import api, auth


# ── phần thuần: băm mật khẩu + token ─────────────────────────────────────────────────────


def test_password_hash_roundtrip():
    stored = auth.hash_password("admin")

    assert stored.startswith("scrypt$")
    assert auth.verify_password("admin", stored) is True
    assert auth.verify_password("Admin", stored) is False
    assert auth.verify_password("", stored) is False


def test_password_hash_is_salted_and_rejects_garbage():
    first, second = auth.hash_password("x"), auth.hash_password("x")

    assert first != second, "mỗi lần băm phải có salt mới"
    assert auth.verify_password("x", first) and auth.verify_password("x", second)
    assert auth.verify_password("x", "không-phải-định-dạng") is False
    assert auth.verify_password("x", "") is False


def test_default_admin_credentials_and_case_insensitive_email(monkeypatch):
    monkeypatch.delenv(auth.ADMIN_EMAIL_ENV, raising=False)
    monkeypatch.delenv(auth.ADMIN_PASSWORD_ENV, raising=False)

    assert auth.admin_email() == "admin@admin" and auth.admin_password() == "admin"
    assert auth.verify_credentials("admin@admin", "admin") is True
    assert auth.verify_credentials("Admin@Admin", "admin") is True
    assert auth.verify_credentials("admin@admin", "sai") is False
    assert auth.verify_credentials("khac@admin", "admin") is False


def test_admin_account_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv(auth.ADMIN_EMAIL_ENV, "boss@example.com")
    monkeypatch.setenv(auth.ADMIN_PASSWORD_ENV, "s3cret")

    assert auth.verify_credentials("boss@example.com", "s3cret") is True
    assert auth.verify_credentials("admin@admin", "admin") is False


def test_token_roundtrip_expiry_and_tampering(monkeypatch):
    monkeypatch.setenv(auth.SECRET_ENV, "secret-test")

    info = auth.issue_token()
    assert info.email == "admin@admin" and info.expires_at > time.time()
    assert auth.verify_token(info.token) == "admin@admin"
    assert auth.verify_token(auth.issue_token(ttl_seconds=-1).token) is None
    body, _, signature = info.token.partition(".")
    assert auth.verify_token(f"{body}x.{signature}") is None
    assert auth.verify_token(f"{body}.{signature[:-2]}xx") is None
    assert auth.verify_token("rác") is None and auth.verify_token(None) is None


def test_token_secret_changes_invalidate_old_tokens(monkeypatch):
    monkeypatch.setenv(auth.SECRET_ENV, "secret-A")
    token = auth.issue_token().token

    monkeypatch.setenv(auth.SECRET_ENV, "secret-B")
    assert auth.verify_token(token) is None


def test_bearer_token_parsing():
    assert auth.bearer_token("Bearer abc.def") == "abc.def"
    assert auth.bearer_token("bearer abc") == "abc"
    assert auth.bearer_token("Token abc") == ""
    assert auth.bearer_token(None) == ""


# ── phần HTTP: ranh giới đọc/ghi trên router thật ────────────────────────────────────────


class _SavedWorkflow:
    """Đủ để router gọi ``model_dump()`` — không cần validate cả model thật."""

    def model_dump(self):
        return {"id": "auth.logout", "app_id": "hunonic", "name": "Đăng xuất"}


class _InnerStore:
    def upsert_app(self, *args):
        return {"id": args[0]}

    def upsert_version(self, *args):
        return {"app_id": args[0]}

    def save_workflow(self, workflow_id, *_a, **_k):
        return _SavedWorkflow()

    def delete_workflow(self, workflow_id):
        return True

    def list_workflows(self, *_a, **_k):
        return []


class _Service:
    available = True
    degraded_reason = ""
    _store = _InnerStore()

    def reindex(self, app_id=None):
        return {"reindexed": 0, "app_id": app_id}

    def index_workflow(self, workflow):
        return True


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv(auth.DISABLED_ENV, raising=False)
    monkeypatch.setenv(auth.ADMIN_EMAIL_ENV, "admin@admin")
    monkeypatch.setenv(auth.ADMIN_PASSWORD_ENV, "admin")
    monkeypatch.setenv(auth.SECRET_ENV, "secret-http-test")
    monkeypatch.setattr(api, "_store", lambda: _Service())
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


WRITE_CALLS = [
    ("post", "/api/kb/apps", {"json": {"id": "hunonic", "name": "Hunonic"}}),
    ("post", "/api/kb/versions", {"json": {"app_id": "hunonic", "version_label": "v1"}}),
    (
        "put",
        "/api/kb/workflows/auth.logout",
        {"json": {"app_id": "hunonic", "name": "Đăng xuất", "steps": []}},
    ),
    ("delete", "/api/kb/workflows/auth.logout", {}),
    ("post", "/api/kb/reindex", {}),
]


def _login(client) -> dict:
    token = client.post(
        "/api/kb/auth/login", json={"email": "admin@admin", "password": "admin"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_login_returns_a_working_token(client):
    bad = client.post("/api/kb/auth/login", json={"email": "admin@admin", "password": "sai"})
    assert bad.status_code == 401

    ok = client.post("/api/kb/auth/login", json={"email": "admin@admin", "password": "admin"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["email"] == "admin@admin" and body["token"] and body["ttl_seconds"] > 0

    me = client.get("/api/kb/auth/me", headers={"Authorization": f"Bearer {body['token']}"}).json()
    assert me["authenticated"] is True and me["can_write"] is True
    assert me["email"] == "admin@admin"


def test_me_reports_read_only_without_a_token(client):
    body = client.get("/api/kb/auth/me").json()

    assert body["authenticated"] is False and body["can_write"] is False


@pytest.mark.parametrize("method,path,kwargs", WRITE_CALLS)
def test_every_write_route_requires_admin(client, method, path, kwargs):
    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401, f"{method.upper()} {path} phải chặn khi chưa đăng nhập"
    assert response.json()["detail"]["auth_required"] is True


@pytest.mark.parametrize("method,path,kwargs", WRITE_CALLS)
def test_admin_token_allows_every_write_route(client, method, path, kwargs):
    response = getattr(client, method)(path, headers=_login(client), **kwargs)

    assert response.status_code < 400, response.text


def test_reads_still_work_without_a_token(client):
    """Agent ở máy tester gọi /retrieve và UI đọc danh sách — không cần tài khoản."""
    assert client.get("/api/kb/workflows?app_id=hunonic").status_code == 200
    assert client.get("/api/kb/auth/me").status_code == 200


def test_a_forged_token_is_rejected(client):
    response = client.put(
        "/api/kb/workflows/auth.logout",
        headers={"Authorization": "Bearer abc.def"},
        json={"app_id": "hunonic", "name": "x", "steps": []},
    )

    assert response.status_code == 401


def test_auth_can_be_disabled_for_a_lone_dev_machine(client, monkeypatch):
    monkeypatch.setenv(auth.DISABLED_ENV, "1")

    response = client.put(
        "/api/kb/workflows/auth.logout",
        json={"app_id": "hunonic", "name": "x", "steps": []},
    )

    assert response.status_code < 400
    assert client.get("/api/kb/auth/me").json()["email"] == "auth-disabled"
