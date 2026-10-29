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
3. Cache theo ``(subtask_id, device_type)`` ⇒ mỗi công đoạn chỉ tra **một lần** mỗi hạn dùng, không
   tra lại sau mỗi tap; lỗi tạm thời **không** bị cache.

Loại thiết bị lấy theo **bằng chứng, ưu tiên màn hình thật** (xem :func:`detect_device_type`): hai
nguồn cùng nói một loại ⇒ chắc chắn; chỉ một nguồn nói ⇒ có dùng nhưng ghi rõ là chưa chắc; màn hình
có nhiều loại mà công đoạn không xác nhận loại nào ⇒ **im lặng**, không đoán.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

from artemis.core.tool_failure import is_tool_failure
from artemis.knowledge.text import normalize

#: Cache regex theo từ khoá (mỗi từ khoá biên dịch một lần cho cả run).
_WORD_RE_CACHE: dict[str, re.Pattern] = {}

#: Bộ chọn dùng chung cho mọi luồng ngữ cảnh/hẹn giờ/liên kết.
SELECTOR_ID = "device_action.selector"

#: Trần số mục đưa vào prompt cho một công đoạn (yêu cầu thiết kế: 1–2).
MAX_ENTRIES = 2

#: Trần ký tự của khối KB theo công đoạn.
DEFAULT_MAX_CHARS = 1400

#: Chỉ dùng luật thiết bị khi nhận diện **chắc chắn** (bỏ qua bằng chứng chỉ từ một phía).
REQUIRE_HIGH_ENV = "ARTEMIS_SUBTASK_KB_REQUIRE_HIGH"

#: Hạn dùng của một câu trả lời KB đã cache (giây) — hết hạn thì tra lại để bắt kịp KB vừa đổi.
CACHE_TTL_ENV = "ARTEMIS_SUBTASK_KB_TTL"
DEFAULT_CACHE_TTL = 600.0

#: Hạn dùng ngắn hơn cho câu trả lời "không có mục nào khớp" — KB có thể vừa được cập nhật.
CACHE_EMPTY_TTL_ENV = "ARTEMIS_SUBTASK_KB_EMPTY_TTL"
DEFAULT_CACHE_EMPTY_TTL = 120.0

#: Sau một lần tra **lỗi**, chờ bao lâu mới thử lại (giây). Lỗi không bao giờ được cache.
CACHE_RETRY_ENV = "ARTEMIS_SUBTASK_KB_RETRY_SECONDS"
DEFAULT_CACHE_RETRY = 30.0


def _env_flag(name: str, *, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _env_seconds(name: str, *, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def require_high_confidence() -> bool:
    """Chính sách: có bắt buộc nhận diện **chắc chắn** mới dùng luật thiết bị hay không."""
    return _env_flag(REQUIRE_HIGH_ENV, default=False)


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
    # Chỉ từ khoá **có thật trong KB** (name + aliases: "doorbell action", "HSRF sound action",
    # "chọn nhạc chuông RF") cộng tên thiết bị thường gặp "chuông cửa". Bản đầu tôi tự thêm "chuong"
    # trần và nó khớp dòng *"Chuông rung."* trên thanh trạng thái ⇒ một luật doorbell không liên quan
    # vào prompt (đo trên run thật 5b459634, 16:02:30). "chuông cửa" thì cụ thể, không mơ hồ.
    ("device_action.doorbell_hsrf", ("doorbell", "hsrf", "nhac chuong", "chuong cua")),
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

# ── Bằng chứng màn hình: bỏ system UI, chỉ nhận dòng giống một dòng thiết bị ──────────────
#: Băng trên/dưới của màn hình là **của hệ điều hành** (status bar / thanh điều hướng), tính theo
#: tỉ lệ chiều cao quan sát được. Không phải bằng chứng về app đang test.
SYSTEM_TOP_BAND = 0.06
SYSTEM_BOTTOM_BAND = 0.90

#: Chiều cao tối thiểu để tính băng system UI: danh sách phần tử của ARTEMIS dùng hệ toạ độ chuẩn
#: hoá ~1000 đơn vị, còn hierarchy thô thì theo pixel (>=1000). Lấy max để đúng cho cả hai.
NORMALIZED_SCREEN_HEIGHT = 1000

#: Số ký tự đầu dòng được phép đứng trước tên thiết bị (ô tick/icon của app).
DEVICE_ROW_LEAD = 2

#: Trần độ dài của một **nhãn thiết bị** (tên + trạng thái). Dài hơn ⇒ là câu/mô tả, không phải tên.
DEVICE_LABEL_MAX_CHARS = 40

#: Dấu hiệu dòng **đo lường** (thẻ thời tiết, cảm biến hiển thị số) — không phải tên thiết bị.
MEASUREMENT_MARKERS: tuple[str, ...] = ("%", "°", ":")

#: Dòng **chỉ báo của hệ thống** — không bao giờ là bằng chứng thiết bị, kể cả khi nằm ngoài băng.
SYSTEM_TEXT_TERMS: tuple[str, ...] = (
    "chuong rung",
    "tin hieu",
    "wi-fi",
    "wifi",
    "pin",
    "che do",
    "ngu che do",
    "am thanh",
    "do pin",
    "battery",
    "thong bao",
    "notification",
)

#: Cụm từ cho biết dòng đang thuộc **danh sách thiết bị / sheet chọn trạng thái** ⇒ bằng chứng có
#: ngữ cảnh rõ ràng, dù tên thiết bị không nằm ở đầu dòng.
LIST_CONTEXT_TERMS: tuple[str, ...] = (
    "chua xep phong",
    "danh sach thiet bi",
    "thiet bi hay dung",
    "chon thiet bi",
    "chon hanh dong",
    "device list",
)

#: Một dòng trong danh sách phần tử: ``[12] Text: '...' | Bounds: [63,265][341,389] (WARNING: ...)``.
_BOUNDS_RE = re.compile(r"Bounds:\s*\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_QUOTED_RE = re.compile(r"'([^']*)'")


@dataclass(frozen=True)
class DeviceTypeMatch:
    """Loại thiết bị nhận ra được, kèm **bằng chứng**, **độ chắc chắn** và **mâu thuẫn** (nếu có).

    ``confidence`` chỉ có hai mức, và mức nào cũng **có thật**:

    * ``"high"`` — màn hình và công đoạn **cùng nói một loại**, hoặc chỉ màn hình nói và nói đúng
      một loại (màn hình là bằng chứng thật của bước đang làm);
    * ``"medium"`` — chỉ **một phía** nói: công đoạn nói mà màn hình không nhắc gì, hoặc màn hình
      nói một loại còn công đoạn nói loại khác (``conflict`` giữ loại của công đoạn).

    Không có mức ``"low"``: không đủ căn cứ thì hàm nhận diện trả ``None`` (im lặng) chứ không trả
    một phỏng đoán.
    """

    workflow_id: str
    term: str
    source: str  # "ui+subtask" | "ui" | "subtask"
    confidence: str = "high"  # "high" | "medium"
    conflict: str = ""  # loại mà phía còn lại nói tới khi hai phía mâu thuẫn

    @property
    def from_screen(self) -> bool:
        """Bằng chứng có đến từ màn hình thật hay không."""
        return self.source.startswith("ui")

    def evidence(self) -> str:
        """Câu mô tả bằng chứng để đưa vào prompt (nói rõ vì sao tin loại này)."""
        where = {
            "ui+subtask": "khớp cả màn hình và công đoạn",
            "ui": "theo màn hình",
            "subtask": "theo công đoạn",
        }.get(self.source, self.source)
        text = f"{self.workflow_id} ({self.term}, {where}"
        if self.confidence != "high":
            text += ", chưa chắc chắn"
        text += ")"
        if self.conflict:
            text += f" ⚠ công đoạn nói {self.conflict} — luật dưới đây theo màn hình"
        return text

    def as_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "term": self.term,
            "source": self.source,
            "confidence": self.confidence,
            "conflict": self.conflict,
        }


@dataclass
class _CacheEntry:
    """Một câu trả lời KB đã cache, kèm **hạn dùng**."""

    text: str
    expires_at: float


@dataclass
class SubtaskKbCache:
    """Cache theo ``(subtask_id, device_type)`` — một công đoạn chỉ tra KB **một lần** mỗi hạn dùng.

    Ba tính chất được chọn có chủ đích:

    * **chỉ cache câu trả lời**, không bao giờ cache **lỗi**: lỗi tạm thời (mạng, KB đang khởi
      động) mà bị cache thì công đoạn đó mất KB tới hết run — đúng thứ cần tránh;
    * lỗi chỉ ghi lại **thời điểm** ⇒ :meth:`retry_allowed` chặn dội liên tục trong
      ``retry_seconds`` rồi **cho thử lại**;
    * câu trả lời "không có mục nào khớp" có hạn dùng **ngắn hơn** nội dung thật (KB có thể vừa
      được cập nhật), và :meth:`invalidate` cho phép làm mới ngay khi cần.
    """

    content_ttl: float = DEFAULT_CACHE_TTL
    empty_ttl: float = DEFAULT_CACHE_EMPTY_TTL
    retry_seconds: float = DEFAULT_CACHE_RETRY
    entries: dict[tuple[str, str], _CacheEntry] = field(default_factory=dict)
    failures: dict[tuple[str, str], float] = field(default_factory=dict)
    lookups: int = 0
    hits: int = 0
    misses: int = 0
    expired: int = 0
    errors: int = 0
    suppressed: int = 0

    @classmethod
    def from_env(cls) -> SubtaskKbCache:
        """Cấu hình đọc từ biến môi trường (một chỗ duy nhất, dễ chỉnh khi vận hành)."""
        return cls(
            content_ttl=_env_seconds(CACHE_TTL_ENV, default=DEFAULT_CACHE_TTL),
            empty_ttl=_env_seconds(CACHE_EMPTY_TTL_ENV, default=DEFAULT_CACHE_EMPTY_TTL),
            retry_seconds=_env_seconds(CACHE_RETRY_ENV, default=DEFAULT_CACHE_RETRY),
        )

    def get(self, key: tuple[str, str], *, now: float | None = None) -> str | None:
        """Câu trả lời còn hạn, hoặc ``None`` (đồng thời dọn mục đã hết hạn)."""
        entry = self.entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_at <= (time.monotonic() if now is None else now):
            del self.entries[key]
            self.expired += 1
            self.misses += 1
            return None
        self.hits += 1
        return entry.text

    def put(
        self,
        key: tuple[str, str],
        value: str,
        *,
        now: float | None = None,
        ttl: float | None = None,
    ) -> None:
        """Ghi một **câu trả lời** (kể cả rỗng) với hạn dùng; xoá dấu vết lỗi của khoá đó."""
        now = time.monotonic() if now is None else now
        if ttl is None:
            ttl = self.content_ttl if value else self.empty_ttl
        self.entries[key] = _CacheEntry(value, now + ttl)
        self.failures.pop(key, None)

    def note_failure(self, key: tuple[str, str], *, now: float | None = None) -> None:
        """Ghi **thời điểm** một lần tra lỗi — không ghi nội dung lỗi vào cache."""
        self.failures[key] = time.monotonic() if now is None else now
        self.errors += 1

    def retry_allowed(self, key: tuple[str, str], *, now: float | None = None) -> bool:
        """Có được thử tra lại khoá này không (tính từ lần lỗi gần nhất)."""
        stamp = self.failures.get(key)
        if stamp is None:
            return True
        now = time.monotonic() if now is None else now
        if now - stamp >= self.retry_seconds:
            del self.failures[key]
            return True
        return False

    def invalidate(self, key: tuple[str, str] | None = None) -> int:
        """Làm mới KB: xoá một khoá (hoặc toàn bộ) và xoá cả dấu vết lỗi. Trả số khoá đã xoá."""
        if key is None:
            removed = len(self.entries) + len(self.failures)
            self.entries.clear()
            self.failures.clear()
            return removed
        removed = int(key in self.entries) + int(key in self.failures)
        self.entries.pop(key, None)
        self.failures.pop(key, None)
        return removed


def get_cache(ctx: object) -> SubtaskKbCache:
    """Cache sống theo **run**: ``ctx`` là đối tượng của một lần chạy, không phải toàn cục."""
    cache = getattr(ctx, "subtask_kb_cache", None)
    if not isinstance(cache, SubtaskKbCache):
        cache = SubtaskKbCache.from_env()
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


def _matches_in_text(text: str) -> tuple[DeviceTypeMatch, ...]:
    """**Mọi** loại thiết bị xuất hiện trong ``text``, theo vị trí, mỗi loại một lần.

    Vì sao cần *tất cả* chứ không chỉ loại đầu tiên: quyết định "đủ chắc chắn hay không" phụ thuộc
    số loại cùng xuất hiện (màn hình có 2 loại mà công đoạn không nói gì ⇒ không biết mục tiêu).

    Vị trí chứ không phải thứ tự bảng: màn danh sách có thể chứa nhiều thiết bị ("Công tắc 10 · Ổ cắm
    12 · Rèm phòng khách"); duyệt theo thứ tự bảng thì thiết bị nào đứng trước trong bảng sẽ thắng,
    bất kể màn hình đang nói tới cái nào.
    """
    haystack = normalize(text or "")
    if not haystack:
        return ()
    found: list[tuple[int, DeviceTypeMatch]] = []
    for workflow_id, terms in DEVICE_TYPE_TERMS:
        best: tuple[int, str] | None = None
        for term in terms:
            # "cửa" trong "cửa hàng" không phải thiết bị cửa.
            if term == "cua" and any(store in haystack for store in _STORE_TERMS):
                continue
            hit = _term_pattern(term).search(haystack)
            if hit and (best is None or hit.start() < best[0]):
                best = (hit.start(), term)
        if best is not None:
            found.append((best[0], DeviceTypeMatch(workflow_id, best[1], "")))
    found.sort(key=lambda row: row[0])
    return _prefer_specific(found)


def _prefer_specific(
    rows: list[tuple[int, DeviceTypeMatch]],
) -> tuple[DeviceTypeMatch, ...]:
    """Bỏ loại khớp bằng từ khoá **nằm trong** từ khoá của loại khác trên cùng một đoạn.

    Vì sao: "Chuông cửa" vừa khớp ``device_action.doorbell_hsrf`` (từ khoá ``chuong cua``) vừa khớp
    ``device_action.sdoor`` (từ khoá ``cua``) — hai loại trên cùng một tên thiết bị. Từ khoá dài hơn
    là bằng chứng cụ thể hơn, nên loại ngắn hơn (chung hơn) bị bỏ khỏi đoạn đó.
    """
    kept: list[tuple[int, DeviceTypeMatch]] = []
    for position, match in rows:
        contained = any(
            other.term != match.term and match.term in other.term
            for _pos, other in rows
            if other.workflow_id != match.workflow_id
        )
        if not contained:
            kept.append((position, match))
    return tuple(match for _position, match in kept)


def _first_match_in_text(text: str) -> DeviceTypeMatch | None:
    """Loại thiết bị xuất hiện **sớm nhất** trong ``text`` (``None`` nếu không có)."""
    matches = _matches_in_text(text)
    return matches[0] if matches else None


def _element_text(line: str) -> str:
    """Phần chữ của một dòng trong danh sách phần tử (bỏ chỉ số, nhãn, toạ độ, cảnh báo)."""
    quoted = _QUOTED_RE.search(line)
    return (quoted.group(1) if quoted else line).strip()


def _is_system_text(text: str) -> bool:
    """Dòng chỉ báo của hệ thống (giờ, pin, Wi-Fi, chế độ chuông…) — không phải nội dung app."""
    haystack = normalize(text)
    if re.fullmatch(r"\d{1,2}:\d{2}", haystack):
        return True
    return any(_matches(haystack, term) for term in SYSTEM_TEXT_TERMS)


def _ui_evidence_lines(observed_text: str) -> list[str]:
    """Chữ của các dòng **không thuộc system UI** trong danh sách phần tử màn hình.

    Vì sao cần: run thật 29/10/2026 (task ``5b459634``) có dòng
    ``[3] Text: 'Chuông rung.' | Bounds: [738,18][777,34]`` trên **thanh trạng thái**, bị khớp thành
    thiết bị *doorbell* ⇒ 244 ms tra KB + 1.238 ký tự luật không liên quan vào prompt của công đoạn
    đầu. Status bar / thanh điều hướng là của hệ điều hành, không phải của app đang test.

    Băng trên/dưới tính theo **chiều cao quan sát được** (``max(y2)``) nên không phụ thuộc độ phân
    giải. Dòng không có ``Bounds`` vẫn được giữ (không đủ dữ kiện để loại).
    """
    lines = [ln for ln in (observed_text or "").splitlines() if ln.strip()]
    parsed: list[tuple[str, tuple[int, int, int, int] | None]] = []
    heights: list[int] = []
    for line in lines:
        found = _BOUNDS_RE.search(line)
        bounds = tuple(int(v) for v in found.groups()) if found else None
        if bounds is not None:
            heights.append(bounds[3])
        parsed.append((line, bounds))
    height = max(heights) if heights else 0
    height = max(height, NORMALIZED_SCREEN_HEIGHT) if heights else 0

    kept: list[str] = []
    for line, bounds in parsed:
        text = _element_text(line)
        if not text or _is_system_text(text):
            continue
        if bounds is not None and height:
            _x1, y1, _x2, y2 = bounds
            if y2 <= SYSTEM_TOP_BAND * height or y1 >= SYSTEM_BOTTOM_BAND * height:
                continue
        kept.append(text)
    return kept


def _is_device_row(text: str, match: DeviceTypeMatch) -> bool:
    """Dòng có đủ tư cách là **một dòng thiết bị** trong danh sách/màn hình.

    Ba đường, theo thứ tự chặt dần:

    1. dòng **bắt đầu** bằng tên thiết bị (cho phép tối đa :data:`DEVICE_ROW_LEAD` ký tự trang trí
       như ô tick/icon của app) — "Công tắc 10", "Ổ cắm 12 , Đang bật";
    2. dòng là **nhãn ngắn** (≤ :data:`DEVICE_LABEL_MAX_CHARS`) và **không** phải dòng đo lường
       (không có ``%``/``°``/``:``) — "Cảm biến nhiệt độ" (tên thiết bị không bắt đầu bằng từ khoá loại);
    3. dòng có ngữ cảnh danh sách/sheet rõ ràng (:func:`_has_list_context`).

    Loại trừ là mục đích chính: thẻ thời tiết ``28°C , Độ ẩm: 55% , Nhiều mây`` rơi vào dòng đo lường
    nên không bị coi là cảm biến, dù nó chứa "độ ẩm".
    """
    haystack = normalize(text)
    found = _term_pattern(match.term).search(haystack)
    if found is None:
        return False
    lead = len(haystack) - len(re.sub(r"^[^\w]+", "", haystack, count=1))
    if found.start() - lead <= DEVICE_ROW_LEAD:
        return True
    if any(marker in text for marker in MEASUREMENT_MARKERS):
        return False
    if len(text.strip()) <= DEVICE_LABEL_MAX_CHARS:
        return True
    return _has_list_context(text)


def _has_list_context(text: str) -> bool:
    """Dòng thuộc danh sách thiết bị / sheet chọn trạng thái ⇒ bằng chứng có ngữ cảnh rõ ràng."""
    haystack = normalize(text)
    terms = (*PICKER_TERMS, *SHEET_TERMS, *LIST_CONTEXT_TERMS)
    return any(_matches(haystack, term) for term in terms)


def _ui_evidence_matches(observed_text: str) -> tuple[DeviceTypeMatch, ...]:
    """Loại thiết bị nhận được từ **màn hình**, chỉ từ dòng đủ tư cách làm bằng chứng.

    Một dòng được nhận khi thoả **cả hai**:

    * chỉ chứa **một** loại thiết bị (dòng trộn nhiều tên thiết bị không phải một dòng thiết bị);
    * hoặc **bắt đầu** bằng tên thiết bị (dòng trong danh sách chọn), hoặc có ngữ cảnh danh sách/
      sheet rõ ràng.

    Không dòng nào đủ tư cách ⇒ rỗng ⇒ :func:`detect_device_type` trả ``None`` (im lặng), giữ đúng
    nguyên tắc "thà thiếu hướng dẫn còn hơn dạy sai luật thiết bị".
    """
    accepted: list[DeviceTypeMatch] = []
    for text in _ui_evidence_lines(observed_text):
        matches = _matches_in_text(text)
        if len({m.workflow_id for m in matches}) != 1:
            continue
        match = matches[0]
        if _is_device_row(text, match) or _has_list_context(text):
            accepted.append(match)
    seen: set[str] = set()
    ordered: list[DeviceTypeMatch] = []
    for match in accepted:
        if match.workflow_id not in seen:
            seen.add(match.workflow_id)
            ordered.append(match)
    return tuple(ordered)


def _confident_enough(match: DeviceTypeMatch, require_high: bool | None = None) -> bool:
    """Có được dùng luật của loại này không, theo chính sách "đủ chắc chắn"."""
    strict = require_high_confidence() if require_high is None else require_high
    return match.confidence == "high" or not strict


def detect_device_type(subtask_text: str, observed_text: str) -> DeviceTypeMatch | None:
    """Loại thiết bị **mục tiêu** của bước này — màn hình là bằng chứng, công đoạn là ý định.

    Bằng chứng màn hình chỉ lấy từ :func:`_ui_evidence_matches`: đã bỏ status bar / thanh điều hướng /
    dòng chỉ báo hệ thống, và chỉ nhận dòng giống **một dòng thiết bị** (xem hàm đó).

    Bảng quyết định (đo trên màn danh sách thật "Công tắc 10 · Ổ cắm 12 · Rèm phòng khách"):

    ==========================================  ==================  ====================
    Bằng chứng                                  Kết quả             ``confidence``
    ==========================================  ==================  ====================
    Màn hình và công đoạn **cùng một loại**     loại đó             ``high``
    Màn hình đúng **một loại**, công đoạn im    loại trên màn hình  ``high``
    Màn hình một loại, công đoạn nói loại khác  **loại trên màn hình** (``conflict``)  ``medium``
    Màn hình nhiều loại, công đoạn không khớp   ``None`` (im lặng)  —
    Chỉ công đoạn nói, đúng một loại            loại của công đoạn  ``medium``
    Không phía nào nói                          ``None`` (im lặng)  —
    ==========================================  ==================  ====================

    Vì sao màn hình thắng khi hai phía mâu thuẫn: màn hình là cái **đang thật sự hiện ra** cho agent,
    còn công đoạn là kế hoạch viết trước. Nhưng mâu thuẫn vẫn được ghi lại (``conflict``) và đưa vào
    prompt, để agent thấy ngay là hai nguồn không khớp nhau thay vì im lặng dạy sai.

    Vì sao **không** chọn khi màn hình có nhiều loại: "tên loại trùng với một item khác" chính là lỗi
    đã đo được (bản đầu chọn ``rèm`` chỉ vì nó đứng trước trong bảng từ khoá). Thà thiếu hướng dẫn.
    """
    ui = _ui_evidence_matches(observed_text)
    plan = _matches_in_text(subtask_text)
    ui_ids = {match.workflow_id for match in ui}
    plan_ids = {match.workflow_id for match in plan}
    common = ui_ids & plan_ids
    if len(common) == 1:
        chosen = next(match for match in ui if match.workflow_id in common)
        return DeviceTypeMatch(chosen.workflow_id, chosen.term, "ui+subtask", "high")
    if len(ui_ids) == 1:
        only = ui[0]
        conflict = ", ".join(sorted(plan_ids - ui_ids))
        return DeviceTypeMatch(
            only.workflow_id, only.term, "ui", "medium" if conflict else "high", conflict
        )
    if ui_ids:
        # Nhiều loại cùng trên màn hình và công đoạn không xác nhận loại nào ⇒ không đoán.
        return None
    if len(plan_ids) == 1:
        return DeviceTypeMatch(plan[0].workflow_id, plan[0].term, "subtask", "medium")
    return None


def subtask_kb_ids(
    subtask_text: str,
    observed_text: str,
    *,
    require_high: bool | None = None,
) -> tuple[str, ...]:
    """Id cần tra cho công đoạn này — nhiều nhất :data:`MAX_ENTRIES`, mục cụ thể đứng trước.

    * Bước chọn thiết bị/hành động ⇒ ``device_action.selector``.
    * Nhận ra loại thiết bị **đủ chắc chắn** ⇒ ``device_action.<type>`` (hoặc biến thể
      ``.bottomsheet.`` nếu màn hình đang hiện sheet chọn trạng thái).
    * Không có bằng chứng nào (hoặc bằng chứng chỉ ở mức ``medium`` khi bật
      :data:`REQUIRE_HIGH_ENV`) ⇒ chỉ còn luật chung của bộ chọn, hoặc rỗng nếu cũng không có.
    """
    ids: list[str] = []
    match = detect_device_type(subtask_text, observed_text)
    if match is not None and _confident_enough(match, require_high):
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
    force_refresh: bool = False,
    now: float | None = None,
) -> str:
    """Khối KB cho **công đoạn đang làm**, hoặc ``""`` khi không có gì chắc chắn để đưa.

    Một công đoạn ⇒ một lần tra cho mỗi hạn dùng của cache (khoá ``subtask_id + device_type``). Tra
    theo **id** nên không tốn embedding, và chỉ 1–2 mục vào prompt thay vì cả 26.

    Xử lý lỗi tạm thời: câu trả lời **lỗi** không bao giờ vào cache. Lần lỗi đầu được nói cho agent
    biết (một câu ngắn), các lượt sau trong ``cache.retry_seconds`` **im lặng** để không lặp lại cùng
    một lỗi mỗi lượt; hết khoảng đó thì tra lại. ``force_refresh`` (hoặc ``cache.invalidate``) dùng
    khi cần làm mới KB ngay.
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
    confident = match if (match is not None and _confident_enough(match)) else None
    device_key = confident.workflow_id if confident else "-"
    key = (str(getattr(active, "id", "") or subtask_text), device_key)
    if cache is not None and not force_refresh:
        cached = cache.get(key, now=now)
        if cached is not None:
            return cached
        if not cache.retry_allowed(key, now=now):
            # Vừa lỗi cách đây ít lâu: không dội lại mỗi lượt, cũng không lặp lại câu lỗi.
            cache.suppressed += 1
            return ""

    fetcher = lookup or _default_lookup
    answers: list[str] = []
    failures: list[str] = []
    for workflow_id in ids:
        text = fetcher(
            workflow_id=workflow_id,
            app_package=app_package,
            run_id=run_id,
            max_results=1,
        )
        if not text:
            continue
        if is_tool_failure(text):
            failures.append(str(text).strip())
        else:
            answers.append(str(text).strip())
    if cache is not None:
        cache.lookups += 1

    if not answers:
        if cache is not None:
            if failures:
                cache.note_failure(key, now=now)
            else:
                cache.put(key, "", now=now)
        # Lỗi hạ tầng: nói **một lần** rồi thôi (các lượt sau trong khoảng backoff trả "" ở trên).
        return failures[0] if failures else ""

    evidence = (
        f"thiết bị: {confident.evidence()}" if confident else "chưa xác định được loại thiết bị"
    )
    header = (
        "[KB THEO CÔNG ĐOẠN] "
        f"công đoạn «{subtask_text[:90]}» · {evidence} · id tra: {', '.join(ids)}"
    )
    block = header + "\n" + "\n".join(answers)
    if len(block) > max_chars:
        block = block[:max_chars].rstrip() + "\n…(đã cắt bớt)"
    if cache is not None:
        # Tra được một phần (còn id lỗi) ⇒ hạn dùng ngắn để sớm bù phần thiếu, không giữ nửa vời
        # suốt công đoạn.
        cache.put(key, block, now=now, ttl=cache.empty_ttl if failures else None)
    return block


__all__ = [
    "CACHE_EMPTY_TTL_ENV",
    "CACHE_RETRY_ENV",
    "CACHE_TTL_ENV",
    "DEFAULT_CACHE_EMPTY_TTL",
    "DEFAULT_CACHE_RETRY",
    "DEFAULT_CACHE_TTL",
    "DEFAULT_MAX_CHARS",
    "DEVICE_TYPE_TERMS",
    "MAX_ENTRIES",
    "PICKER_TERMS",
    "REQUIRE_HIGH_ENV",
    "SELECTOR_ID",
    "SHEET_TERMS",
    "SHEET_VARIANTS",
    "DeviceTypeMatch",
    "SubtaskKbCache",
    "detect_device_type",
    "detect_picker_step",
    "get_cache",
    "require_high_confidence",
    "subtask_kb_block",
    "subtask_kb_ids",
]
