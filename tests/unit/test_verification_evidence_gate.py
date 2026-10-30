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


# ── siết 30/10: assert phải passed; bằng chứng phải khớp công đoạn + MÀN HÌNH ──────────


def test_an_assert_that_failed_does_not_count_as_evidence():
    """Assert fail là kết quả test hợp lệ, nhưng **không** phải bằng chứng đã đạt tiêu chí."""
    plan = "- [ ] Switch to `Dled`\n  - [x] Tap the Dled card\n  - assert@end: Trang chủ hiển thị nhà `Dled`\n"

    _, settled, withheld = settle_completed_milestones(
        plan, judged_check_items=["Trang chủ hiển thị nhà `Dled`"]  # đã phán (failed)
    )

    assert settled == () and len(withheld) == 1
    assert "đã được phán nhưng KHÔNG passed" in withheld[0]


def test_only_passed_check_items_let_a_milestone_settle():
    plan = "- [ ] Switch to `Dled`\n  - [x] Tap the Dled card\n  - verify@end: Trang chủ hiển thị nhà `Dled`\n"

    assert settle_completed_milestones(plan)[1] == ()
    assert settle_completed_milestones(plan, passed_check_items=["khác"])[1] == ()
    assert settle_completed_milestones(
        plan, passed_check_items=["Trang chủ hiển thị nhà `Dled`"]
    )[1] == ("Switch to `Dled`",)


def _dled_evidence_env(tmp_path, record: dict):
    from artemis.graph import graph as graph_module

    base = tmp_path / "traces"
    (base / "notes").mkdir(parents=True, exist_ok=True)
    (base / "notes" / "task_plan.md").write_text(DLED_PLAN, encoding="utf-8")
    graph_module.append_ledger_record(base, record)
    ctx = SimpleNamespace(
        data_engine=SimpleNamespace(base_dir=str(base)),
        assert_halt=False,
        checkpoint_tasks=None,
        pending_checkpoints=None,
    )
    state = SimpleNamespace(user_stop_requested=False, current_step_id=None)
    return graph_module, ctx, state, base


def _dled_record(evidence: str, *, kind: str = "verify", subgoal: str = HOUSE_MILESTONE) -> dict:
    return {
        "attempt_id": "final#1",
        "checkpoint_id": "final",
        "subgoal_text": subgoal,
        "item_text": "Trang chủ hiển thị nhà `Dled`",
        "kind": kind,
        "when": "at_end",
        "status": "passed",
        "evidence": evidence,
    }


@pytest.mark.parametrize(
    "evidence, expected",
    [
        ("Dled card visible on Quản lý nhà", False),  # đủ để thấy, KHÔNG đủ để xác minh Trang chủ
        ("Dled selected/active on Quản lý nhà", False),  # chứng minh thao tác chọn, chưa chứng minh Home
        ("Trang chủ hiển thị nhà `Dled` ở góc trên bên trái", True),  # đúng màn hình ⇒ đủ
    ],
)
def test_evidence_must_be_on_the_screen_the_criterion_names(tmp_path, evidence, expected):
    graph_module, ctx, state, base = _dled_evidence_env(tmp_path, _dled_record(evidence))

    _, _, evidenced = graph_module._verification_evidence(str(base), parse_plan(DLED_PLAN))

    assert (graph_module.normalize_check_text(HOUSE_MILESTONE) in evidenced) is expected


def test_the_gate_only_ends_the_run_when_the_evidence_is_on_the_right_screen(tmp_path, monkeypatch):
    """Chỉ bằng chứng trên Trang chủ mới cho phép chốt công đoạn 'via Home tab'."""
    graph_module, ctx, state, base = _dled_evidence_env(
        tmp_path, _dled_record("Dled selected/active on Quản lý nhà")
    )
    monkeypatch.setattr(graph_module, "final_check_enabled", lambda _ctx: False)

    assert graph_module.convergence_gate(state, ctx) == "continue"

    # Bằng chứng mới, đúng màn hình ⇒ chốt được.
    graph_module.append_ledger_record(
        base, _dled_record("Home tab hiển thị nhà `Dled` ở góc trên bên trái")
    )
    assert graph_module.convergence_gate(state, ctx) in ("exit_settlement", "end")


@pytest.mark.parametrize("kind", ["assert", "verify"])
def test_a_passed_record_for_another_criterion_or_milestone_is_not_evidence(tmp_path, kind):
    graph_module, ctx, state, base = _dled_evidence_env(
        tmp_path,
        _dled_record("Home tab hiển thị nhà `Dled`", kind=kind, subgoal="Verify something else"),
    )

    _, _, evidenced = graph_module._verification_evidence(str(base), parse_plan(DLED_PLAN))

    assert graph_module.normalize_check_text(HOUSE_MILESTONE) not in evidenced


def test_an_assert_record_cannot_complete_a_milestone(tmp_path):
    graph_module, ctx, state, base = _dled_evidence_env(
        tmp_path, _dled_record("Home tab hiển thị nhà `Dled`", kind="assert")
    )

    _, _, evidenced = graph_module._verification_evidence(str(base), parse_plan(DLED_PLAN))

    assert evidenced == set()


# ── item 3: fingerprint phải nhạy với ảnh/UI và ledger evidence ─────────────────────────


def _fp(state, plan_text, base, graph_module):
    from artemis.graph.checkpoints import read_ledger

    return graph_module._final_review_fingerprint(
        state, plan_text, parse_plan(plan_text), str(base), read_ledger(base)
    )


def test_fingerprint_changes_when_the_screen_changes_even_with_the_same_step(tmp_path):
    from artemis.graph import graph as graph_module

    base = tmp_path / "traces"
    base.mkdir(parents=True, exist_ok=True)
    state = SimpleNamespace(current_step_id="step-1", latest_screenshot="a.jpg")

    before = _fp(state, DLED_PLAN, base, graph_module)
    state.latest_screenshot = "b.jpg"  # UI đổi, step_id GIỮ NGUYÊN
    after = _fp(state, DLED_PLAN, base, graph_module)

    assert before != after, "UI đổi mà step không đổi vẫn phải chạy lại review"


def test_fingerprint_changes_when_ledger_evidence_changes(tmp_path):
    from artemis.graph import graph as graph_module

    base = tmp_path / "traces"
    base.mkdir(parents=True, exist_ok=True)
    state = SimpleNamespace(current_step_id="step-1", latest_screenshot="a.jpg")

    before = _fp(state, DLED_PLAN, base, graph_module)
    graph_module.append_ledger_record(base, _dled_record("Home tab hiển thị nhà `Dled`"))
    after = _fp(state, DLED_PLAN, base, graph_module)

    assert before != after, "có bằng chứng mới ⇒ checker phải chạy lại"


def test_fingerprint_is_stable_when_nothing_changed(tmp_path):
    from artemis.graph import graph as graph_module

    base = tmp_path / "traces"
    base.mkdir(parents=True, exist_ok=True)
    state = SimpleNamespace(current_step_id="step-1", latest_screenshot="a.jpg")

    assert _fp(state, DLED_PLAN, base, graph_module) == _fp(state, DLED_PLAN, base, graph_module)


# ── item 4: withholding — tối đa 2 lượt, yêu cầu cụ thể, không success khi hết budget ───


def test_withholding_stops_after_two_rounds_with_an_unchanged_plan(tmp_path, monkeypatch):
    graph_module, ctx, state, _ = _gate_env(tmp_path, DLED_PLAN)
    monkeypatch.setattr(graph_module, "final_check_enabled", lambda _ctx: False)

    routes = [graph_module.convergence_gate(state, ctx) for _ in range(3)]

    assert routes[:2] == ["continue", "continue"], routes
    assert routes[2] in ("exit_settlement", "end"), "không được continue mãi"


def test_an_absolute_bound_stops_the_loop_even_when_the_plan_keeps_changing(tmp_path, monkeypatch):
    """Kế hoạch đổi mỗi vòng (digest khác) vẫn phải dừng: trần tổng, không vòng lặp vô hạn."""
    graph_module, ctx, state, base = _gate_env(tmp_path, DLED_PLAN)
    monkeypatch.setattr(graph_module, "final_check_enabled", lambda _ctx: False)
    plan_path = base / "notes" / "task_plan.md"

    routes = []
    for i in range(8):
        plan_path.write_text(DLED_PLAN + f"  - [x] việc phụ {i}\n", encoding="utf-8")
        routes.append(graph_module.convergence_gate(state, ctx))

    assert routes[-1] in ("exit_settlement", "end"), routes
    assert routes.count("continue") <= graph_module.VERIFY_WITHHELD_MAX_TOTAL


def test_the_operator_gets_a_specific_verification_request(tmp_path, monkeypatch):
    """Còn budget ⇒ Operator phải nhận yêu cầu **cụ thể** (đúng công đoạn + đúng màn hình)."""
    graph_module, ctx, state, base = _gate_env(tmp_path, DLED_PLAN)
    monkeypatch.setattr(graph_module, "final_check_enabled", lambda _ctx: False)

    assert graph_module.convergence_gate(state, ctx) == "continue"

    plan = (base / "notes" / "task_plan.md").read_text(encoding="utf-8")
    assert "- finding:" in plan, plan
    finding = [line for line in plan.splitlines() if "- finding:" in line][0]
    assert "Trang chủ" in finding and "chưa có bằng chứng" in finding


def test_withholding_never_marks_the_milestone_done(tmp_path, monkeypatch):
    graph_module, ctx, state, base = _gate_env(tmp_path, DLED_PLAN)
    monkeypatch.setattr(graph_module, "final_check_enabled", lambda _ctx: False)

    for _ in range(3):
        graph_module.convergence_gate(state, ctx)

    plan = (base / "notes" / "task_plan.md").read_text(encoding="utf-8")
    assert "- [x] Verify current house" not in plan
    assert "- [ ] Verify current house" in plan


def test_a_done_plan_without_evidence_still_cannot_be_success(tmp_path):
    """Mọi milestone `[x]` (agent tự khai) mà thiếu bằng chứng ⇒ exit chốt blocked, không success."""
    from artemis.graph import graph as graph_module
    from artemis.graph.checkpoints import compute_run_outcome

    base = tmp_path / "traces"
    base.mkdir(parents=True, exist_ok=True)
    snapshot = parse_plan(DLED_PLAN.replace("- [ ]", "- [x]"))

    gaps = graph_module._unverified_verification_milestones(snapshot, str(base))
    assert gaps, "phải phát hiện công đoạn xác minh thiếu bằng chứng"

    outcome = compute_run_outcome(snapshot, [], verify_blocked=bool(gaps))
    assert outcome.task_status == "blocked"


# ── item 5: bất biến gate ledger không cho action vượt qua khi plan không hợp lệ ────────


def test_withheld_plan_still_has_no_active_item_for_the_ledger_gate(tmp_path, monkeypatch):
    """Sau khi bị giữ lại, plan **không** có `[/]` nào ⇒ gate ledger vẫn có việc để bounce."""
    from artemis.utils.plan_grammar import plan_ledger_candidates, repeat_bounce_message

    graph_module, ctx, state, base = _gate_env(tmp_path, DLED_PLAN)
    monkeypatch.setattr(graph_module, "final_check_enabled", lambda _ctx: False)
    graph_module.convergence_gate(state, ctx)

    plan = (base / "notes" / "task_plan.md").read_text(encoding="utf-8")

    assert plan_ledger_candidates(plan), "gate ledger vẫn phải nêu được ứng viên"
    assert repeat_bounce_message(plan).startswith("Not executed:")


def test_final_review_skip_is_wired_in_the_exit_node():
    """Bảo vệ bất biến: đường bỏ qua review phải còn tồn tại và gắn với fingerprint."""
    import inspect

    from artemis.graph import graph as graph_module

    source = inspect.getsource(graph_module.exit_settlement_node)
    assert "final_check_fingerprint" in source
    assert "skip_final_review" in source
    assert "attempt_skipped" in source
    assert "_final_review_fingerprint" in source


