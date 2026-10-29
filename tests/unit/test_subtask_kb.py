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

"""KB theo công đoạn: tra đúng id, cache theo công đoạn, không đoán thiết bị.

Vì sao khoá bằng test: đo trên KB thật 29/10/2026, 26 mục ``device_action.*`` có trong KB nhưng
**câu task đầy đủ không kéo về mục nào**, và nâng ``TOP_K`` 3→12 cũng không đổi. Cơ chế này thay
phạm vi truy vấn từ "cả câu task" sang "đoạn đang làm" và phải tra đúng **một lần** cho mỗi công
đoạn (tra sau mỗi tap là thứ bị cấm).
"""

from __future__ import annotations

from types import SimpleNamespace

from artemis.knowledge.subtask_kb import (
    MAX_ENTRIES,
    SELECTOR_ID,
    SubtaskKbCache,
    detect_device_type,
    detect_picker_step,
    get_cache,
    subtask_kb_block,
    subtask_kb_ids,
)
from artemis.utils.execution_state import build_execution_state

PLAN = (
    "- [/] Tạo ngữ cảnh chạy khi ấn cho công tắc\n"
    "  - [x] Mở màn ngữ cảnh\n"
    "  - [/] Thêm hành động cho ngữ cảnh\n"
    "- [ ] Lưu ngữ cảnh\n"
)


def _active_state(plan: str = PLAN):
    return build_execution_state(plan, "tạo ngữ cảnh cho công tắc", "")


def _recording_lookup(seen: list[dict]):
    def lookup(**kwargs):
        seen.append(kwargs)
        return f"[KB] {kwargs.get('workflow_id')} ok"

    return lookup


# ── nhận diện ───────────────────────────────────────────────────────────
def test_picker_step_is_recognised_from_the_subtask_and_from_the_screen():
    assert detect_picker_step("Thêm hành động cho ngữ cảnh")
    assert detect_picker_step("chọn thiết bị")
    assert not detect_picker_step("đặt tên ngữ cảnh")


def test_device_type_comes_from_the_screen_first():
    match = detect_device_type("chọn thiết bị", "Danh sách: Công tắc 10, Ổ cắm 12")

    assert match is not None
    assert match.workflow_id == "device_action.switch"
    assert match.source == "ui"


def test_device_type_falls_back_to_the_subtask_when_the_screen_is_empty():
    match = detect_device_type("chọn hành động cho rèm phòng khách", "")

    assert match is not None
    assert match.workflow_id == "device_action.blind"
    assert match.source == "subtask"


def test_no_evidence_means_no_guess():
    assert detect_device_type("đặt tên ngữ cảnh", "Ô tên ngữ cảnh") is None
    assert subtask_kb_ids("đặt tên ngữ cảnh", "Ô tên ngữ cảnh") == ()


def test_a_store_is_not_a_door_device():
    """ "cửa hàng" không được khớp thành thiết bị cửa."""
    assert detect_device_type("mở cửa hàng", "Cửa hàng") is None


def test_switch_is_not_mistaken_for_a_gate():
    """Sau khi bỏ dấu, "công tắc" chứa "công" — từng khớp nhầm sang luật cổng (gate)."""
    match = detect_device_type("chọn hành động", "Công tắc 10")

    assert match is not None and match.workflow_id == "device_action.switch"


def test_remote_is_not_mistaken_for_a_blind():
    match = detect_device_type("chọn hành động", "RF remote 1")

    assert match is not None and match.workflow_id == "device_action.rf_remote"


def test_at_most_two_entries_and_the_specific_one_comes_first():
    ids = subtask_kb_ids("chọn thiết bị và hành động cho công tắc", "Công tắc 10")

    assert len(ids) <= MAX_ENTRIES
    assert ids[0] == "device_action.switch"
    assert SELECTOR_ID in ids


def test_the_sheet_variant_wins_when_the_screen_shows_the_sheet():
    """Màn hình đang hiện sheet ⇒ luật của sheet là luật **đầu tiên** (luật dòng trong danh sách đã
    dùng xong); luật chung của bộ chọn vẫn được phép đi kèm trong hạn mức 2 mục."""
    ids = subtask_kb_ids("chọn hành động cửa", "Chọn trạng thái — Mở cửa / Đóng cửa")

    assert ids[0] == "device_action.bottomsheet.sdoor"
    assert len(ids) <= MAX_ENTRIES


def test_without_a_picker_step_only_the_sheet_rule_is_used():
    ids = subtask_kb_ids("chọn trạng thái cửa", "Chọn trạng thái — Mở cửa / Đóng cửa")

    assert ids == ("device_action.bottomsheet.sdoor",)


# ── tra KB: đúng id, đúng số lần ────────────────────────────────────────
def test_lookup_uses_exact_ids_and_carries_the_package():
    seen: list[dict] = []
    cache = SubtaskKbCache()

    block = subtask_kb_block(
        _active_state(),
        "Danh sách: Công tắc 10",
        app_package="com.iot.hunonic",
        lookup=_recording_lookup(seen),
        cache=cache,
    )

    assert [row["workflow_id"] for row in seen] == ["device_action.switch", SELECTOR_ID]
    assert all(row["app_package"] == "com.iot.hunonic" for row in seen)
    assert "[KB THEO CÔNG ĐOẠN]" in block
    assert "device_action.switch" in block


def test_many_operator_turns_in_one_subtask_cost_exactly_one_lookup_round():
    """Yêu cầu trung tâm: cache theo công đoạn ⇒ **không** tra lại sau mỗi tap."""
    seen: list[dict] = []
    cache = SubtaskKbCache()
    lookup = _recording_lookup(seen)
    state = _active_state()

    first = subtask_kb_block(state, "Công tắc 10", lookup=lookup, cache=cache)
    for _turn in range(5):
        again = subtask_kb_block(state, "Công tắc 10", lookup=lookup, cache=cache)
        assert again == first

    assert cache.lookups == 1
    assert len(seen) == 2  # đúng 2 id cho công đoạn này, không phải 6 lượt × 2
    assert cache.hits == 5


def test_a_new_subtask_triggers_a_new_lookup_round():
    seen: list[dict] = []
    cache = SubtaskKbCache()
    lookup = _recording_lookup(seen)

    subtask_kb_block(_active_state(), "Công tắc 10", lookup=lookup, cache=cache)
    other = build_execution_state(
        "- [/] Tạo ngữ cảnh chạy khi ấn cho rèm\n  - [/] Thêm hành động cho ngữ cảnh\n",
        "tạo ngữ cảnh cho rèm",
        "",
    )
    subtask_kb_block(other, "Rèm phòng khách", lookup=lookup, cache=cache)

    assert cache.lookups == 2
    assert [row["workflow_id"] for row in seen][:2] == ["device_action.switch", SELECTOR_ID]
    assert "device_action.blind" in [row["workflow_id"] for row in seen]


def test_the_same_subtask_with_a_different_device_is_a_new_lookup_round():
    """Khoá cache là ``subtask_id + device_type``: đổi thiết bị thì phải tra lại (luật khác).

    Công đoạn ở đây **không** nêu loại thiết bị, nên loại lấy từ màn hình — đúng ca "agent đang mở
    danh sách và chọn từng thiết bị". (Nếu công đoạn đã nêu loại thì công đoạn thắng và màn hình
    không làm đổi khoá — đó là hành vi mong muốn, xem test ngay dưới.)
    """
    seen: list[dict] = []
    cache = SubtaskKbCache()
    lookup = _recording_lookup(seen)
    neutral = build_execution_state(
        "- [/] Tạo ngữ cảnh chạy khi ấn\n  - [/] Thêm hành động cho ngữ cảnh\n",
        "tạo ngữ cảnh",
        "",
    )

    subtask_kb_block(neutral, "Công tắc 10", lookup=lookup, cache=cache)
    subtask_kb_block(neutral, "Rèm phòng khách", lookup=lookup, cache=cache)

    assert cache.lookups == 2
    assert "device_action.switch" in [row["workflow_id"] for row in seen]
    assert "device_action.blind" in [row["workflow_id"] for row in seen]


def test_the_subtask_intent_wins_over_a_busy_device_list():
    """Màn danh sách chứa nhiều thiết bị: phải theo loại mà **công đoạn** nói tới.

    Đo trên ví dụ thật: màn hình có "Công tắc 10 … Rèm phòng khách" và bản đầu chọn ``rèm`` chỉ vì
    nó đứng trước trong bảng từ khoá.
    """
    match = detect_device_type(
        "Thêm hành động cho ngữ cảnh Select a switch device",
        "[12] Công tắc 10 [13] Ổ cắm 12 [14] Rèm phòng khách",
    )

    assert match is not None
    assert match.workflow_id == "device_action.switch"
    assert match.source == "subtask"


def test_no_active_subtask_means_no_lookup_and_no_noise():
    seen: list[dict] = []
    empty = build_execution_state("", "mở app", "")

    assert (
        subtask_kb_block(empty, "bất kỳ", lookup=_recording_lookup(seen), cache=SubtaskKbCache())
        == ""
    )
    assert seen == []


def test_a_subtask_without_device_evidence_stays_silent():
    seen: list[dict] = []
    state = build_execution_state(
        "- [/] Đặt tên ngữ cảnh\n  - [/] Nhập tên\n", "đặt tên ngữ cảnh", ""
    )

    block = subtask_kb_block(
        state, "Ô tên ngữ cảnh", lookup=_recording_lookup(seen), cache=SubtaskKbCache()
    )

    assert block == ""
    assert seen == []


def test_the_block_stays_within_its_budget():
    cache = SubtaskKbCache()
    huge = lambda **kwargs: "x" * 5000  # noqa: E731 - cố tình phình để đo trần

    block = subtask_kb_block(
        _active_state(), "Công tắc 10", lookup=huge, cache=cache, max_chars=300
    )

    assert len(block) <= 320
    assert block.endswith("…(đã cắt bớt)")


def test_the_cache_lives_on_the_run_context():
    ctx = SimpleNamespace()

    first = get_cache(ctx)
    second = get_cache(ctx)

    assert first is second


# ── nối vào prompt của Operator ─────────────────────────────────────────
def test_the_prompt_component_injects_the_block_once_per_subtask(monkeypatch):
    """Component chạy trong **nhiều lượt** của cùng một công đoạn ⇒ chỉ một lần tra KB."""
    import asyncio

    from artemis.agents.operator.prompts import PromptBuilder, SubtaskKbPromptComponent
    from artemis.graph.state import State

    seen: list[dict] = []
    monkeypatch.setattr(
        "artemis.knowledge.subtask_kb._default_lookup", _recording_lookup(seen), raising=True
    )
    ctx = SimpleNamespace(execution_setup=None)
    state = State.initial("tạo ngữ cảnh cho công tắc")
    component = SubtaskKbPromptComponent()

    blocks: list[str] = []
    for _turn in range(4):
        builder = PromptBuilder()
        asyncio.run(
            component(
                builder,
                state,
                ctx,
                task_plan=PLAN,
                observed_identifiers=set(),
                minimal_list="Danh sách thiết bị: Công tắc 10",
            )
        )
        blocks.append("\n".join(builder.human_parts))

    assert all(block == blocks[0] for block in blocks)
    assert "[KB THEO CÔNG ĐOẠN]" in blocks[0]
    # 4 lượt × 2 id nếu không cache; có cache thì đúng 2 lần tra cho cả 4 lượt.
    assert len(seen) == 2


def test_the_prompt_component_stays_silent_without_evidence(monkeypatch):
    import asyncio

    from artemis.agents.operator.prompts import PromptBuilder, SubtaskKbPromptComponent
    from artemis.graph.state import State

    seen: list[dict] = []
    monkeypatch.setattr(
        "artemis.knowledge.subtask_kb._default_lookup", _recording_lookup(seen), raising=True
    )
    state = State.initial("đặt tên ngữ cảnh")
    builder = PromptBuilder()

    asyncio.run(
        SubtaskKbPromptComponent()(
            builder,
            state,
            SimpleNamespace(execution_setup=None),
            task_plan="- [/] Đặt tên ngữ cảnh\n  - [/] Nhập tên\n",
            observed_identifiers=set(),
            minimal_list="Ô tên ngữ cảnh",
        )
    )

    assert "\n".join(builder.human_parts).strip() == ""
    assert seen == []
