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

"""Bộ lọc metadata cho truy hồi knowledge base.

**Vì sao cần một module riêng.** Truy hồi cũ chỉ có *một* đường: đưa câu tester vào, lấy vài
workflow gần nhất. Đo trên máy thật 29/10/2026: agent gặp modal ngoài dự kiến thì **không có cách
nào** hỏi KB "modal này là gì" — câu hỏi của nó không phải câu tiếng Việt mô tả công việc, mà là
truy vấn có cấu trúc ("màn hình `home`, loại `modal`"). Nhồi thêm dữ liệu vào cùng một đường đó
chỉ làm prompt dài thêm; muốn lấy **đúng** mục thì phải lọc theo id, namespace, loại UI, màn hình
và tag **trước** khi xếp hạng ngữ nghĩa.

Phần ở đây là phần *thuần*: chuẩn hoá và kiểm tra đầu vào. Câu SQL nằm ở store, chính sách kênh nằm
ở service — cùng cách chia đã dùng cho :mod:`artemis.knowledge.retrieval`, để luật lọc test được
mà không cần cơ sở dữ liệu.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, fields

#: Trần số phần tử mỗi trường lọc. Truy vấn do LLM sinh ra có thể kèm hàng trăm id; danh sách dài
#: không làm kết quả tốt hơn mà chỉ làm câu SQL phình ra, nên cắt ở đây thay vì ở SQL.
MAX_FILTER_ITEMS = 32

#: Trạng thái hợp lệ theo schema (``kb_workflow.status``).
KNOWN_STATUSES = frozenset({"draft", "verified", "deprecated"})


def _as_tuple(value: object, *, limit: int = MAX_FILTER_ITEMS) -> tuple[str, ...]:
    """Chuẩn hoá một trường lọc: chấp nhận chuỗi, danh sách, hoặc bỏ trống.

    Nhận cả ``str`` vì nguồn gọi là LLM và tham số HTTP — nơi ``namespace="modal"`` là cách viết
    tự nhiên, còn ``namespace=["modal"]`` là cách viết của code.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raw: Iterable[object] = [value]
    elif isinstance(value, Iterable):
        raw = value
    else:
        raw = [value]
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip().lower()
        if not text:
            continue
        if text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return tuple(out)


def _as_namespace(value: object) -> tuple[str, ...]:
    """Namespace bỏ dấu chấm cuối: ``"navigation."`` và ``"navigation"`` là một."""
    return tuple(item.rstrip(".") for item in   _as_tuple(value))


@dataclass(frozen=True)
class KnowledgeFilter:
    """Điều kiện lọc metadata của một lần truy hồi.

    Mọi trường **giao nhau** (AND): ``namespace="navigation"`` + ``type="navigation"`` nghĩa là
    "workflow điều hướng". Trường rỗng nghĩa là không ràng buộc — nên ``KnowledgeFilter()`` (rỗng)
    tương đương hành vi cũ, đó là cách giữ tương thích ngược cho mọi đường gọi hiện có.
    """

    ids: tuple[str, ...] = ()
    namespaces: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    screens: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    #: ``True`` (mặc định) giữ luật cũ: bản ``deprecated`` không bao giờ được phục vụ.
    exclude_deprecated: bool = True
    #: Ghi chú tự do từ nơi gọi (ví dụ ``"operator kb_lookup"``) để lại dấu trong log truy hồi.
    origin: str = ""

    def __post_init__(self) -> None:
        # Chuẩn hoá ngay khi khởi tạo: mọi đường vào (HTTP, tool, test) đều đi qua cùng một luật,
        # và không có hai biểu diễn khác nhau của cùng một bộ lọc.
        object.__setattr__(self, "ids", _as_tuple(self.ids))
        object.__setattr__(self, "namespaces", _as_namespace(self.namespaces))
        object.__setattr__(self, "types", _as_tuple(self.types))
        object.__setattr__(self, "tags", _as_tuple(self.tags))
        object.__setattr__(self, "screens", _as_tuple(self.screens))
        object.__setattr__(self, "statuses", _as_tuple(self.statuses))
        object.__setattr__(self, "origin", str(self.origin or "").strip())

    # ── Câu hỏi về chính bộ lọc ───────────────────────────────────────────
    @property
    def is_empty(self) -> bool:
        """Không có ràng buộc nào ngoài luật mặc định về ``deprecated``."""
        return not any(
            getattr(self, item.name)
            for item in fields(self)
            if item.name not in {"exclude_deprecated", "origin"}
        )

    @property
    def needs_query(self) -> bool:
        """Bộ lọc rỗng: chưa biết cần gì nên vẫn phải có câu truy vấn."""
        return self.is_empty

    def namespace_patterns(self) -> tuple[str, ...]:
        """Mẫu ``LIKE`` cho từng namespace (``navigation`` → ``navigation.%``)."""
        return tuple(f"{namespace}.%" for namespace in self.namespaces)

    def describe(self) -> str:
        """Mô tả ngắn để ghi log và cho agent biết nó đã lọc gì."""
        parts = [
            f"{item.name}={','.join(getattr(self, item.name))}"
            for item in fields(self)
            if item.name not in {"exclude_deprecated", "origin"} and getattr(self, item.name)
        ]
        if not self.exclude_deprecated:
            parts.append("include_deprecated")
        return " ".join(parts)

    def as_dict(self) -> dict[str, object]:
        """Dạng ghi vào log truy hồi (chỉ trường có giá trị, để log không đầy khoá rỗng)."""
        payload: dict[str, object] = {
            item.name: list(getattr(self, item.name))
            for item in fields(self)
            if item.name not in {"exclude_deprecated", "origin"} and getattr(self, item.name)
        }
        if not self.exclude_deprecated:
            payload["exclude_deprecated"] = False
        if self.origin:
            payload["origin"] = self.origin
        return payload

    def as_request_dict(self) -> dict[str, object]:
        """Dạng gửi qua HTTP/tool: dùng **tên số ít** mà người gọi quen viết.

        Log thì dùng tên trường của dataclass (``as_dict``), còn dây thì dùng ``namespace``/``type``/
        ``screen``/``tags``/``ids`` — đúng những tên endpoint và schema tool công bố.
        """
        singular = {
            "ids": "ids",
            "namespaces": "namespace",
            "types": "type",
            "tags": "tags",
            "screens": "screen",
            "statuses": "status",
        }
        payload: dict[str, object] = {}
        for field_name, wire_name in singular.items():
            values = getattr(self, field_name)
            if values:
                payload[wire_name] = list(values)
        if not self.exclude_deprecated:
            payload["exclude_deprecated"] = False
        return payload

    def with_origin(self, origin: str) -> KnowledgeFilter:
        """Bản sao có ghi nguồn gọi — dùng khi đi qua nhiều tầng (HTTP → service → store)."""
        return KnowledgeFilter(
            ids=self.ids,
            namespaces=self.namespaces,
            types=self.types,
            tags=self.tags,
            screens=self.screens,
            statuses=self.statuses,
            exclude_deprecated=self.exclude_deprecated,
            origin=origin or self.origin,
        )


#: Tên gọi số ít ↔ trường của dataclass. Người dùng HTTP và LLM luôn viết ``namespace``,
#: ``screen``, ``type``; dataclass dùng số nhiều vì chúng là danh sách. Chấp nhận cả hai để
#: không có chuyện "lọc mà không lọc" vì lệch một chữ ``s``.
FIELD_ALIASES: dict[str, str] = {
    "namespace": "namespaces",
    "type": "types",
    "tag": "tags",
    "screen": "screens",
    "status": "statuses",
    "id": "ids",
    "workflow_id": "ids",
    "workflow_ids": "ids",
}


def filter_from_payload(payload: dict | None, *, origin: str = "") -> KnowledgeFilter:
    """Dựng bộ lọc từ payload thô (HTTP body / tham số tool), bỏ qua khoá lạ.

    Bỏ qua khoá lạ thay vì báo lỗi là chủ ý: đây là đường *tùy chọn* — một tham số sai chính tả
    không nên làm hỏng cả lần truy hồi, chỉ nên làm bộ lọc rộng hơn.
    """
    data = payload or {}
    allowed = {item.name for item in fields(KnowledgeFilter)} - {"origin"}
    kwargs: dict[str, object] = {}
    for key, value in data.items():
        name = key if key in allowed else FIELD_ALIASES.get(key, "")
        if not name or value is None:
            continue
        if name in kwargs:
            # Hai cách viết cùng chỉ một trường (``id`` và ``ids``): gộp lại, không ghi đè.
            kwargs[name] = [*_as_tuple(kwargs[name]), *_as_tuple(value)]
            continue
        kwargs[name] = value
    return KnowledgeFilter(**kwargs, origin=origin)


def parse_workflow_ids(value: object) -> tuple[str, ...]:
    """Tách danh sách id workflow từ chuỗi ngăn bằng dấu phẩy/khoảng trắng (đầu vào của LLM)."""
    if value is None:
        return ()
    if isinstance(value, str):
        chunks = [piece for piece in value.replace(",", " ").split() if piece.strip()]
        return _as_tuple(chunks)
    return _as_tuple(value)
