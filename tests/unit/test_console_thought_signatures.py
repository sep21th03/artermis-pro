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

"""Thought signature của Gemini 3 phải sống sót qua proxy của Test Console.

Gemini 3 bắt buộc client gửi lại ``thought_signature`` của mỗi functionCall part khi
replay lượt có tool call. Gateway trả chữ ký ở
``tool_calls[].extra_content.google.thought_signature``, nhưng ``langchain_openai`` (và
SDK ``openai``) không biết trường ``extra_content`` nên nó rơi mất khi parse response:
request kế tiếp của Planner thiếu chữ ký → upstream trả 400 INVALID_ARGUMENT → ARTEMIS
coi là lỗi vĩnh viễn (``bad_request``) và task dừng ngay, không có mô tả chi tiết.

Proxy nhớ chữ ký theo ``tool_call.id`` rồi gắn lại vào request, nên agent không phải đổi.
"""

import copy
import importlib

console_server = importlib.import_module("apps.test_console.server")


def _fresh_signature_store(monkeypatch) -> dict:
    store: dict = {}
    monkeypatch.setattr(console_server, "_THOUGHT_SIGNATURES", store)
    return store


def _tool_call(call_id: str = "call_1234567", signature: str | None = None) -> dict:
    call = {
        "id": call_id,
        "type": "function",
        "function": {"name": "save_note", "arguments": '{"key":"plan"}'},
    }
    if signature is not None:
        call["extra_content"] = {"google": {"thought_signature": signature}}
    return call


def _chat_body(tool_calls: list[dict]) -> dict:
    return {
        "model": "ts/gemini-3.5-flash-lite",
        "messages": [
            {"role": "system", "content": "You are the Planner."},
            {"role": "user", "content": "Save the plan."},
            {"role": "assistant", "content": None, "tool_calls": tool_calls},
            {"role": "tool", "tool_call_id": "call_1234567", "content": "saved"},
        ],
    }


def test_remembers_signature_from_full_response(monkeypatch):
    store = _fresh_signature_store(monkeypatch)
    response = {"choices": [{"message": {"role": "assistant", "tool_calls": [_tool_call(signature="sig-a")]}}]}

    remembered = console_server.remember_thought_signatures(response)

    assert remembered == 1
    assert store == {"call_1234567": "sig-a"}


def test_remembers_signature_from_stream_delta(monkeypatch):
    store = _fresh_signature_store(monkeypatch)
    event = {
        "choices": [
            {"delta": {"tool_calls": [{"index": 0, "id": "call_7777777", "function": {"name": "save_note"}}]}}
        ]
    }
    signed_delta = {
        "choices": [
            {"delta": {"tool_calls": [_tool_call("call_7777777", "sig-stream")]}}
        ]
    }

    console_server.remember_thought_signatures(event)
    console_server.remember_thought_signatures(signed_delta)

    assert store == {"call_7777777": "sig-stream"}


def test_attaches_signature_to_replayed_tool_call(monkeypatch):
    _fresh_signature_store(monkeypatch)
    console_server.remember_thought_signatures(
        {"choices": [{"message": {"tool_calls": [_tool_call(signature="sig-a")]}}]}
    )
    body = _chat_body([_tool_call()])

    attached = console_server.attach_thought_signatures(body)

    assert attached == 1
    assert body["messages"][2]["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": "sig-a"}
    }


def test_unknown_and_already_signed_calls_are_left_untouched(monkeypatch):
    _fresh_signature_store(monkeypatch)
    body = _chat_body([_tool_call("call_9999999"), _tool_call("call_1234567", "sig-existing")])
    original = copy.deepcopy(body)

    attached = console_server.attach_thought_signatures(body)

    assert attached == 0
    assert body == original


def test_memory_is_capped(monkeypatch):
    store = _fresh_signature_store(monkeypatch)
    monkeypatch.setattr(console_server, "_THOUGHT_SIGNATURE_LIMIT", 2)

    for index in range(3):
        console_server.remember_thought_signatures(
            {"choices": [{"message": {"tool_calls": [_tool_call(f"call_{index}", f"sig-{index}")]}}]}
        )

    assert len(store) == 2
    assert "call_0" not in store
    assert store == {"call_1": "sig-1", "call_2": "sig-2"}


def test_malformed_payloads_are_ignored(monkeypatch):
    store = _fresh_signature_store(monkeypatch)

    for payload in (None, "text", 7, [], {"extra_content": None}, {"choices": "nope"}):
        assert console_server.remember_thought_signatures(payload) == 0

    assert store == {}
    assert console_server.attach_thought_signatures({"messages": "nope"}) == 0
    assert console_server.attach_thought_signatures({"messages": [None, {"tool_calls": "x"}]}) == 0
