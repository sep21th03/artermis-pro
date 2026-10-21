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

"""`artemis llm check` maps provider responses to the fix the operator needs."""

import importlib

llm_module = importlib.import_module("artemis.interfaces.cli.commands.llm")


def _result(**kwargs):
    base = dict(
        provider="openai",
        model="ts/gemini-3.5-flash-lite",
        base_url="https://api.vilao.ai/v1",
        key_present=True,
        key_hint="…ce41",
    )
    base.update(kwargs)
    return llm_module.LlmCheckResult(**base)


def test_successful_probe_reports_reply(monkeypatch):
    monkeypatch.setattr(llm_module, "_api_key", lambda: "sk-test-key-123456")
    monkeypatch.setattr(llm_module, "_default_model_from_config", lambda: ("openai", "m1"))
    monkeypatch.setattr(llm_module, "_get_models", lambda *a, **k: (200, '{"data":[{"id":"m1"}]}'))
    monkeypatch.setattr(
        llm_module,
        "_call_chat",
        lambda *a, **k: (200, 0.9, '{"choices":[{"message":{"content":"pong"}}]}'),
    )

    result = llm_module.run_llm_check(timeout=5)

    assert result.ok is True
    assert result.reply == "pong"
    assert result.models_count == 1
    assert result.hint() is None


def test_models_404_points_at_the_base_url():
    result = _result(models_status=404, chat_status=404, models_error='{"detail":"Not Found"}')

    hint = result.hint() or ""
    assert "/v1" in hint
    assert "BASE URL" in hint


def test_invalid_key_is_reported_as_an_auth_problem():
    result = _result(
        models_status=401,
        chat_status=401,
        chat_error='{"error":{"message":"Incorrect API key provided"}}',
    )

    assert "API key" in (result.hint() or "")


def test_html_body_means_a_wrong_base_url():
    result = _result(
        models_status=200, chat_status=200,
        chat_error="<!DOCTYPE html><html><body>login</body></html>",
    )

    assert "HTML" in (result.hint() or "")


def test_budget_json_is_explained():
    result = _result(
        models_status=200,
        chat_status=429,
        chat_error='{"error":{"code":"budget_exceeded","message":"BUDGET: boom"}}',
    )

    assert "ngân sách" in (result.hint() or "")


def test_missing_key_short_circuits(monkeypatch):
    monkeypatch.setattr(llm_module, "_api_key", lambda: "")

    result = llm_module.run_llm_check(timeout=1)

    assert result.ok is False
    assert "OPENAI_API_KEY" in (result.chat_error or "")
