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

"""KB tự hồi khi tunnel được mở lại — không cần khởi động lại console.

Tunnel KB nằm ngoài tiến trình console (`run.sh` mở nó), và service được dựng **một lần** cho
cả tiến trình.  Nếu lần dựng đầu rơi đúng lúc tunnel chưa lên, console ghi nhớ "KB tắt" và
không bao giờ thử lại — tester phải khởi động lại console dù tunnel đã sẵn sàng.  Đo thật:
`sh apps/test_console/run.sh` khi tunnel đứt ⇒ tab Knowledge báo "KB tắt"; mở tunnel xong vẫn
báo "KB tắt" cho tới khi restart.
"""

from __future__ import annotations

import importlib
import time

service_module = importlib.import_module("artemis.knowledge.service")

#: ``tests/unit/conftest.py`` thay ``get_service`` bằng một lambda (để không test nào mở KB
#: thật).  Giữ tham chiếu **hàm gốc** ngay lúc import — trước khi fixture chạy — để test được
#: đúng logic tự hồi.
_REAL_GET_SERVICE = service_module.get_service
_REAL_RESET_SERVICE = service_module.reset_service


class _FakeService:
    """Service giả: chỉ cần ``available`` để mô phỏng KB tắt/bật."""

    def __init__(self, available: bool) -> None:
        self._store = object() if available else None
        self._degraded_reason = "" if available else "Không kết nối được PostgreSQL"

    @property
    def available(self) -> bool:
        return self._store is not None

    @property
    def degraded_reason(self) -> str:
        return self._degraded_reason


def _install(monkeypatch, results: list[bool]):
    """``from_settings`` trả lần lượt theo ``results``; ghi lại số lần được gọi."""
    calls: list[int] = []

    def fake_from_settings():
        calls.append(1)
        index = min(len(calls) - 1, len(results) - 1)
        return _FakeService(results[index])

    monkeypatch.setattr(service_module.KnowledgeService, "from_settings", staticmethod(fake_from_settings))
    return calls


def test_a_degraded_service_is_rebuilt_once_the_cooldown_passed(monkeypatch):
    """KB tắt lúc dựng ⇒ tới request sau (đã hết cooldown) phải thử dựng lại và thấy KB sống."""
    monkeypatch.setattr(service_module, "_SERVICE", None)
    monkeypatch.setattr(service_module, "_SERVICE_RETRY_AFTER", 0.0)
    calls = _install(monkeypatch, [False, True])

    first = _REAL_GET_SERVICE()
    assert first.available is False

    # Chưa hết cooldown: dùng lại bản cũ, không thử lại ở mỗi request.
    again = _REAL_GET_SERVICE()
    assert again.available is False
    assert len(calls) == 1

    # Hết cooldown: dựng lại và thấy KB đã sống.
    monkeypatch.setattr(service_module, "_SERVICE_RETRY_AFTER", time.time() - 1)
    healed = _REAL_GET_SERVICE()
    assert healed.available is True
    assert len(calls) == 2


def test_a_healthy_service_is_never_rebuilt(monkeypatch):
    monkeypatch.setattr(service_module, "_SERVICE", None)
    monkeypatch.setattr(service_module, "_SERVICE_RETRY_AFTER", 0.0)
    calls = _install(monkeypatch, [True])

    assert _REAL_GET_SERVICE().available is True
    monkeypatch.setattr(service_module, "_SERVICE_RETRY_AFTER", time.time() - 1)
    assert _REAL_GET_SERVICE().available is True

    assert len(calls) == 1


def test_reset_service_clears_the_cooldown(monkeypatch):
    monkeypatch.setattr(service_module, "_SERVICE", _FakeService(False))
    monkeypatch.setattr(service_module, "_SERVICE_RETRY_AFTER", time.time() + 600)

    _REAL_RESET_SERVICE()

    assert service_module._SERVICE is None
    assert service_module._SERVICE_RETRY_AFTER == 0.0
