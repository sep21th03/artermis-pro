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

"""Bất biến chống "thành công giả": `[x]` **không** phải bằng chứng cho tiêu chí xác minh.

Đo trên máy thật 30/10/2026 (session 93882dc3): milestone
"Verify current house via Home tab and switch to `Dled`" có mọi dòng con `[x]` (agent tự khai, chưa
hề đọc tên nhà trên Trang chủ) ⇒ ``settle_completed_milestones`` chốt nó **từ checkbox** ⇒
convergence gate kết thúc run ⇒ plan không có check item nào nên ``run_outcome`` **không hề được
ghi** và run được coi là thành công.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from artemis.utils.plan_grammar import (
    milestone_evidence_gap,
    parse_plan,
    settle_completed_milestones,
    verification_intent_items,
)

DLED_PLAN = (
    "- [x] Verify current account via Menu tab and ensure account `0972842548` is logged in\n"
    "  - [x] Tap Menu tab at bottom right to view account info\n"
    "- [ ] Verify current house via Home tab and switch to `Dled` if current house is different\n"
    "  - [x] Tap Home tab at bottom left and check house name at top-left\n"
    "  - [x] If house is not `Dled`, navigate to Menu -> Quản lý nhà and select `Dled`\n"
)

HOUSE_MILESTONE = "Verify current house via Home tab and switch to `Dled` if current house is different"


def test_the_dled_milestone_is_withheld_without_evidence():
    """Đúng ca đã sai trên máy thật: mọi việc con `[x]` nhưng chưa có bằng chứng xác minh."""
    text, settled, withheld = settle_completed_milestones(DLED_PLAN)

    assert settled == ()
    assert len(withheld) == 1 and "xác minh" in withheld[0]
    assert text == DLED_PLAN  # không đổi một ký tự nào
    assert "- [ ] Verify current house" in text


def test_the_dled_milestone_settles_once_the_home_screen_was_verified():
    """Có bằng chứng (ledger record passed gắn với công đoạn) ⇒ được chốt như cũ."""
    text, settled, withheld = settle_completed_milestones(
        DLED_PLAN, evidenced_milestones=[HOUSE_MILESTONE]
    )

    assert settled == (HOUSE_MILESTONE,)
    assert withheld == ()
    assert "- [x] Verify current house via Home tab" in text


def test_a_declared_verify_check_item_must_have_passed():
    plan = (
        "- [ ] Switch to `Dled`\n"
        "  - [x] Tap the Dled card\n"
        "  - verify@end: Trang chủ hiển thị nhà `Dled`\n"
    )

    _, settled, withheld = settle_completed_milestones(plan)
    assert settled == () and len(withheld) == 1 and "chưa có verdict passed" in withheld[0]

    _, settled_ok, withheld_ok = settle_completed_milestones(
        plan, passed_check_items=["Trang chủ hiển thị nhà `Dled`"]
    )
    assert settled_ok == ("Switch to `Dled`",) and withheld_ok == ()


def test_a_plan_without_verification_intent_is_untouched():
    """Không đổi hành vi với plan không khai xác minh và không có check item."""
    plan = "- [ ] Mở app\n  - [x] Tap icon\n"

    text, settled, withheld = settle_completed_milestones(plan)

    assert settled == ("Mở app",) and withheld == ()
    assert "- [x] Mở app" in text


def test_verification_intent_detection_covers_vietnamese_and_english():
    snapshot = parse_plan(DLED_PLAN)
    intents = [item.text for item in verification_intent_items(snapshot)]

    assert any("Verify current house" in t for t in intents)
    assert any("check house name" in t for t in intents)

    vn = parse_plan("- [ ] Kiểm tra nhà hiện tại trên Trang chủ\n")
    assert len(verification_intent_items(vn)) == 1


def test_milestone_evidence_gap_explains_the_gap():
    snapshot = parse_plan(DLED_PLAN)
    house = [item for item in snapshot.top_level if "house" in item.text][0]

    gap = milestone_evidence_gap(snapshot, house)
    assert gap and "chưa có bằng chứng" in gap

    assert milestone_evidence_gap(snapshot, house, evidenced_milestones=[house.text]) is None


@pytest.mark.parametrize("status", ["failed", "inconclusive"])
def test_only_a_passed_record_counts_as_evidence(status):
    """`failed`/`inconclusive` không phải bằng chứng — nếu tính, ta lại tự lừa mình."""
    snapshot = parse_plan(DLED_PLAN)
    house = [item for item in snapshot.top_level if "house" in item.text][0]

    assert milestone_evidence_gap(snapshot, house, evidenced_milestones=[]) is not None
    # chỉ status passed mới nằm trong danh sách bằng chứng (xem `_verification_evidence`)
    assert status in ("failed", "inconclusive")


# ── tầng graph: gate giữ lại, hết ngân sách thì blocked, không lặp checker ──────────────


def _gate_env(tmp_path: Path, plan_text: str, ledger: list[dict] | None = None):
    """Stub tối thiểu cho `convergence_gate`: note task_plan + ledger trong thư mục tạm."""
    from artemis.graph import graph as graph_module

    base = tmp_path / "traces"
    notes = base / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "task_plan.md").write_text(plan_text, encoding="utf-8")
    for record in ledger or []:
        graph_module.append_ledger_record(base, record)

    ctx = SimpleNamespace(
        data_engine=SimpleNamespace(base_dir=str(base)),
        assert_halt=False,
        checkpoint_tasks=None,
        pending_checkpoints=None,
    )
    state = SimpleNamespace(user_stop_requested=False, current_step_id=None)
    return graph_module, ctx, state, base


def _passed_record(subgoal: str, item: str = "Trang chủ hiển thị nhà `Dled`") -> dict:
    return {
        "attempt_id": "final#1",
        "checkpoint_id": "final",
        "subgoal_text": subgoal,
        "item_text": item,
        "kind": "verify",
        "when": "at_end",
        "status": "passed",
        "evidence": "Trang chủ hiển thị `Dled`",
    }


def test_gate_does_not_end_the_run_while_verification_evidence_is_missing(tmp_path, monkeypatch):
    graph_module, ctx, state, _ = _gate_env(tmp_path, DLED_PLAN)
    monkeypatch.setattr(graph_module, "final_check_enabled", lambda _ctx: False)

    route = graph_module.convergence_gate(state, ctx)

    assert route == "continue", "thiếu bằng chứng xác minh ⇒ không được kết thúc run"
    # công đoạn vẫn là việc đang làm (không bị chốt từ checkbox)
    assert "- [ ] Verify current house" in (Path(ctx.data_engine.base_dir) / "notes" / "task_plan.md").read_text()


def test_gate_ends_the_run_once_evidence_exists(tmp_path, monkeypatch):
    graph_module, ctx, state, _ = _gate_env(
        tmp_path, DLED_PLAN, ledger=[_passed_record(HOUSE_MILESTONE)]
    )
    monkeypatch.setattr(graph_module, "final_check_enabled", lambda _ctx: False)

    route = graph_module.convergence_gate(state, ctx)

    assert route in ("exit_settlement", "end")
    assert "- [x] Verify current house" in (Path(ctx.data_engine.base_dir) / "notes" / "task_plan.md").read_text()


def test_gate_gives_up_after_the_verification_budget(tmp_path, monkeypatch):
    """Kế hoạch không đổi qua nhiều vòng ⇒ hết ngân sách ⇒ kết thúc (để exit chốt blocked)."""
    graph_module, ctx, state, _ = _gate_env(tmp_path, DLED_PLAN)
    monkeypatch.setattr(graph_module, "final_check_enabled", lambda _ctx: False)

    routes = [graph_module.convergence_gate(state, ctx) for _ in range(5)]

    assert routes[0] == "continue"
    assert routes[-1] in ("exit_settlement", "end"), routes


def test_unverified_milestones_helper_reports_the_dled_case(tmp_path):
    from artemis.graph import graph as graph_module

    base = tmp_path / "traces"
    base.mkdir(parents=True, exist_ok=True)

    gaps = graph_module._unverified_verification_milestones(parse_plan(DLED_PLAN), str(base))

    # Cả hai công đoạn đều tự khai xác minh mà không có bằng chứng: công đoạn nhà là ca đã sai trên
    # máy thật, công đoạn tài khoản là cùng loại (được agent tự chốt `[x]`).
    assert len(gaps) == 2
    assert any("house" in gap for gap in gaps)
    assert all("chưa có bằng chứng" in gap for gap in gaps)


def test_run_outcome_is_blocked_when_verification_is_unmet():
    from artemis.graph.checkpoints import compute_run_outcome

    snapshot = parse_plan(DLED_PLAN)
    blocked = compute_run_outcome(snapshot, [], verify_blocked=True)
    assert blocked.task_status == "blocked"

    completed = compute_run_outcome(parse_plan(DLED_PLAN.replace("- [ ]", "- [x]")), [], verify_blocked=False)
    assert completed.task_status == "completed"


def test_verification_evidence_reads_only_passed_records(tmp_path):
    from artemis.graph import graph as graph_module

    base = tmp_path / "traces"
    base.mkdir(parents=True, exist_ok=True)
    graph_module.append_ledger_record(
        base, {**_passed_record("Verify current house"), "status": "failed"}
    )
    graph_module.append_ledger_record(
        base, {**_passed_record("Verify current account"), "item_text": "menu tab visible"}
    )

    passed_items, judged_items, evidenced = graph_module._verification_evidence(str(base))

    assert "menu tab visible" in passed_items
    # record `failed` ⇒ **đã được phán** nhưng KHÔNG phải bằng chứng passed (assert cần judged,
    # verify cần passed).
    assert graph_module.normalize_check_text("Trang chủ hiển thị nhà `Dled`") in judged_items
    assert graph_module.normalize_check_text("Verify current house") not in evidenced


def test_final_review_is_not_repeated_without_new_evidence(tmp_path, monkeypatch):
    """Cùng plan + cùng bước ⇒ lần kiểm tra thứ hai bị bỏ qua (không tốn LLM vô ích)."""
    import inspect

    from artemis.graph import graph as graph_module

    source = inspect.getsource(graph_module.exit_settlement_node)
    assert "final_check_fingerprint" in source
    assert "skip_final_review" in source
    assert "attempt_skipped" in source

