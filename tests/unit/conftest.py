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

"""Fixtures shared by the unit suite.

No unit test may reach the network or the operator's real knowledge base.  The
console builds its KB service lazily from ``ARTEMIS_KB_DSN``, and the repository
``.env`` sets that to a real database with a real ``GEMINI_API_KEY`` — so a test
that merely reached ``/api/knowledge/context`` or ``/api/kb/*`` would open the
real database and spend real money on an embedding call.  The autouse fixture
below makes the service unavailable by default; tests that exercise those paths
stub ``get_service`` themselves.
"""

import importlib

import pytest


@pytest.fixture(autouse=True)
def knowledge_base_offline(monkeypatch):
    """No unit test may open the real knowledge base or call the embedding API.

    The operator's own configuration must not leak into the suite either.
    ``artemis.config.settings`` calls ``load_dotenv``, which puts the repository
    ``.env`` into ``os.environ`` — so an ``ARTEMIS_KB_API`` pointing at the shared
    server would silently send every local-path test down the remote path and make
    it assert against the wrong thing.
    """
    for name in (
        "ARTEMIS_KB_API",
        "ARTEMIS_KB_API_KEY",
        "ARTEMIS_KB_API_TIMEOUT",
        "ARTEMIS_KB_PRESUPPLIED",
        "ARTEMIS_KB_LOCAL_FIRST",
        "ARTEMIS_KB_REMOTE_COOLDOWN",
        "ARTEMIS_KB_REMOTE_TRANSIENT_COOLDOWN",
    ):
        monkeypatch.delenv(name, raising=False)
    # Cầu dao của KB từ xa là trạng thái toàn tiến trình: một test làm nó ngắt mạch sẽ khiến test
    # sau không còn thấy lời gọi remote nữa. Mỗi test phải bắt đầu với cầu dao đóng.
    try:
        from artemis.tools.knowledge import reset_app_id_memo, reset_remote_health
    except Exception:  # noqa: BLE001 - module KB không import được thì bỏ qua
        pass
    else:
        reset_remote_health()
        reset_app_id_memo()
    try:
        importlib.import_module("apps.test_console.server")
    except Exception:  # noqa: BLE001 - the console is an optional app
        return
    import artemis.knowledge.service as service_module

    monkeypatch.setattr(service_module, "get_service", lambda: service_module.KnowledgeService())
