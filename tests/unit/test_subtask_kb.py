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
    require_high_confidence,
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
    """Danh sách phần tử thật: mỗi thiết bị là **một dòng** có ``Bounds``."""
    ui = "[12] Text: '󰄰, Công tắc 10' | Bounds: [63,265][341,389]\n[13] Text: '󰄰, Ổ cắm 12' | Bounds: [354,265][648,389]"
    match = detect_device_type("chọn thiết bị", ui)

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
    nó đứng trước trong bảng từ khoá. Ở đây màn hình có **hai** loại (công tắc, rèm) nên một mình nó
    không đủ căn cứ; công đoạn ("Select a switch device") xác nhận một loại ⇒ giao của hai nguồn đúng
    một loại ⇒ ``ui+subtask`` với độ chắc chắn ``high`` (mạnh hơn cả khi chỉ công đoạn nói).
    """
    ui = (
        "[12] Text: '󰄰, Công tắc 10' | Bounds: [63,265][341,389]\n"
        "[13] Text: '󰄰, Ổ cắm 12' | Bounds: [354,265][648,389]\n"
        "[14] Text: '󰄰, Rèm phòng khách' | Bounds: [669,265][962,389]"
    )
    match = detect_device_type(
        "Thêm hành động cho ngữ cảnh Select a switch device",
        ui,
    )

    assert match is not None
    assert match.workflow_id == "device_action.switch"
    assert match.source == "ui+subtask"
    assert match.confidence == "high"


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


# ── bằng chứng màn hình > công đoạn, và chỉ khi đủ chắc chắn ────────────
def test_screen_only_evidence_is_certain_when_the_subtask_is_silent():
    """Màn hình đúng **một** loại, công đoạn không nói gì ⇒ dùng luật loại đó, mức ``high``."""
    match = detect_device_type("thêm hành động cho ngữ cảnh", "Công tắc 10")

    assert match is not None
    assert (match.source, match.confidence) == ("ui", "high")


def test_subtask_only_evidence_is_medium_confidence():
    """Chỉ công đoạn nói ⇒ vẫn dùng (không có gì mâu thuẫn), nhưng phải nói rõ là chưa chắc."""
    match = detect_device_type("chọn hành động cho rèm phòng khách", "")

    assert match is not None
    assert (match.source, match.confidence) == ("subtask", "medium")


def test_a_conflict_uses_the_screen_and_says_so():
    """Plan và màn hình mâu thuẫn: theo **màn hình** (bằng chứng thật) và ghi rõ mâu thuẫn.

    Không dùng luật của loại mà công đoạn nói tới — đó chính là "KB sai loại" cần tránh. Mâu thuẫn
    được ghi vào ``conflict`` để prompt nói thẳng ra thay vì im lặng dạy sai.
    """
    match = detect_device_type("chọn hành động cho công tắc", "Rèm phòng khách")

    assert match is not None
    assert match.workflow_id == "device_action.blind"
    assert match.source == "ui"
    assert match.confidence == "medium"
    assert match.conflict == "device_action.switch"
    assert "công đoạn nói device_action.switch" in match.evidence()


def test_two_types_on_screen_without_subtask_agreement_is_silent():
    """Màn hình nhiều loại, công đoạn không xác nhận loại nào ⇒ **không** chọn đại một cái.

    Vẫn còn luật **chung** của bộ chọn (``device_action.selector``) vì đó không phải luật của một
    loại thiết bị nào — nhưng không có luật thiết bị nào được đưa.
    """
    assert (
        detect_device_type("thêm hành động cho ngữ cảnh", "Công tắc 10 · Rèm phòng khách") is None
    )

    ids = subtask_kb_ids("thêm hành động cho ngữ cảnh", "Công tắc 10 · Rèm phòng khách")

    assert ids == (SELECTOR_ID,)
    assert "device_action.switch" not in ids and "device_action.blind" not in ids


def test_a_type_name_in_another_item_does_not_pick_a_device():
    """Tên loại trùng với một item khác trên màn hình **không** được tự chọn thành mục tiêu.

    Ca thật đã đo (task ``5b459634``): dòng *"Chuông rung."* trên thanh trạng thái bị khớp thành
    thiết bị doorbell. Sau khi lọc system UI + siết từ khoá, màn hình chỉ còn các dòng thiết bị thật.
    """
    ui = (
        "[12] Text: '󰄰, Công tắc 10' | Bounds: [63,265][341,389]\n"
        "[13] Text: '󰄰, Rèm phòng khách' | Bounds: [354,265][648,389]"
    )
    # Màn hình có hai loại thiết bị, công đoạn không xác nhận loại nào ⇒ không chọn đại.
    assert detect_device_type("chọn hành động cho ngữ cảnh", ui) is None
    # Công đoạn nói một loại **không có trên màn hình** ⇒ màn hình đang mơ hồ, không gán loại nào
    # của màn hình cho nó (và cũng không tự chọn loại của công đoạn khi màn hình đang nói khác).
    assert detect_device_type("chọn hành động cửa", ui) is None
    # Màn hình **không** có loại thiết bị nào (màn đặt tên/lưu) ⇒ chỉ còn bằng chứng công đoạn.
    plan_only = detect_device_type(
        "chọn hành động cửa", "[9] Text: 'Tên ngữ cảnh' | Bounds: [30,300][900,360]"
    )
    assert plan_only is not None
    assert plan_only.source == "subtask"
    assert plan_only.confidence == "medium"


def test_require_high_drops_medium_evidence(monkeypatch):
    """Chính sách chặt: chỉ dùng luật thiết bị khi hai nguồn cùng xác nhận."""
    monkeypatch.setenv("ARTEMIS_SUBTASK_KB_REQUIRE_HIGH", "1")

    assert require_high_confidence() is True
    # Bằng chứng chỉ từ công đoạn (medium) ⇒ bỏ luật thiết bị, chỉ còn luật chung của bộ chọn.
    assert subtask_kb_ids("chọn thiết bị cho rèm", "") == (SELECTOR_ID,)
    # Hai nguồn cùng nói (high) ⇒ vẫn dùng.
    assert subtask_kb_ids("chọn thiết bị cho rèm", "Rèm phòng khách")[0] == "device_action.blind"


def test_the_header_states_confidence_and_conflict():
    seen: list[dict] = []
    block = subtask_kb_block(
        _active_state(),
        "Công tắc 10",
        lookup=_recording_lookup(seen),
        cache=SubtaskKbCache(),
    )

    assert "thiết bị: device_action.switch" in block
    assert "khớp cả màn hình và công đoạn" in block


# ── F-A: system UI không phải bằng chứng thiết bị ───────────────────────
#: Danh sách phần tử thật lấy từ prompt của run 5b459634 (16:02, màn Trang chủ + status bar).
REAL_UI_WITH_STATUS_BAR = (
    "[1] Text: '16:03' | Bounds: [62,8][158,44]\n"
    "[2] Text: 'Ngủ Chế độ' | Bounds: [694,18][736,34]\n"
    "[3] Text: 'Chuông rung.' | Bounds: [738,18][777,34]\n"
    "[4] Text: 'Tín hiệu Wi-Fi đủ.' | Bounds: [780,18][827,34]\n"
    "[5] Text: 'Tín hiệu điện thoại ba vạch.' | Bounds: [827,18][865,34]\n"
    "[6] Text: '100 phần trăm pin.' | Bounds: [873,17][937,35]\n"
    "[7] Text: 'Mesh Tạo 2 , Thái Nguyên' | Bounds: [191,44][493,113]\n"
    "[8] Text: '28°C , Độ ẩm: 55% , Nhiều mây' | Bounds: [68,254][472,330]\n"
    "[9] Text: 'Ngữ cảnh hay dùng' | Bounds: [26,431][973,481]\n"
    "[10] Text: 'Chưa có ngữ cảnh hay dùng, thêm ngay' | Bounds: [108,515][794,538]\n"
    "[11] Text: 'Thiết bị hay dùng' | Bounds: [26,573][973,623]\n"
    "[12] Text: '󰄰, Ổ cắm 12 , Đang bật' | Bounds: [37,627][330,759]\n"
    "[13] Text: '󰄰, Công tắc cảm ứng Hunonic Dali Master , Mất kết nối' | Bounds: [354,627][648,759]\n"
    "[14] Text: '󰄰, Quạt , Mất kết nối' | Bounds: [37,769][330,901]\n"
    "[15] Text: '󰄰' | Bounds: [0,915][200,976]\n"
    "[16] Text: '󰕰' | Bounds: [200,915][400,976]\n"
    "[17] Text: '󰟇' | Bounds: [600,915][800,976]"
)


def test_a_status_bar_ringer_is_not_a_doorbell():
    """F-A: ``Chuông rung.`` trên thanh trạng thái từng bị khớp thành ``device_action.doorbell_hsrf``.

    Đo trên run thật ``5b459634`` (16:02:30): 244 ms tra KB + 1.238 ký tự luật doorbell vào prompt của
    công đoạn "Vào màn ngữ cảnh" — hoàn toàn không liên quan. Màn Trang chủ có nhiều dòng thiết bị nên
    kết quả đúng là **im lặng** (không đoán thiết bị nào).
    """
    assert detect_device_type("Vào màn ngữ cảnh (Tab Ngữ cảnh)", REAL_UI_WITH_STATUS_BAR) is None
    assert "device_action.doorbell_hsrf" not in subtask_kb_ids(
        "Vào màn ngữ cảnh (Tab Ngữ cảnh)", REAL_UI_WITH_STATUS_BAR
    )


def test_a_status_bar_is_not_evidence_even_with_a_real_device_name():
    """Bỏ system UI trước khi khớp: cùng tên thiết bị nằm trên thanh trạng thái thì không tính."""
    status_bar_only = (
        "[1] Text: 'Công tắc 10' | Bounds: [62,8][158,44]\n"
        "[2] Text: 'Ngữ cảnh hay dùng' | Bounds: [26,431][973,481]"
    )
    nav_bar_only = (
        "[1] Text: 'Ngữ cảnh hay dùng' | Bounds: [26,431][973,481]\n"
        "[2] Text: 'Công tắc 10' | Bounds: [0,915][200,976]"
    )

    assert detect_device_type("thêm hành động cho ngữ cảnh", status_bar_only) is None
    assert detect_device_type("thêm hành động cho ngữ cảnh", nav_bar_only) is None
    # Cùng dòng đó, nằm trong vùng nội dung app ⇒ là bằng chứng hợp lệ.
    in_content = "[1] Text: 'Công tắc 10' | Bounds: [63,265][341,389]"
    match = detect_device_type("thêm hành động cho ngữ cảnh", in_content)
    assert match is not None and match.workflow_id == "device_action.switch"


def test_a_bare_ringtone_word_is_not_a_doorbell():
    """Từ khoá doorbell: ``doorbell``/``hsrf``/``nhạc chuông``/``chuông cửa`` — không có "chuông" trần."""
    for text in ("Chuông rung.", "Chuông báo thức", "Chuông thông báo"):
        line = f"[3] Text: '{text}' | Bounds: [10,18][60,34]"
        assert detect_device_type("chọn hành động", line) is None, text


def test_a_real_doorbell_row_is_still_detected():
    """Thiết bị chuông thật vẫn nhận được — tên nằm ở đầu dòng thiết bị."""
    ui = "[20] Text: '󰄰, Chuông cửa' | Bounds: [63,800][341,930]"

    match = detect_device_type("chọn hành động", ui)

    assert match is not None
    assert match.workflow_id == "device_action.doorbell_hsrf"


def test_the_weather_card_is_not_a_sensor():
    """Thẻ thời tiết ở màn Trang chủ không phải cảm biến nhiệt độ/độ ẩm."""
    ui = "[8] Text: '28°C , Độ ẩm: 55% , Nhiều mây' | Bounds: [68,254][472,330]"

    assert detect_device_type("thêm hành động cho ngữ cảnh", ui) is None
    # Nhưng một dòng thiết bị cảm biến thật thì vẫn nhận.
    match = detect_device_type(
        "chọn thiết bị", "[9] Text: '󰄰, Cảm biến nhiệt độ' | Bounds: [63,265][341,389]"
    )
    assert match is not None and match.workflow_id == "device_action.ths_sensor"


def test_a_line_mixing_two_device_names_is_not_evidence():
    """Một dòng chứa nhiều tên thiết bị không phải "một dòng thiết bị" ⇒ bỏ cả dòng."""
    mixed = "[12] Text: 'Công tắc 10 · Rèm phòng khách' | Bounds: [63,265][341,389]"

    assert detect_device_type("thêm hành động cho ngữ cảnh", mixed) is None


def test_the_device_list_still_works_with_the_new_filter():
    """Không được siết quá tay: danh sách chọn thiết bị thật vẫn nhận đúng loại."""
    ui = (
        "[7] Text: 'Chọn hành động' | Bounds: [331,64][669,91]\n"
        "[11] Text: 'Chưa xếp phòng' | Bounds: [52,213][948,256]\n"
        "[12] Text: '󰄰, Ổ cắm 12' | Bounds: [63,265][341,389]\n"
        "[13] Text: '󰄰, Quạt' | Bounds: [354,265][648,389]"
    )
    match = detect_device_type("chọn hành động cho công tắc", ui)

    assert match is not None
    assert match.workflow_id == "device_action.switch"
    assert match.source == "ui+subtask"


# ── cache: hạn dùng, lỗi tạm thời, làm mới ──────────────────────────────
def _failing_lookup(message: str = "[KB] không lấy được hướng dẫn: KB local tra cứu lỗi: timeout"):
    from artemis.core.tool_failure import ToolFailure

    def lookup(**_kwargs):
        return ToolFailure(message)

    return lookup


def test_a_transient_failure_is_not_cached_and_is_retried_after_the_backoff():
    """Lỗi tạm thời không được cache: hết backoff phải thử lại và lấy được KB thật."""
    cache = SubtaskKbCache(retry_seconds=30.0)
    state = _active_state()
    calls: list[str] = []
    answers = {"n": 0}

    def flaky(**kwargs):
        calls.append(kwargs["workflow_id"])
        answers["n"] += 1
        if answers["n"] <= 2:  # cả hai id của công đoạn đều lỗi ở lượt đầu
            return _failing_lookup()()
        return f"[KB] {kwargs['workflow_id']} ok"

    first = subtask_kb_block(state, "Công tắc 10", lookup=flaky, cache=cache, now=100.0)

    assert "không lấy được hướng dẫn" in first  # agent được biết KB đang không trả lời
    assert cache.entries == {}  # nhưng KHÔNG có mục nào được cache
    assert cache.errors == 1

    # Trong khoảng backoff: im lặng, không gọi lại KB.
    before = len(calls)
    assert subtask_kb_block(state, "Công tắc 10", lookup=flaky, cache=cache, now=120.0) == ""
    assert len(calls) == before
    assert cache.suppressed == 1

    # Hết backoff: thử lại và lấy được câu trả lời thật.
    again = subtask_kb_block(state, "Công tắc 10", lookup=flaky, cache=cache, now=131.0)

    assert "[KB THEO CÔNG ĐOẠN]" in again
    assert cache.errors == 1  # không tăng: lần này thành công
    assert cache.hits == 0


def test_the_failure_message_is_shown_once_then_silenced():
    """Không lặp lại cùng một câu lỗi ở mọi lượt Operator."""
    cache = SubtaskKbCache(retry_seconds=60.0)
    state = _active_state()
    lookup = _failing_lookup()

    first = subtask_kb_block(state, "Công tắc 10", lookup=lookup, cache=cache, now=0.0)
    second = subtask_kb_block(state, "Công tắc 10", lookup=lookup, cache=cache, now=1.0)

    assert first.startswith("[KB]")
    assert second == ""


def test_a_cached_answer_expires_and_is_looked_up_again():
    """Hết hạn dùng thì tra lại — KB có thể vừa được cập nhật."""
    cache = SubtaskKbCache(content_ttl=600.0)
    state = _active_state()
    seen: list[dict] = []
    lookup = _recording_lookup(seen)

    subtask_kb_block(state, "Công tắc 10", lookup=lookup, cache=cache, now=0.0)
    subtask_kb_block(state, "Công tắc 10", lookup=lookup, cache=cache, now=599.0)

    assert cache.lookups == 1 and cache.hits == 1

    subtask_kb_block(state, "Công tắc 10", lookup=lookup, cache=cache, now=601.0)

    assert cache.lookups == 2
    assert cache.expired == 1


def test_an_empty_answer_has_a_shorter_ttl_than_content():
    """ "Không có mục nào khớp" hết hạn sớm hơn nội dung thật (KB có thể vừa được bổ sung)."""
    cache = SubtaskKbCache(content_ttl=600.0, empty_ttl=120.0)
    state = _active_state()
    silent = lambda **_kwargs: ""  # noqa: E731 - KB không có gì để trả, không phải lỗi

    assert subtask_kb_block(state, "Công tắc 10", lookup=silent, cache=cache, now=0.0) == ""

    empty_key = next(iter(cache.entries))
    assert cache.entries[empty_key].expires_at == 120.0

    cache.put(("s", "d"), "[KB] có nội dung", now=0.0)
    assert cache.entries[("s", "d")].expires_at == 600.0


def test_invalidate_and_force_refresh_make_a_fresh_lookup():
    """Làm mới KB khi cần: ``invalidate`` và ``force_refresh`` đều buộc tra lại."""
    cache = SubtaskKbCache()
    state = _active_state()
    seen: list[dict] = []
    lookup = _recording_lookup(seen)

    subtask_kb_block(state, "Công tắc 10", lookup=lookup, cache=cache)
    assert cache.lookups == 1

    assert cache.invalidate() == 1
    subtask_kb_block(state, "Công tắc 10", lookup=lookup, cache=cache)
    assert cache.lookups == 2

    subtask_kb_block(state, "Công tắc 10", lookup=lookup, cache=cache, force_refresh=True)
    assert cache.lookups == 3


def test_a_partial_answer_is_cached_with_the_short_ttl():
    """Một id lỗi, một id trả lời ⇒ có KB để dùng, nhưng hạn dùng ngắn để sớm bù phần thiếu."""
    cache = SubtaskKbCache(content_ttl=600.0, empty_ttl=120.0)
    state = _active_state()

    def half(**kwargs):
        if kwargs["workflow_id"] == SELECTOR_ID:
            return _failing_lookup()()
        return "[KB] device_action.switch ok"

    block = subtask_kb_block(state, "Công tắc 10", lookup=half, cache=cache, now=0.0)

    assert "[KB THEO CÔNG ĐOẠN]" in block
    key = next(iter(cache.entries))
    assert cache.get(key, now=100.0) == block  # còn trong hạn ngắn
    assert cache.get(key, now=200.0) is None  # hết hạn ⇒ tra lại để bù phần thiếu
    assert cache.errors == 0  # vẫn có câu trả lời ⇒ không tính là lượt lỗi


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
