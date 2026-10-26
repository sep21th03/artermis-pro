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

"""Wiring retrieval into a run.

Two properties carry the weight here: a run with no usable knowledge must behave
exactly as it did before the feature existed, and a vector-only guess must never
reach the Planner as executable steps.
"""

from __future__ import annotations

import pytest

from artemis.context import ArtemisContext, DeviceContext
from artemis.knowledge.integration import (
    KnowledgeContext,
    attach,
    context_meta,
    context_text,
    record_outcome,
    resolve_app_id,
    retrieve_for_run,
    usage_report,
)
from artemis.knowledge.models import RetrievalResult, Workflow, WorkflowHit, WorkflowStep
from artemis.knowledge.stores.base import KnowledgeUnavailable


class FakeStore:
    def __init__(self, *, app_id="hunonic", raises=None):
        self.app_id = app_id
        self.raises = raises
        self.outcomes = []

    def find_app_by_package(self, package_name):
        if self.raises:
            raise self.raises
        return self.app_id

    def record_outcome(self, run_id, outcome):
        self.outcomes.append((run_id, outcome))
        return 1

    def list_retrieval_logs(self, run_id=None, limit=50):
        return [{"id": 1, "run_id": run_id, "query": "q"}]


class FakeService:
    def __init__(self, *, available=True, reason="", store=None, result=None, raises=None):
        self.available = available
        self.degraded_reason = reason
        self._store = store if store is not None else FakeStore()
        self._result = result
        self._raises = raises
        self.context_max_chars = 4000

    def context_for(self, query, app_id, version_id=None, run_id=None):
        from artemis.knowledge.context import build_context

        if self._raises:
            raise self._raises
        result = self._result or RetrievalResult(found=False, query=query, app_id=app_id)
        return result, build_context(result, self.context_max_chars)


def _workflow(workflow_id="auth.logout", **kwargs) -> Workflow:
    payload = {
        "id": workflow_id,
        "app_id": "hunonic",
        "name": "Đăng xuất tài khoản",
        "steps": [WorkflowStep(action="tap", target="Tab cuối cùng trên bottom navigation")],
        "status": "verified",
    }
    payload.update(kwargs)
    return Workflow(**payload)


def _hit(*, trusted: bool, channels: dict | None = None, **kwargs) -> WorkflowHit:
    return WorkflowHit(
        workflow=_workflow(**kwargs),
        score=0.03,
        channels=channels or ({"exact": 1} if trusted else {"vector": 1}),
        trusted=trusted,
    )


@pytest.fixture
def ctx():
    device = DeviceContext(device_id="unit-test", device_width=1080, device_height=2400)
    return ArtemisContext(device=device)


# ── app resolution ───────────────────────────────────────────────────────
def test_resolve_app_id_uses_the_registered_package():
    assert resolve_app_id("com.hunonic.app", FakeService()) == "hunonic"


def test_resolve_app_id_is_none_without_a_package():
    assert resolve_app_id(None, FakeService()) is None
    assert resolve_app_id("", FakeService()) is None


def test_resolve_app_id_survives_a_broken_store():
    service = FakeService(store=FakeStore(raises=KnowledgeUnavailable("down")))
    assert resolve_app_id("com.hunonic.app", service) is None


# ── retrieval ────────────────────────────────────────────────────────────
def test_a_disabled_knowledge_base_yields_no_text():
    service = FakeService(available=False, reason="Chưa cấu hình ARTEMIS_KB_DSN")
    knowledge = retrieve_for_run("Test đăng xuất", "com.hunonic.app", service=service)
    assert knowledge.injected is False
    assert knowledge.degraded is True
    assert knowledge.reason == "Chưa cấu hình ARTEMIS_KB_DSN"


def test_an_unregistered_app_yields_no_text_and_says_why():
    service = FakeService(store=FakeStore(app_id=None))
    knowledge = retrieve_for_run("Test đăng xuất", "com.la.app", service=service)
    assert knowledge.injected is False
    assert knowledge.degraded is False
    assert "com.la.app" in knowledge.reason


def test_a_successful_lookup_carries_text_and_the_trusted_ids():
    service = FakeService(
        result=RetrievalResult(
            found=True, query="q", app_id="hunonic", hits=[_hit(trusted=True)]
        )
    )
    knowledge = retrieve_for_run("Test đăng xuất", "com.hunonic.app", service=service)
    assert knowledge.injected is True
    assert knowledge.found is True
    assert knowledge.trusted_ids == ["auth.logout"]
    assert knowledge.app_id == "hunonic"


def test_a_retrieval_exception_degrades_instead_of_raising():
    service = FakeService(raises=RuntimeError("boom"))
    knowledge = retrieve_for_run("Test đăng xuất", "com.hunonic.app", service=service)
    assert knowledge.injected is False and knowledge.degraded is True
    assert "boom" in knowledge.reason


def test_the_planner_never_receives_steps_from_an_unconfirmed_guess():
    """The rule the whole trust model exists for."""
    service = FakeService(
        result=RetrievalResult(
            found=False,
            query="chụp ảnh màn hình",
            app_id="hunonic",
            hits=[_hit(trusted=False, channels={"vector": 1})],
            reason="chưa đủ tin cậy",
        )
    )
    knowledge = retrieve_for_run("chụp ảnh màn hình", "com.hunonic.app", service=service)
    assert "Tab cuối cùng trên bottom navigation" not in knowledge.text
    assert "[chạm vào]" not in knowledge.text
    assert knowledge.trusted_ids == []


# ── carrying it on the context ───────────────────────────────────────────
def test_attach_and_read_back_on_a_real_artemis_context(ctx):
    """The design rests on ArtemisContext accepting an extra attribute."""
    attach(ctx, KnowledgeContext(text="BLOCK", found=True, trusted_ids=["auth.logout"]))
    assert context_text(ctx) == "BLOCK"
    assert context_meta(ctx)["trusted_ids"] == ["auth.logout"]


def test_a_context_without_knowledge_reads_as_empty(ctx):
    assert context_text(ctx) == ""
    assert context_meta(ctx) == {}


def test_whitespace_only_knowledge_is_not_injected():
    knowledge = KnowledgeContext(text="   \n ")
    assert knowledge.injected is False
    attach(ArtemisContext(device=DeviceContext(device_id="d")), knowledge)
    assert context_text(ArtemisContext(device=DeviceContext(device_id="d"))) == ""


def test_a_guess_only_result_is_not_injected_into_the_run():
    """Requirement: no suitable workflow means behaving as if RAG were off.

    The guess block is still rendered (the console preview shows it), but it
    must not reach the Planner, or a run with no confirmed knowledge would stop
    looking like a run without a knowledge base.
    """
    service = FakeService(
        result=RetrievalResult(
            found=False,
            query="chụp ảnh màn hình",
            app_id="hunonic",
            hits=[_hit(trusted=False, channels={"vector": 1})],
        )
    )
    knowledge = retrieve_for_run("chụp ảnh màn hình", "com.hunonic.app", service=service)
    assert knowledge.text  # something was rendered...
    assert knowledge.injected is False  # ...but it is not injected
    ctx = ArtemisContext(device=DeviceContext(device_id="d"))
    attach(ctx, knowledge)
    assert context_text(ctx) == ""


def test_attach_never_raises_on_a_hostile_context():
    class Frozen:
        def __setattr__(self, name, value):
            raise RuntimeError("no")

    attach(Frozen(), KnowledgeContext(text="BLOCK"))
    assert context_text(Frozen()) == ""


# ── outcome logging ──────────────────────────────────────────────────────
def test_record_outcome_writes_to_the_store():
    store = FakeStore()
    service = FakeService(store=store)
    updated = record_outcome("run-1", {"task_status": "completed"}, service=service)
    assert updated == 1
    assert store.outcomes == [("run-1", {"task_status": "completed"})]


def test_record_outcome_without_a_run_id_does_nothing():
    store = FakeStore()
    assert record_outcome("", {"task_status": "completed"}, service=FakeService(store=store)) == 0
    assert record_outcome(None, {"task_status": "completed"}, service=FakeService(store=store)) == 0
    assert store.outcomes == []


def test_record_outcome_never_raises_when_the_knowledge_base_is_down():
    service = FakeService(available=False)
    assert record_outcome("run-1", {"task_status": "completed"}, service=service) == 0


def test_usage_report_reports_degraded_without_a_knowledge_base():
    report = usage_report("run-1", service=FakeService(available=False))
    assert report["degraded"] is True and report["logs"] == []


def test_usage_report_returns_the_logs():
    report = usage_report("run-1", service=FakeService())
    assert report["degraded"] is False
    assert report["logs"][0]["run_id"] == "run-1"
