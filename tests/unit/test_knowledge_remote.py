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

"""Retrieval through a console elsewhere (``ARTEMIS_KB_API``).

The split deployment this supports: one machine holds PostgreSQL, the embedding
key and the LLM key; every other machine installs ARTEMIS and plugs in a phone.
Those machines must reach knowledge over HTTP and must hold nothing else, so the
remote path has to work with no database, no embedder and no key of its own.

The path also must never fail a run.  A console that is down, slow, or refusing
the key means *no knowledge* -- the same degradation as a missing database.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from artemis.knowledge import integration as kb


class FakeResponse:
    def __init__(self, payload: dict | str, status: int = 200) -> None:
        self._body = payload if isinstance(payload, str) else json.dumps(payload)
        self.status = status

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (kb.REMOTE_API_ENV, kb.REMOTE_KEY_ENV, kb.REMOTE_TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)


def _capture(monkeypatch, response) -> list:
    """Replace ``urlopen`` and record the requests it was handed."""
    seen: list = []

    def fake_urlopen(request, timeout=None):
        seen.append((request, timeout))
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(kb.urllib.request, "urlopen", fake_urlopen)
    return seen


# ── configuration ────────────────────────────────────────────────────────
def test_no_console_configured_means_the_local_database(monkeypatch):
    assert kb.remote_api_url() == ""
    monkeypatch.setenv(kb.REMOTE_API_ENV, "   ")
    assert kb.remote_api_url() == ""


def test_a_trailing_slash_is_trimmed(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787/")
    assert kb.remote_api_url() == "http://10.0.0.5:8787"


# ── the happy path ───────────────────────────────────────────────────────
def test_the_console_block_is_used_as_is(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    seen = _capture(
        monkeypatch,
        FakeResponse(
            {
                "prompt": "LUẬT CỦA APP\n- modal chuyển nhà là bình thường",
                "found": True,
                "kb": {
                    "app_id": "com.iot.hunonic",
                    "workflows": ["auth.logout"],
                    "trusted": ["auth.logout"],
                },
            }
        ),
    )

    knowledge = kb.retrieve_for_run("Test đăng xuất", app_package="com.iot.hunonic", run_id="r1")

    assert knowledge.found is True
    assert knowledge.injected is True
    assert knowledge.text.startswith("LUẬT CỦA APP")
    assert knowledge.app_id == "com.iot.hunonic"
    assert knowledge.trusted_ids == ["auth.logout"]
    assert seen[0][0].full_url == "http://10.0.0.5:8787/api/knowledge/context"


def test_the_request_carries_the_goal_package_and_run(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    seen = _capture(monkeypatch, FakeResponse({"prompt": "", "found": False, "kb": {}}))

    kb.retrieve_for_run("Test đăng xuất", app_package="com.iot.hunonic", run_id="r7")

    body = json.loads(seen[0][0].data.decode("utf-8"))
    assert body == {"goal": "Test đăng xuất", "app_package": "com.iot.hunonic", "run_id": "r7"}


def test_the_access_key_is_sent_as_a_bearer_token(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    monkeypatch.setenv(kb.REMOTE_KEY_ENV, "21092003")
    seen = _capture(monkeypatch, FakeResponse({"prompt": "", "found": False, "kb": {}}))

    kb.retrieve_for_run("x")

    assert seen[0][0].get_header("Authorization") == "Bearer 21092003"


def test_no_authorization_header_without_a_key(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    seen = _capture(monkeypatch, FakeResponse({"prompt": "", "found": False, "kb": {}}))

    kb.retrieve_for_run("x")

    assert seen[0][0].get_header("Authorization") is None


def test_the_client_identifies_itself(monkeypatch):
    """Cloudflare in front of the console blocks Python's default User-Agent.

    Measured: the same request returns 403 with ``Python-urllib/3.x`` and 200 with
    this one.  Without the header the whole remote path silently degrades to "no
    knowledge", which looks exactly like a healthy run with nothing to inject.
    """
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    seen = _capture(monkeypatch, FakeResponse({"prompt": "", "found": False, "kb": {}}))

    kb.retrieve_for_run("x")

    assert seen[0][0].get_header("User-agent") == kb.REMOTE_USER_AGENT
    assert "urllib" not in kb.REMOTE_USER_AGENT.lower()


def test_the_remote_path_never_builds_a_local_service(monkeypatch):
    """A tester machine has no database; touching one would make retrieval fail."""
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    _capture(monkeypatch, FakeResponse({"prompt": "X", "found": True, "kb": {}}))

    def explode():
        raise AssertionError("remote retrieval must not build the local knowledge service")

    monkeypatch.setattr(kb, "get_service", explode)
    assert kb.retrieve_for_run("x").found is True


def test_a_remote_block_is_marked_as_coming_from_a_console(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    _capture(
        monkeypatch,
        FakeResponse(
            {
                "prompt": "BLOCK",
                "found": True,
                "kb": {"app_id": "hunonic", "workflows": ["auth.logout"]},
            }
        ),
    )

    assert kb.retrieve_for_run("x").source == "console"


def test_a_local_block_is_marked_as_coming_from_the_database():
    """``retrieve_for_run``'s default source, so the log line stays accurate."""
    assert kb.KnowledgeContext(text="X", found=True).source == "database"


def test_the_source_reaches_the_meta(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    _capture(monkeypatch, FakeResponse({"prompt": "X", "found": True, "kb": {}}))

    assert kb.retrieve_for_run("x").meta()["source"] == "console"


# ── degradation ──────────────────────────────────────────────────────────
def test_an_unreachable_console_means_no_knowledge(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    _capture(monkeypatch, OSError("connection refused"))

    knowledge = kb.retrieve_for_run("Test đăng xuất")

    assert knowledge.injected is False
    assert knowledge.degraded is True
    assert "10.0.0.5:8787" in knowledge.reason


def test_a_rejected_key_is_reported_with_its_status(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    _capture(monkeypatch, urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))

    knowledge = kb.retrieve_for_run("x")

    assert knowledge.degraded is True
    assert "401" in knowledge.reason


def test_a_non_json_answer_does_not_raise(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    _capture(monkeypatch, FakeResponse("<html>502 Bad Gateway</html>"))

    knowledge = kb.retrieve_for_run("x")

    assert knowledge.degraded is True
    assert knowledge.injected is False


def test_a_json_answer_that_is_not_an_object_does_not_raise(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    _capture(monkeypatch, FakeResponse("[1, 2, 3]"))

    assert kb.retrieve_for_run("x").degraded is True


def test_an_empty_block_is_not_injected(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    _capture(monkeypatch, FakeResponse({"prompt": "   ", "found": True, "kb": {}}))

    assert kb.retrieve_for_run("x").injected is False


def test_a_degraded_kb_on_the_console_is_surfaced(monkeypatch):
    """The guide half can still answer while the KB half is down."""
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    _capture(
        monkeypatch,
        FakeResponse(
            {
                "prompt": "LUẬT CỦA APP\n- x",
                "found": True,
                "kb": {"degraded": True, "reason": "database không kết nối được"},
            }
        ),
    )

    knowledge = kb.retrieve_for_run("x")

    assert knowledge.injected is True
    assert knowledge.degraded is True
    assert "database" in knowledge.reason


def test_the_timeout_is_configurable(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    monkeypatch.setenv(kb.REMOTE_TIMEOUT_ENV, "3")
    seen = _capture(monkeypatch, FakeResponse({"prompt": "", "found": False, "kb": {}}))

    kb.retrieve_for_run("x")

    assert seen[0][1] == 3.0


def test_a_nonsense_timeout_falls_back_instead_of_raising(monkeypatch):
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    monkeypatch.setenv(kb.REMOTE_TIMEOUT_ENV, "sớm")
    seen = _capture(monkeypatch, FakeResponse({"prompt": "", "found": False, "kb": {}}))

    kb.retrieve_for_run("x")

    assert seen[0][1] == 20.0


# ── prompt đã có kiến thức sẵn ───────────────────────────────────────────
def test_a_presupplied_prompt_skips_retrieval_entirely(monkeypatch):
    """Console ghép sẵn và tester đã sửa; lấy thêm là chèn hai lần."""
    monkeypatch.setenv(kb.REMOTE_API_ENV, "http://10.0.0.5:8787")
    monkeypatch.setenv(kb.PRESUPPLIED_ENV, "1")
    seen = _capture(monkeypatch, FakeResponse({"prompt": "X", "found": True, "kb": {}}))

    knowledge = kb.retrieve_for_run("x")

    assert seen == [], "không được gọi máy chủ khi prompt đã có kiến thức"
    assert knowledge.injected is False
    assert "ghép sẵn" in knowledge.reason


def test_a_presupplied_prompt_does_not_fall_back_to_the_local_database(monkeypatch):
    """Máy tester không có database; rơi về đường local là hỏng."""
    monkeypatch.setenv(kb.PRESUPPLIED_ENV, "yes")

    def explode():
        raise AssertionError("không được dựng knowledge service khi prompt đã có kiến thức")

    monkeypatch.setattr(kb, "get_service", explode)
    assert kb.retrieve_for_run("x").injected is False


def test_the_presupplied_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv(kb.PRESUPPLIED_ENV, raising=False)
    assert kb.knowledge_presupplied() is False
    monkeypatch.setenv(kb.PRESUPPLIED_ENV, "0")
    assert kb.knowledge_presupplied() is False
    monkeypatch.setenv(kb.PRESUPPLIED_ENV, " TRUE ")
    assert kb.knowledge_presupplied() is True
