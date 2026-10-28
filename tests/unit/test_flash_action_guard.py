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

"""P0 + P1 cho Flash: dùng lại Action Guard của Pro, và kiểm soát thao tác lặp.

Tám tính chất được khoá ở đây, mỗi cái là một cách hỏng khác nhau:

1. Guard của Flash là **đúng hàm guard của Pro** — không có bộ luật thứ hai.
2. Guard chặn khi phần tử đích đã biến mất khỏi màn hình live, và **không bắn** action đó.
3. Không đọc được hierarchy live (``xml_bypassed``) ⇒ **vẫn bắn**: thiếu dữ liệu không phải
   bằng chứng cú chạm sai.
4. Action không nhắm vào phần tử (``swipe``, ``press_key``…) và ``input_text`` không bị tính là
   "không hiệu quả" — nhập chữ/cuộn hết danh sách không đổi hierarchy là hợp lệ.
5. Lặp y nguyên cùng action + cùng target mà UI không đổi tới hạn ⇒ lần kế bị trả về cho model,
   và câu trả về **không** kết luận action thất bại.
6. UI vừa đổi ⇒ chuỗi "không hiệu quả" của đối tượng đó hết hiệu lực.
7. Guard lỗi vì bất kỳ lý do gì ⇒ **vẫn bắn** (guard không bao giờ làm gãy lượt chạy).
8. Trace có fingerprint trước/sau, ``ui_changed`` và kết quả guard (P1).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from artemis.agents.validator import precondition_xml
from artemis.mcp.action_executor import (
    _GUARD_ACTION_NAMES,
    _XML_GUARDED_ACTIONS,
    McpActionExecutor,
)
from artemis.mcp.action_types import ActionResult, ObserveResult

#: Phần tử trong ``state.indexed_elements`` — shape mà ``_resolve_index`` đọc: khoá có gạch
#: dưới (``resource_id``) và ``bounds`` là **list 4 số**.
INDEXED_LOGIN = {
    "text": "Nút Đăng nhập",
    "resource_id": "com.demo:id/login",
    "class": "android.widget.Button",
    "center": [360, 800],
    "bounds": [300, 760, 420, 840],
}

#: Cùng phần tử đó trong hierarchy **live** — shape mà guard Pro đọc (``_score_element``):
#: ``resource-id`` gạch nối và ``bounds`` là **chuỗi** ``"[l,t][r,b]"``.  Hai shape khác nhau
#: là chuyện thật của code hiện tại, nên test khoá cả hai.
LIVE_LOGIN = {
    "text": "Nút Đăng nhập",
    "resource-id": "com.demo:id/login",
    "class": "android.widget.Button",
    "bounds": "[300,760][420,840]",
}
LIVE_OTHER = [
    {"text": "Trang chủ", "resource-id": "com.demo:id/home", "bounds": "[0,0][200,200]"}
]


class FakeSession:
    """Session giả: đếm số lần thật sự bắn action, và trả hierarchy theo kịch bản."""

    started = True

    def __init__(self, *, hierarchy=None, hierarchy_error: Exception | None = None, screen=None):
        self.calls: list[tuple[str, dict]] = []
        self.observations = 0
        self._hierarchy = hierarchy
        self._hierarchy_error = hierarchy_error
        self._screen = screen if screen is not None else [INDEXED_LOGIN]

    async def call(self, wire_name, wire_args) -> ActionResult:
        self.calls.append((wire_name, dict(wire_args)))
        return ActionResult.success(wire_name, "ok")

    async def observe(self, settle_ms: int = 400) -> ObserveResult:
        self.observations += 1
        text = "|".join(str(el.get("text", "")) for el in self._screen)
        return ObserveResult(ok=True, elements=list(self._screen), elements_text=text)

    async def ui_hierarchy(self, timeout=None):
        if self._hierarchy_error is not None:
            raise self._hierarchy_error
        return list(self._hierarchy) if self._hierarchy is not None else [LIVE_LOGIN]

    async def aclose(self) -> None:
        return None


def fake_state(elements=None) -> SimpleNamespace:
    """State tối thiểu mà bộ chuyển target cần (``indexed_elements``)."""
    return SimpleNamespace(
        indexed_elements=list(elements if elements is not None else [INDEXED_LOGIN]),
        indexed_points=[],
        latest_screenshot=None,
    )


def build_executor(session: FakeSession) -> McpActionExecutor:
    ctx = SimpleNamespace(
        device=SimpleNamespace(device_width=720, device_height=1600), data_engine=None
    )
    actuator = SimpleNamespace(
        controller=None, extensions=lambda: [], capabilities=lambda: frozenset()
    )
    executor = McpActionExecutor(ctx, controller=None, actuator=actuator)
    executor._session = session
    return executor


async def run_click(executor: McpActionExecutor, *, target=1, state=None):
    return await executor.execute("click", {"target": target}, "tc1", state or fake_state())


# ── 1. Dùng lại đúng luật của Pro ─────────────────────────────────────────
def test_the_guard_rules_come_from_the_pro_guard_not_a_second_copy():
    """Flash phải kiểm đúng nhóm action mà Pro kiểm — lệch là test đỏ, không phải im lặng."""
    assert _XML_GUARDED_ACTIONS == frozenset(precondition_xml.XML_VALIDATED_ACTIONS)


def test_actuator_action_names_map_onto_the_guard_vocabulary():
    assert set(_GUARD_ACTION_NAMES.values()) <= _XML_GUARDED_ACTIONS
    assert _GUARD_ACTION_NAMES["click"] == "tap"


# ── 2. Chặn khi đích đã biến mất ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_target_that_left_the_screen_is_not_tapped():
    """Đích không còn trên màn hình live ⇒ trả về cho model, **không** bắn action."""
    session = FakeSession(hierarchy=LIVE_OTHER)  # nút đăng nhập đã biến mất
    executor = build_executor(session)

    result = await run_click(executor)

    assert result.status == "error"
    assert session.calls == [], "action phải bị chặn trước khi bắn"
    guard = result.metadata["guard"]
    assert guard["checked"] is True
    assert guard["passed"] is False
    assert guard["category"] != "none"


@pytest.mark.asyncio
async def test_a_target_still_on_screen_is_tapped():
    session = FakeSession(hierarchy=[LIVE_LOGIN])
    executor = build_executor(session)

    result = await run_click(executor)

    assert result.status == "success"
    assert len(session.calls) == 1
    assert result.metadata["guard"]["passed"] is True


# ── 3. Không đọc được hierarchy ⇒ không chặn ──────────────────────────────
@pytest.mark.asyncio
async def test_an_unreadable_hierarchy_never_blocks_the_action():
    """``xml_bypassed`` là *thiếu dữ liệu*, không phải bằng chứng sai — phải vẫn bắn."""
    session = FakeSession(hierarchy_error=RuntimeError("hierarchy timeout"))
    executor = build_executor(session)

    result = await run_click(executor)

    assert result.status == "success"
    assert len(session.calls) == 1
    assert result.metadata["guard"]["checked"] is False



# ── 4 + 5. Chống lặp: chỉ chặn khi lặp y nguyên mà UI không đổi ───────────
OTHER_INDEXED = {
    "text": "Trang chủ",
    "resource_id": "com.demo:id/home",
    "class": "android.widget.TextView",
    "center": [100, 100],
    "bounds": [0, 0, 200, 200],
}


@pytest.mark.asyncio
async def test_repeating_the_same_tap_without_a_ui_change_is_returned_to_the_model():
    """Lặp y nguyên tới hạn ⇒ lần kế **không bắn**, và câu trả về không kết luận thất bại."""
    session = FakeSession(hierarchy=[LIVE_LOGIN])  # guard luôn cho qua; UI không bao giờ đổi
    executor = build_executor(session)

    results = [await run_click(executor) for _ in range(4)]

    # Lượt 1 chưa có "vân tay trước" (thực tế Flash luôn quan sát trước khi hành động) nên chỉ
    # dựng mốc ⇒ 3 lượt bắn, lượt thứ 4 bị trả về cho model.
    assert [r.status for r in results] == ["success", "success", "success", "error"]
    assert len(session.calls) == 3, "lượt lặp thứ 4 không được bắn"
    refusal = results[-1].text_summary
    assert "did not change" in refusal
    assert "not proof" in refusal, "không được kết luận action thất bại"


@pytest.mark.asyncio
async def test_a_ui_change_clears_the_repeat_streak():
    """UI vừa đổi ⇒ đối tượng đó được phép chạm lại, kể cả cùng target."""
    session = FakeSession(hierarchy=[LIVE_LOGIN])
    executor = build_executor(session)

    await run_click(executor)
    await run_click(executor)  # UI không đổi ⇒ streak = 1

    session._screen = [OTHER_INDEXED]  # màn hình vừa đổi thật
    changed = await run_click(executor)

    assert changed.status == "success"
    assert len(session.calls) == 3


@pytest.mark.asyncio
async def test_typing_is_never_counted_as_a_useless_repeat():
    """Nhập chữ không làm hierarchy đổi — đó là chuyện bình thường, không được chặn."""
    session = FakeSession(hierarchy=[LIVE_LOGIN])
    executor = build_executor(session)

    results = [
        await executor.execute("input_text", {"target": 1, "text": "abc"}, "tc", fake_state())
        for _ in range(4)
    ]

    assert [r.status for r in results] == ["success"] * 4
    assert len(session.calls) == 4


# ── 8. P1: trace đọc lại được ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_result_carries_the_guard_verdict_and_the_ui_effect():
    """P1 — fingerprint trước/sau, ``ui_changed`` và kết quả guard nằm trong metadata."""
    session = FakeSession(hierarchy=[LIVE_LOGIN])
    executor = build_executor(session)

    await run_click(executor)  # lượt đầu dựng mốc vân tay (chưa có "trước" để so)
    result = await run_click(executor)

    effect = result.metadata["effect"]
    assert effect["ui_fingerprint_before"]
    assert effect["ui_fingerprint_after"]
    assert effect["ui_changed"] is False, "cùng một màn hình ⇒ UI không đổi"
    assert result.metadata["guard"] == {
        "checked": True,
        "passed": True,
        "category": "none",
        "reason": "",
    }


# ── 7. Guard hỏng ⇒ vẫn chạy ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_broken_guard_never_breaks_the_run(monkeypatch):
    """Guard là lớp phụ trợ: nó ném lỗi thì action vẫn phải được bắn."""

    async def boom(*args, **kwargs):
        raise RuntimeError("guard exploded")

    monkeypatch.setattr(precondition_xml, "validate_action_precondition_single", boom)
    session = FakeSession(hierarchy=[LIVE_LOGIN])
    executor = build_executor(session)

    result = await run_click(executor)

    assert result.status == "success"
    assert len(session.calls) == 1
    assert result.metadata["guard"]["checked"] is False
