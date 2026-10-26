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

"""Package pre-flight of the Test Console.

A run locked to a package that is not installed made ARTEMIS try to launch it
3 × 15s and then give up, so the tester saw a task that looked hung while the
real app never opened. The console now refuses the run and names the closest
installed package instead.
"""

import importlib
from types import SimpleNamespace

console_server = importlib.import_module("apps.test_console.server")

HUNONIC = [
    "com.hunonic.ctv.two",
    "com.hunonic.hmqtt",
    "com.hunonic.work",
    "com.hunonic_daily",
    "com.hunoniccustomercare",
    "com.iot.hunonic",
    "com.qlk.hunonic",
]


def test_typo_gets_the_package_with_the_same_prefix():
    assert console_server.suggest_packages("com.iot.hunonicc", HUNONIC) == ["com.iot.hunonic"]


def test_unrelated_default_gets_no_noise_suggestions():
    """`com.example.app` must not produce five random 'app' packages."""
    assert console_server.suggest_packages("com.example.app", HUNONIC) == []


def test_exact_name_ranks_first():
    assert console_server.suggest_packages("com.hunonic.work", HUNONIC)[0] == "com.hunonic.work"


def test_empty_inputs_are_safe():
    assert console_server.suggest_packages("", HUNONIC) == []
    assert console_server.suggest_packages("com.x.y", []) == []


def test_installed_packages_parses_pm_output(monkeypatch):
    class Result:
        stdout = "package:com.a.one\npackage:com.b.two\n\n"

    monkeypatch.setattr(console_server, "run_adb", lambda args, timeout=15: Result())
    monkeypatch.setattr(console_server, "_package_cache", {})
    assert console_server.installed_packages("SERIAL") == ["com.a.one", "com.b.two"]


def test_installed_packages_is_empty_when_adb_fails(monkeypatch):
    """No adb must not block runs: the check is skipped, not failed."""

    def boom(args, timeout=15):
        raise OSError("adb not found")

    monkeypatch.setattr(console_server, "run_adb", boom)
    monkeypatch.setattr(console_server, "_package_cache", {})
    assert console_server.installed_packages("SERIAL") == []


def test_installed_packages_uses_the_cache(monkeypatch):
    calls = []

    class Result:
        stdout = "package:com.a.one\n"

    def counting(args, timeout=15):
        calls.append(args)
        return Result()

    monkeypatch.setattr(console_server, "run_adb", counting)
    monkeypatch.setattr(console_server, "_package_cache", {})
    console_server.installed_packages("SERIAL")
    console_server.installed_packages("SERIAL")
    assert len(calls) == 1


# --- Nhắc agent bám đúng app -------------------------------------------------
# `--locked-app` chỉ đảm bảo app ở foreground lúc khởi động: nó không ràng buộc
# hành động sau đó và agent cũng không được thông báo. Đo thực tế cho thấy agent
# tự force-stop app khác, bấm HOME, mở app switcher rồi chạm lung tung.


def test_app_lock_hint_names_the_package_and_the_forbidden_actions():
    hint = console_server.app_lock_hint("com.iot.hunonic")
    assert hint.startswith(console_server.APP_LOCK_HEADER)
    assert "com.iot.hunonic" in hint
    assert "app switcher" in hint
    assert "HOME" in hint
    assert "FAILED" in hint


def test_app_lock_hint_is_empty_without_a_package():
    assert console_server.app_lock_hint("") == ""
    assert console_server.app_lock_hint("   ") == ""


def test_app_lock_hint_can_be_disabled(monkeypatch):
    monkeypatch.setattr(console_server, "APP_LOCK_HINT_ENABLED", False)
    assert console_server.app_lock_hint("com.iot.hunonic") == ""


# --- Cảnh báo thiếu đường Metro cho app React Native bản debug ----------------
# Bản debug không nhúng JS bundle: thiếu `adb reverse` là app kẹt ở splash mãi,
# agent không biết nên cứ chờ rồi chạm lung tung.


def _adb_returning(text):
    return lambda args, timeout=15: SimpleNamespace(stdout=text)


def test_metro_warning_when_reverse_is_missing(monkeypatch):
    monkeypatch.setattr(console_server, "run_adb", _adb_returning(""))
    warning = console_server.metro_warning("SERIAL", "8081")
    assert "adb reverse tcp:8081" in warning
    assert "splash" in warning


def test_metro_warning_is_empty_when_reverse_exists(monkeypatch):
    monkeypatch.setattr(
        console_server, "run_adb", _adb_returning("UsbFfs tcp:8081 tcp:8081")
    )
    assert console_server.metro_warning("SERIAL", "8081") == ""


def test_metro_warning_never_false_alarms_without_adb(monkeypatch):
    def boom(args, timeout=15):
        raise OSError("adb not found")

    monkeypatch.setattr(console_server, "run_adb", boom)
    assert console_server.metro_warning("SERIAL", "8081") == ""


def test_metro_warning_is_off_when_the_port_is_not_numeric(monkeypatch):
    monkeypatch.setattr(console_server, "run_adb", _adb_returning(""))
    assert console_server.metro_warning("SERIAL", "0") == ""
