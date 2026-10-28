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

"""Upstream của proxy LLM: đi Google trực tiếp thay vì gateway bên thứ ba.

Đo trên máy thật 28/10/2026: gateway (``api.vilao.ai``) đứt hẳn — ``httpx.ConnectError`` ở
``llm_proxy`` → trả 500 → SDK OpenAI retry im lặng → console thấy "treo 120s không có log mới"
và giết task.  Người dùng chỉ thấy "lập kế hoạch mãi không xong".

Bốn tính chất được khoá ở đây:

1. Nhận ra upstream Google theo **host**, không theo tên biến.
2. Khoá xác thực chọn theo upstream: Google ⇒ ``GEMINI_API_KEY``, còn lại ⇒ ``OPENAI_API_KEY``.
   Gửi sai khoá thì Google trả 401 và người đọc log tưởng lỗi cấu hình của mình.
3. Tên model của gateway (``ts/…``) bị bỏ tiền tố trước khi tới Google — endpoint Google
   không biết tiền tố đó, gửi nguyên sẽ 404.
4. Thông báo lỗi nêu **host** upstream, không lộ khoá.
"""

from __future__ import annotations

import importlib

console_server = importlib.import_module("apps.test_console.server")


def _point_at(monkeypatch, base: str) -> None:
    """Ép proxy dùng một upstream nhất định, không cần sửa .env."""
    monkeypatch.setenv("ARTEMIS_UPSTREAM_BASE_URL", base)


GOOGLE_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
GATEWAY_BASE = "https://api.vilao.ai/v1"


def test_the_google_endpoint_is_recognised_by_host(monkeypatch):
    _point_at(monkeypatch, GOOGLE_BASE)
    assert console_server.upstream_is_google() is True
    assert console_server.upstream_host() == "generativelanguage.googleapis.com"

    _point_at(monkeypatch, GATEWAY_BASE)
    assert console_server.upstream_is_google() is False
    assert console_server.upstream_host() == "api.vilao.ai"


def test_the_auth_key_follows_the_upstream(monkeypatch, tmp_path):
    """Google phải nhận khoá Gemini; gateway nhận khoá OpenAI — chọn theo upstream."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=openai-key\nGEMINI_API_KEY=gemini-key\n", encoding="utf-8"
    )
    monkeypatch.setattr(console_server, "ARTEMIS_ENV", env_file)

    _point_at(monkeypatch, GOOGLE_BASE)
    assert console_server.upstream_key() == "gemini-key"

    _point_at(monkeypatch, GATEWAY_BASE)
    assert console_server.upstream_key() == "openai-key"


def test_a_gateway_model_name_loses_its_routing_prefix(monkeypatch):
    """``ts/gemini-3.5-flash-lite`` ⇒ ``gemini-3.5-flash-lite`` khi đi Google.

    Đây là điều kiện để phía agent **không phải đổi gì**: harness vẫn gửi tên có ``ts/``.
    """
    _point_at(monkeypatch, GOOGLE_BASE)
    assert console_server.normalize_upstream_model("ts/gemini-3.5-flash-lite") == (
        "gemini-3.5-flash-lite"
    )
    # Tên đã sạch thì giữ nguyên.
    assert console_server.normalize_upstream_model("gemini-2.5-flash") == "gemini-2.5-flash"

    # Gateway thì CẦN tiền tố (nó dùng để định tuyến) ⇒ không được bỏ.
    _point_at(monkeypatch, GATEWAY_BASE)
    assert console_server.normalize_upstream_model("ts/gemini-3.5-flash-lite") == (
        "ts/gemini-3.5-flash-lite"
    )
