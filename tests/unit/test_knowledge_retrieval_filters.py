"""Truy hồi KB có bộ lọc: chuẩn hoá bộ lọc, chính sách kênh, và hành vi tương thích ngược.

Vì sao test riêng cho phần này: đây là đường mà agent dùng để hỏi KB **giữa lúc chạy** khi gặp UI
ngoài dự kiến. Hai luật phải không thể vi phạm do vô ý:

* bộ lọc rỗng ⇒ hành vi **y hệt** trước đây (không có chuyện thêm filter làm đổi kết quả cũ);
* bộ lọc khớp ⇒ mục trả về là **tin cậy** (người gọi đã nói chính xác nó cần gì), không bị
  :func:`decide_found` từ chối như một phỏng đoán ngữ nghĩa.
"""

import pytest

from artemis.knowledge.filters import (
    MAX_FILTER_ITEMS,
    KnowledgeFilter,
    filter_from_payload,
    parse_workflow_ids,
)
from artemis.knowledge.models import Workflow, WorkflowStep
from artemis.knowledge.service import KnowledgeService


def _workflow(workflow_id: str = "modal.home.switch_house", **kwargs) -> Workflow:
    payload = {
        "id": workflow_id,
        "app_id": "hunonic",
        "name": "Modal chuyển nhà",
        "status": "draft",
        "metadata": {"type": "modal", "screen": "home", "confidence": 0.5},
        "steps": [],
    }
    payload.update(kwargs)
    return Workflow(**payload)


class FakeStore:
    """Store tối thiểu, ghi lại tham số để chứng minh bộ lọc tới được **mọi** kênh."""

    def __init__(self, workflows=(), *, filtered=(), exact=(), keyword=(), vector=()):
        self.workflows = {workflow.id: workflow for workflow in workflows}
        self.filtered = list(filtered)
        self.exact = list(exact)
        self.keyword = list(keyword)
        self.vector = list(vector)
        self.seen: list[tuple[str, object]] = []

    def search_filtered(self, filters, app_id, version_id, limit=50):
        self.seen.append(("filtered", filters))
        return list(self.filtered)

    def search_exact(self, query, app_id, version_id, limit=50, filters=None):
        self.seen.append(("exact", filters))
        return list(self.exact)

    def search_keyword(self, query, app_id, version_id, limit=50, filters=None):
        self.seen.append(("keyword", filters))
        return list(self.keyword)

    def search_vector(self, vector, model, app_id, version_id, limit=50, filters=None):
        self.seen.append(("vector", filters))
        return list(self.vector)

    def get_workflow(self, workflow_id):
        return self.workflows.get(workflow_id)

    def log_retrieval(self, entry):
        self.logged = entry

    def load_embeddings(self, model, app_id=None, version_id=None):
        return {}

    def save_embedding(self, workflow_id, model, content_hash_, vector):
        return None


class FakeEmbedder:
    model = "fake-embedding"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def embed_query(self, text: str):
        self.calls += 1
        return [1.0, 0.0]


def _service(store, embedder=None, **kwargs) -> KnowledgeService:
    return KnowledgeService(store=store, embedder=embedder or FakeEmbedder(), **kwargs)


# ── chuẩn hoá bộ lọc ─────────────────────────────────────────────────────
def test_namespace_is_normalised_so_trailing_dot_does_not_change_meaning():
    assert KnowledgeFilter(namespaces="Navigation.").namespaces == ("navigation",)
    assert KnowledgeFilter(namespaces="navigation").namespace_patterns() == ("navigation.%",)


def test_singular_aliases_from_llm_and_http_are_accepted():
    """LLM viết ``namespace``/``screen`` (số ít); dataclass dùng số nhiều — phải hiểu cả hai."""
    filters = filter_from_payload(
        {"namespace": "modal.home", "type": "modal", "screen": "home", "tag": "nav-graph"}
    )
    assert filters.namespaces == ("modal.home",)
    assert filters.types == ("modal",)
    assert filters.screens == ("home",)
    assert filters.tags == ("nav-graph",)


def test_unknown_payload_keys_are_ignored_instead_of_breaking_the_lookup():
    filters = filter_from_payload({"namespace": "auth", "bogus": "x", "confidance": 0.9})
    assert filters.namespaces == ("auth",)
    assert not filters.types


def test_two_spellings_of_ids_merge_rather_than_overwrite():
    filters = filter_from_payload({"workflow_id": "a.b", "ids": ["c.d"]})
    assert filters.ids == ("a.b", "c.d")


def test_comma_separated_ids_from_a_model_are_split():
    assert parse_workflow_ids("auth.logout, home.switch_house") == (
        "auth.logout",
        "home.switch_house",
    )
    assert parse_workflow_ids("auth.logout home.switch_house") == (
        "auth.logout",
        "home.switch_house",
    )


def test_filter_item_count_is_capped():
    filters = KnowledgeFilter(ids=[f"screen.s{i}" for i in range(MAX_FILTER_ITEMS + 20)])
    assert len(filters.ids) == MAX_FILTER_ITEMS


def test_empty_filter_is_the_old_behaviour():
    empty = KnowledgeFilter()
    assert empty.is_empty
    assert empty.describe() == ""
    assert empty.as_dict() == {}


def test_describe_and_as_dict_report_what_was_asked():
    filters = KnowledgeFilter(namespaces="navigation", types="navigation", origin="test")
    assert filters.describe() == "namespaces=navigation types=navigation"
    assert filters.as_dict() == {
        "namespaces": ["navigation"],
        "types": ["navigation"],
        "origin": "test",
    }


# ── chính sách kênh ─────────────────────────────────────────────────────
def test_filter_only_lookup_needs_no_query_and_is_trusted():
    """Câu hỏi có cấu trúc: không cần câu văn nào, và kết quả phải là tin cậy."""
    workflow = _workflow()
    store = FakeStore([workflow], filtered=[workflow.id])
    embedder = FakeEmbedder()

    result = _service(store, embedder).retrieve(
        "", "hunonic", filters=KnowledgeFilter(screens="home", types="modal")
    )

    assert result.found is True
    assert [hit.workflow.id for hit in result.hits] == [workflow.id]
    assert result.hits[0].trusted is True
    assert result.hits[0].semantic_only is False
    assert result.filters["screens"] == ["home"]
    # Không có câu truy vấn thì không gọi embedding: tra cứu theo bộ lọc phải miễn phí.
    assert embedder.calls == 0


def test_filters_reach_every_channel_not_just_the_filtered_one():
    """Lọc trước, xếp hạng sau: nếu chỉ kênh ``filtered`` được lọc thì mục ngoài phạm vi lọt vào."""
    filters = KnowledgeFilter(namespaces="navigation")
    store = FakeStore([_workflow()], exact=["a.b"], keyword=[("a.b", 1.0)], vector=[("a.b", 0.9)])

    _service(store).retrieve("mở màn cài đặt", "hunonic", filters=filters)

    channels = [name for name, _ in store.seen]
    assert {"filtered", "exact", "keyword", "vector"} <= set(channels)
    assert all(item is filters for name, item in store.seen if name != "filtered")


def test_no_query_and_no_filter_asks_for_one_of_them():
    store = FakeStore([_workflow()])
    result = _service(store).retrieve("", "hunonic")

    assert result.found is False
    assert "bộ lọc" in result.reason
    assert not store.seen  # không chạm vào store khi chưa biết cần gì


def test_a_filter_that_matches_nothing_says_so():
    """Hai nguyên nhân khác nhau phải có hai câu trả lời khác nhau."""
    store = FakeStore([_workflow()], filtered=[])
    result = _service(store).retrieve(
        "", "hunonic", filters=KnowledgeFilter(screens="khong_ton_tai")
    )

    assert result.found is False
    assert "bộ lọc" in result.reason
    assert "khong_ton_tai" in result.reason


def test_lookup_without_filters_is_unchanged():
    """Tương thích ngược: đường truy hồi cũ không được đổi hành vi."""
    workflow = _workflow(status="verified")
    store = FakeStore([workflow], exact=[workflow.id])

    result = _service(store).retrieve("chuyển nhà", "hunonic")

    assert result.found is True
    assert result.filters == {}
    assert [name for name, _ in store.seen] == ["exact", "keyword", "vector"]
    assert all(item is None for _, item in store.seen)


def test_log_records_the_filter_used():
    """Không có bộ lọc trong log thì không phân biệt được "KB không có" với "lọc sai"."""
    store = FakeStore([_workflow()], filtered=["modal.home.switch_house"])
    _service(store).retrieve(
        "", "hunonic", filters=KnowledgeFilter(screens="home", origin="operator kb_lookup")
    )

    assert store.logged.filters["screens"] == ["home"]
    assert store.logged.filters["origin"] == "operator kb_lookup"


def test_wire_names_are_singular_while_log_names_follow_the_dataclass():
    """Dây dùng tên endpoint công bố (``screen``); log dùng tên trường (``screens``)."""
    filters = KnowledgeFilter(screens="home", types="modal", namespaces="modal.home")
    assert filters.as_request_dict() == {
        "namespace": ["modal.home"],
        "type": ["modal"],
        "screen": ["home"],
    }
    assert set(filters.as_dict()) == {"namespaces", "types", "screens"}


def test_wire_payload_round_trips_through_the_api_parser():
    """Gửi bằng tên số ít rồi đọc lại bằng chính parser của API ⇒ cùng một bộ lọc."""
    original = KnowledgeFilter(screens="home", tags="nav-graph", ids="modal.home.switch_house")
    restored = filter_from_payload(original.as_request_dict())
    assert restored == original
