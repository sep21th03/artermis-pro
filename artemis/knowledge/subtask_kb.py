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

"""KB theo **công đoạn đang làm** — lấy đúng 1–2 mục liên quan, không nhồi cả KB.

**Vì sao cần.** Đo trên KB thật 29/10/2026: 26 mục ``device_action.*`` (luật chọn thiết bị và
hành động khi tạo ngữ cảnh/hẹn giờ) **có** trong KB, nhưng truy hồi theo câu task đầy đủ
(``"vào màn ngữ cảnh chạy khi ấn tạo ngữ cảnh cho công tắc…"``) **không trả về mục nào trong số
đó** — và nâng ``ARTEMIS_KB_TOP_K`` 3→12 cũng không đổi (vẫn đúng 4 hit ``scene.*``). Ngược lại,
khi truy vấn đúng *đoạn đang làm* (``"chọn công tắc"``) thì ``device_action.switch`` xếp **hạng 1**
và ``device_action.selector`` hạng 3. Nói cách khác: vấn đề là **phạm vi truy vấn**, không phải số
lượng hay việc mục đó là ``draft``.

Module này chỉ làm ba việc, đều tất định và test được:

1. Từ công đoạn đang làm (``ExecutionState`` — nguồn sự thật duy nhất) suy ra **id cần tra**:
   bước chọn thiết bị/hành động ⇒ ``device_action.selector``; nhận ra loại thiết bị ⇒
   ``device_action.<type>`` tương ứng.
2. Tra **đúng id đó** qua ``lookup_knowledge`` (cơ chế KB sẵn có, không dựng engine mới) — tra theo
   id tốn **0 embedding**.
3. Cache theo ``(subtask_id, device_type)`` ⇒ mỗi công đoạn chỉ tra **một lần**, không tra lại sau
   mỗi tap.

Không suy đoán: không có bằng chứng về bước chọn thiết bị và cũng không nhận ra loại thiết bị ⇒
trả chuỗi rỗng (im lặng), thà thiếu hướng dẫn còn hơn dạy sai.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from artemis.knowledge.text import normalize

#: Cache regex theo từ khoá (mỗi từ khoá biên dịch một lần cho cả run).
_WORD_RE_CACHE: dict[str, re.Pattern] = {}

#: Bộ chọn dùng chung cho mọi luồng ngữ cảnh/hẹn giờ/liên kết.
SELECTOR_ID = "device_action.selector"

#: Trần số mục đưa vào prompt cho một công đoạn (yêu cầu thiết kế: 1–2).
MAX_ENTRIES = 2

#: Trần ký tự của khối KB theo công đoạn.
DEFAULT_MAX_CHARS = 1400

#: Dấu hiệu công đoạn **đang ở bước chọn thiết bị / chọn hành động**.
#: Viết ở dạng đã bỏ dấu vì ``normalize`` bỏ dấu cả hai phía.
PICKER_TERMS: tuple[str, ...] = (
    "chon thiet bi",
    "chon hanh dong",
    "them hanh dong",
    "hanh dong cho ngu canh",
    "danh sach thiet bi",
    "select device",
    "select action",
    "add action",
    "choose device",
    "device list",
)

#: Loại thiết bị → id KB. Thứ tự **quan trọng**: mục cụ thể đứng trước mục chung, vì "công tắc ổ
#: cắm" phải rơi vào luật chu kỳ công tắc chứ không phải luật nào khác. Từ khoá lấy từ chính
#: ``name``/``aliases`` của 26 mục ``device_action.*`` (không tự nghĩ thêm), viết dạng bỏ dấu.
DEVICE_TYPE_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("device_action.neo_r6", ("neo r6", "neor6", "neo_r6", "sau nut", "6 nut")),
    ("device_action.sdoor", ("sdoor", "hanh dong cua", "chon cua", "door action", "cua")),
    ("device_action.ocs_wifi", ("ocs", "ocs wifi")),
    ("device_action.gate_hun", ("gate hun", "cong hai canh")),
    # "cổng" một mình bỏ dấu thành "cong" và trùng với "công" trong "công tắc" ⇒ chỉ dùng cụm
    # riêng của chính entry ("gate", "nút cổng") để không dạy sai luật thiết bị.
    ("device_action.gate", ("gate", "nut cong")),
    ("device_action.blind", ("rem", "blind", "curtain")),
    ("device_action.switch", ("cong tac", "o cam", "switch", "socket", "plug", "bat tat")),
    ("device_action.rf_button_collection", ("rf collection", "nut rf")),
    ("device_action.rf_remote", ("rf remote", "remote rf")),
    ("device_action.rf_event", ("rf motion", "rf sos", "rf door")),
    ("device_action.ir_rf_child", ("hong ngoai", "dieu hoa", "tivi", "ir child", "rf child")),
    ("device_action.driver_led_dali", ("dali", "driver led")),
    ("device_action.led_ht", ("led ht",)),
    ("device_action.meter_atm", ("atm", "dong ho dien", "dien nang", "meter")),
    ("device_action.ths_sensor", ("ths", "nhiet do", "do am")),
    ("device_action.motion_rada_sensor", ("rada", "mos", "cam bien chuyen dong")),
    ("device_action.stair_led", ("stair led", "den cau thang")),
    ("device_action.doorbell_hsrf", ("doorbell", "hsrf", "chuong")),
    ("device_action.buoy_thief_shock", ("phao", "chong trom", "shock")),
    ("device_action.smart_lock", ("smart lock", "slock", "tadolock", "khoa thong minh")),
    ("device_action.io_gateway", ("io gateway", "multi output")),
    ("device_action.camera", ("camera", "rayshap")),
    ("device_action.fan", ("quat", "fan")),
)

#: Loại thiết bị có **bottom sheet** riêng: khi màn hình đang hiện sheet thì luật của sheet mới là
#: luật cần đọc, còn luật của dòng trong danh sách đã dùng xong.
SHEET_VARIANTS: dict[str, str] = {
    "device_action.sdoor": "device_action.bottomsheet.sdoor",
    "device_action.neo_r6": "device_action.bottomsheet.neo_r6",
}

#: Dấu hiệu màn hình **đang hiện bottom sheet chọn trạng thái** (lấy từ chính nội dung 2 mục
#: ``device_action.bottomsheet.*``: "Chọn trạng thái", "Mở cửa", "Đóng cửa", "Dừng cửa", "Khoá cửa").
SHEET_TERMS: tuple[str, ...] = (
    "chon trang thai",
    "mo cua",
    "dong cua",
    "dung cua",
    "khoa cua",
    "mo khoa",
)

#: Cụm từ chỉ *cửa hàng* (store) — loại trừ để "cửa" không khớp nhầm.
_STORE_TERMS: tuple[str, ...] = ("cua hang", "store", "shop")


@dataclass(frozen=True)
class DeviceTypeMatch:
    """Loại thiết bị nhận ra được, kèm **bằng chứng** (từ khoá + nguồn)."""

    workflow_id: str
    term: str
    source: str  # "ui" | "subtask"

    def as_dict(self) -> dict:
        return {"workflow_id": self.workflow_id, "term": self.term, "source": self.source}


@dataclass
class SubtaskKbCache:
    """Cache theo ``(subtask_id, device_type)`` — một công đoạn chỉ tra KB **một lần**."""

    blocks: dict[tuple[str, str], str] = field(default_factory=dict)
    lookups: int = 0
    hits: int = 0

    def get(self, key: tuple[str, str]) -> str | None:
        value = self.blocks.get(key)
        if value is not None:
            self.hits += 1
        return value

    def put(self, key: tuple[str, str], value: str) -> None:
        self.blocks[key] = value


def get_cache(ctx: object) -> SubtaskKbCache:
    """Cache sống theo **run**: ``ctx`` là đối tượng của một lần chạy, không phải toàn cục."""
    cache = getattr(ctx, "subtask_kb_cache", None)
    if not isinstance(cache, SubtaskKbCache):
        cache = SubtaskKbCache()
        try:
            setattr(ctx, "subtask_kb_cache", cache)
        except Exception:  # noqa: BLE001 - không set được thì vẫn dùng cache cục bộ của lượt
            return cache
    return cache


# ── Nhận diện ───────────────────────────────────────────────────────────
def _term_pattern(term: str):
    """Khớp **theo từ**: ``rem`` không được khớp trong ``remote``.

    Vì sao không dùng ``in``: sau khi bỏ dấu, các từ khác nghĩa hay chồng nhau ("rem" ⊂ "remote",
    "cong" ⊂ "cong tac"), và khớp chuỗi con sẽ chọn sai luật thiết bị — sai ở đây là dạy agent
    thao tác của loại thiết bị khác.
    """
    return _WORD_RE_CACHE.setdefault(term, re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)"))


def _matches(text: str, term: str) -> bool:
    return bool(_term_pattern(term).search(text))


def detect_picker_step(*texts: str) -> bool:
    """Công đoạn (hoặc màn hình) đang ở bước chọn thiết bị / chọn hành động."""
    haystack = normalize(" ".join(t for t in texts if t))
    return any(_matches(haystack, term) for term in PICKER_TERMS)


def _first_match_in_text(text: str) -> DeviceTypeMatch | None:
    """Loại thiết bị xuất hiện **sớm nhất** trong ``text`` (không theo thứ tự bảng).

    Vì sao theo vị trí: một màn danh sách có thể chứa nhiều thiết bị ("Công tắc 10 … Rèm phòng
    khách"). Duyệt theo thứ tự bảng thì thiết bị nào đứng trước trong bảng sẽ thắng, bất kể màn hình
    đang nói tới cái nào — đo trên ví dụ thật: ra ``device_action.blind`` (rèm) trong khi công đoạn
    nói về công tắc.
    """
    haystack = normalize(text or "")
    if not haystack:
        return None
    best: tuple[int, DeviceTypeMatch] | None = None
    for workflow_id, terms in DEVICE_TYPE_TERMS:
        for term in terms:
            # "cửa" trong "cửa hàng" không phải thiết bị cửa.
            if term == "cua" and any(store in haystack for store in _STORE_TERMS):
                continue
            found = _term_pattern(term).search(haystack)
            if not found:
                continue
            if best is None or found.start() < best[0]:
                best = (found.start(), DeviceTypeMatch(workflow_id, term, ""))
    return best[1] if best else None


def detect_device_type(subtask_text: str, observed_text: str) -> DeviceTypeMatch | None:
    """Loại thiết bị theo **bằng chứng**, ưu tiên công đoạn rồi mới tới màn hình.

    Ưu tiên **công đoạn** vì công đoạn nói *ý định của bước này* ("thêm hành động cho công tắc"),
    còn màn hình danh sách chỉ là nơi chứa nhiều thiết bị. Màn hình vẫn là nguồn khi công đoạn chưa
    nêu loại thiết bị (màn hình đang mở đúng danh sách có "Công tắc 10"). Không suy đoán: không khớp
    từ khoá nào ⇒ ``None``.
    """
    for source, text in (("subtask", subtask_text), ("ui", observed_text)):
        match = _first_match_in_text(text)
        if match is not None:
            return DeviceTypeMatch(match.workflow_id, match.term, source)
    return None


def subtask_kb_ids(subtask_text: str, observed_text: str) -> tuple[str, ...]:
    """Id cần tra cho công đoạn này — nhiều nhất :data:`MAX_ENTRIES`, mục cụ thể đứng trước.

    * Bước chọn thiết bị/hành động ⇒ ``device_action.selector``.
    * Nhận ra loại thiết bị ⇒ ``device_action.<type>`` (hoặc biến thể ``.bottomsheet.`` nếu màn hình
      đang hiện sheet chọn trạng thái).
    * Không có bằng chứng nào ⇒ rỗng (im lặng).
    """
    ids: list[str] = []
    match = detect_device_type(subtask_text, observed_text)
    if match is not None:
        variant = SHEET_VARIANTS.get(match.workflow_id)
        on_sheet = any(term in normalize(observed_text or "") for term in SHEET_TERMS)
        ids.append(variant if (variant and on_sheet) else match.workflow_id)
    if detect_picker_step(subtask_text, observed_text):
        ids.append(SELECTOR_ID)
    ordered: list[str] = []
    for workflow_id in ids:
        if workflow_id not in ordered:
            ordered.append(workflow_id)
    return tuple(ordered[:MAX_ENTRIES])


def _default_lookup(**kwargs) -> str:
    from artemis.tools.knowledge import lookup_knowledge

    return lookup_knowledge(**kwargs)


def subtask_kb_block(
    exec_state: object,
    observed_text: str = "",
    *,
    app_package: str = "",
    run_id: str | None = None,
    lookup=None,
    cache: SubtaskKbCache | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Khối KB cho **công đoạn đang làm**, hoặc ``""`` khi không có gì chắc chắn để đưa.

    Một công đoạn ⇒ một lần tra (cache theo ``subtask_id + device_type``). Tra theo **id** nên
    không tốn embedding, và chỉ 1–2 mục vào prompt thay vì cả 26.
    """
    active = exec_state.active() if exec_state is not None else None
    if active is None:
        return ""
    # Văn bản công đoạn = dòng milestone + các dòng con: bằng chứng cho bước đang làm ("Thêm hành
    # động cho ngữ cảnh", "Select "Công tắc 10"") nằm ở dòng con, không nằm ở dòng milestone.
    children = list(getattr(active, "children", None) or [])
    subtask_text = " ".join([getattr(active, "text", "") or "", *children]).strip()
    ids = subtask_kb_ids(subtask_text, observed_text)
    if not ids:
        return ""

    match = detect_device_type(subtask_text, observed_text)
    device_key = match.workflow_id if match else "-"
    key = (str(getattr(active, "id", "") or subtask_text), device_key)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached

    fetcher = lookup or _default_lookup
    parts: list[str] = []
    for workflow_id in ids:
        text = fetcher(
            workflow_id=workflow_id,
            app_package=app_package,
            run_id=run_id,
            max_results=1,
        )
        if text:
            parts.append(str(text).strip())
    if cache is not None:
        cache.lookups += 1

    if not parts:
        block = ""
    else:
        evidence = (
            f"thiết bị: {match.workflow_id} ({match.term}, theo {'màn hình' if match.source == 'ui' else 'công đoạn'})"
            if match
            else "chưa xác định được loại thiết bị"
        )
        header = (
            "[KB THEO CÔNG ĐOẠN] "
            f"công đoạn «{subtask_text[:90]}» · {evidence} · id tra: {', '.join(ids)}"
        )
        block = header + "\n" + "\n".join(parts)
        if len(block) > max_chars:
            block = block[:max_chars].rstrip() + "\n…(đã cắt bớt)"
    if cache is not None:
        cache.put(key, block)
    return block


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEVICE_TYPE_TERMS",
    "MAX_ENTRIES",
    "PICKER_TERMS",
    "SELECTOR_ID",
    "SHEET_TERMS",
    "SHEET_VARIANTS",
    "DeviceTypeMatch",
    "SubtaskKbCache",
    "detect_device_type",
    "detect_picker_step",
    "get_cache",
    "subtask_kb_block",
    "subtask_kb_ids",
]
