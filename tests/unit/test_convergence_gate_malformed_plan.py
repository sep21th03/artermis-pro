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

"""``convergence_gate``: phân biệt plan rỗng thật / plan malformed / plan còn việc.

Ca thật 29/10/2026: plan thụt lề ⇒ ``has_top_level == False`` ⇒ cổng kết thúc run ngay sau khi agent
mở bảng chọn thiết bị, final check chốt ``blocked`` dù việc còn dang dở. Cổng phải **không kết thúc
nhầm**, nhưng cũng không được mở vòng lặp vô hạn: mỗi ``continue`` vẫn phải qua một lượt Operator và
chịu mọi chốt dừng hiện có (``assert_halt``, ``user_stop_requested``, trần thời gian, trần tool).
"""

from __future__ import annotations

from types import SimpleNamespace

from artemis.graph import graph as graph_module
from artemis.utils.plan_grammar import PlanItem, PlanSnapshot, parse_plan


class _Engine:
    def __init__(self, base_dir):
        self.base_dir = base_dir


def _ctx(tmp_path, *, plan: str, assert_halt: bool = False):
    notes = tmp_path / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    if plan is not None:
        (notes / "task_plan.md").write_text(plan, encoding="utf-8")
    return SimpleNamespace(
        data_engine=_Engine(str(tmp_path)),
        assert_halt=assert_halt,
        checkpoint_tasks={},
        pending_checkpoints=[],
    )


#: Plan có item nhưng không dòng nào top-level — trạng thái B1 đã loại bỏ cho plan thụt lề, nên
#: nhánh này là **phòng thủ nhiều lớp** (parser đổi, định dạng lạ). Test tiêm đúng snapshot đó.
NO_TOP_LEVEL_UNFINISHED = PlanSnapshot(
    items=(
        PlanItem(indent=2, status=" ", text="Bước một", line_no=0),
        PlanItem(indent=2, status="/", text="Bước hai", line_no=1),
    )
)
NO_TOP_LEVEL_ALL_DONE = PlanSnapshot(
    items=(
        PlanItem(indent=2, status="x", text="Bước một", line_no=0),
        PlanItem(indent=2, status="x", text="Bước hai", line_no=1),
    )
)


def test_a_plan_with_items_but_no_top_level_does_not_end_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_module, "parse_plan", lambda _content: NO_TOP_LEVEL_UNFINISHED)

    route = graph_module.convergence_gate(SimpleNamespace(), _ctx(tmp_path, plan="x"))

    assert route == "continue"


def test_a_truly_empty_plan_still_goes_to_settlement(tmp_path):
    route = graph_module.convergence_gate(SimpleNamespace(), _ctx(tmp_path, plan=""))

    assert route in {"exit_settlement", "end"}


def test_items_without_top_level_that_are_all_done_go_to_settlement(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_module, "parse_plan", lambda _content: NO_TOP_LEVEL_ALL_DONE)

    route = graph_module.convergence_gate(SimpleNamespace(), _ctx(tmp_path, plan="x"))

    assert route in {"exit_settlement", "end"}


def test_a_well_formed_plan_with_work_left_continues(tmp_path):
    plan = "- [/] Một\n  - [ ] con\n- [ ] Hai\n"
    assert graph_module.convergence_gate(SimpleNamespace(), _ctx(tmp_path, plan=plan)) == "continue"


def test_assert_halt_still_stops(tmp_path, monkeypatch):
    """Chốt dừng cũ phải thắng: cổng không được biến assert-halt thành continue."""
    monkeypatch.setattr(graph_module, "parse_plan", lambda _content: NO_TOP_LEVEL_UNFINISHED)
    ctx = _ctx(tmp_path, plan="x", assert_halt=True)

    assert graph_module.convergence_gate(SimpleNamespace(), ctx) in {"exit_settlement", "end"}


def test_the_reason_is_logged_with_counts(tmp_path, monkeypatch, caplog):
    """Ghi log rõ nguyên nhân để lần sau chẩn đoán được ngay (yêu cầu B2).

    Lưu ý: ``ArtemisLogger.warning`` chỉ nhận **một** tham số (không nhận ``%s`` kèm args), nên
    thông điệp phải là f-string — test này khoá đúng điều đó (gọi kiểu cũ sẽ ném TypeError ngay
    tại nhánh phòng thủ).
    """
    monkeypatch.setattr(graph_module, "parse_plan", lambda _content: NO_TOP_LEVEL_UNFINISHED)

    with caplog.at_level("WARNING"):
        graph_module.convergence_gate(SimpleNamespace(), _ctx(tmp_path, plan="x"))

    messages = [record.getMessage() for record in caplog.records]
    assert any("NO top-level milestone" in text for text in messages)
    assert any(
        "2 checkbox item(s)" in text and "2 item(s) still unfinished" in text for text in messages
    )


def test_the_normalised_parse_is_what_the_gate_sees(tmp_path):
    """Răng của cả B1+B2: sau chuẩn hoá, plan thụt lề trở thành plan bình thường (3 milestone)."""
    plan = "  - [/] Một\n  - [ ] Hai\n  - [ ] Ba\n"
    snapshot = parse_plan(plan)

    assert len(snapshot.top_level) == 3
    assert graph_module.convergence_gate(SimpleNamespace(), _ctx(tmp_path, plan=plan)) == "continue"
