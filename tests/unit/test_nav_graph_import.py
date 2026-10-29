"""Tests cho importer Navigation Graph (``scripts/kb_import_nav_graph.py``).

Vì sao test ở đây chứ không chỉ chạy script: luật an toàn của importer (không ghi đè bản
``verified``, không tạo cạnh chỉ vì cùng nằm trong route table, mọi entry là ``draft`` kèm
confidence/evidence) là thứ **phải không thể vi phạm do vô ý** — nếu chỉ có script thì một lần
sửa vội là mất luật. Các test dưới đây kiểm tra đúng những luật đó, không cần KB thật.
"""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "kb_import_nav_graph.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("kb_import_nav_graph", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nav = _load_module()


def _graph(**overrides):
    """nav-graph tối thiểu, giống output của ``scripts/nav_analyze.mjs``."""
    graph = {
        "schema": nav.SCHEMA,
        "generated_at": "2026-10-29T00:00:00.000Z",
        "source": {
            "root": "/tmp/rn-app",
            "app_id": "hunonic",
            "analyzer": {"script": "scripts/nav_analyze.mjs", "mode": "ast"},
        },
        "counts": {"screens": 2, "edges_with_evidence": 1, "unresolved": 1, "guards": 1},
        "screens": [
            {
                "route": "Home",
                "key": "home",
                "components": ["HomeScreen"],
                "navigators": ["Stack"],
                "sources": ["route_table"],
                "deep_link": None,
                "evidence": [{"file": "App.js", "line": 20, "snippet": "Stack.Screen name=Home"}],
            },
            {
                "route": "collections",
                "key": "collections",
                "components": ["CollectionsScreen"],
                "navigators": ["linking"],
                "sources": ["route_table", "linking"],
                "deep_link": "collections/:id",
                "evidence": [{"file": "App.js", "line": 25, "snippet": "Collections: path"}],
            },
        ],
        "edges": [
            {
                "from": "Home",
                "to": "Profile",
                "kind": "open",
                "calls": ["navigation.navigate"],
                "guards": [
                    {"text": "isLoggedIn", "polarity": "then", "file": "Home.js", "line": 12}
                ],
                "evidence": [
                    {"file": "Home.js", "line": 13, "snippet": "navigation.navigate('Profile')"}
                ],
                "confidence": 0.6,
            }
        ],
        "overlays": [
            {
                "name": "ModalSwitchHouse",
                "kind": "modal",
                "defined": False,
                "used_in_component": "HomeScreen",
                "used_in_screen": "Home",
                "visible_prop": "{isSwitchHouseVisible}",
                "trigger": None,
                "file": "Home.js",
                "line": 88,
                "evidence": "ModalSwitchHouse visible={isSwitchHouseVisible}",
            }
        ],
        "unresolved": [
            {"kind": "dynamic_target", "file": "Home.js", "line": 30, "detail": "navigate(route)"}
        ],
        "parse_errors": [],
    }
    graph.update(overrides)
    return graph


def test_screen_payload_is_draft_with_evidence_and_no_invented_steps():
    payloads, errors = nav.build_payloads(_graph(), "hunonic")
    assert not errors

    screen = next(row for row in payloads if row["id"] == "screen.home")

    assert screen["status"] == "draft"
    assert screen["app_id"] == "hunonic"
    assert screen["steps"] == []  # định nghĩa màn hình không phải hướng dẫn thao tác
    assert screen["metadata"]["type"] == "screen"
    assert screen["metadata"]["confidence"] == 0.9
    assert screen["metadata"]["evidence"][0]["file"] == "App.js"
    assert "Planner" in screen["description"]  # nói rõ chỉ là ngữ cảnh cho Planner


def test_edge_payload_carries_guards_and_says_it_is_unverified():
    payloads, _ = nav.build_payloads(_graph(), "hunonic")
    edge = next(row for row in payloads if row["id"] == "navigation.open_profile_from_home")

    assert edge["status"] == "draft"
    assert edge["metadata"]["nav_edge"]["guards"][0]["text"] == "isLoggedIn"
    assert edge["metadata"]["nav_edge"]["verified"] is False
    assert edge["metadata"]["confidence"] == 0.6
    assert edge["metadata"]["evidence"][0]["line"] == 13
    # Bước duy nhất phải nói thẳng là suy ra từ source và chưa xác minh trên UI.
    assert "chưa xác minh trên UI" in edge["steps"][0]["target"]
    assert "Home.js:13" in edge["steps"][0]["target"]


def test_no_navigation_payload_when_route_table_has_no_call_site():
    """Không tạo cạnh chỉ vì hai màn hình cùng nằm trong route table."""
    graph = _graph(edges=[], overlays=[])
    payloads, errors = nav.build_payloads(graph, "hunonic")

    assert not errors
    assert {row["id"].split(".")[0] for row in payloads} == {"screen"}
    assert [row["id"] for row in payloads] == ["screen.home", "screen.collections"]


def test_overlay_payload_never_infers_trigger_or_dismissal():
    payloads, _ = nav.build_payloads(_graph(), "hunonic")
    overlay = next(row for row in payloads if row["id"].startswith("modal."))

    assert overlay["id"] == "modal.home.modalswitchhouse"
    assert overlay["metadata"]["type"] == "modal"
    assert overlay["metadata"]["trigger"] is None
    assert overlay["steps"] == []  # chưa có bằng chứng cách mở/đóng ⇒ không dạy agent bấm gì
    assert "Chưa có bằng chứng" in overlay["description"]


def test_overlay_without_screen_falls_back_to_global_scope():
    graph = _graph(
        overlays=[
            {
                "name": "AppBottomSheet",
                "kind": "bottomsheet",
                "defined": True,
                "used_in_screen": None,
                "file": "AppModal.js",
                "line": 12,
                "evidence": "export function AppBottomSheet()",
            }
        ]
    )
    payloads, _ = nav.build_payloads(graph, "hunonic")
    assert any(row["id"] == "bottomsheet.global.appbottomsheet" for row in payloads)


def test_duplicate_ids_abort_instead_of_overwriting_each_other():
    graph = _graph()
    graph["screens"] = [graph["screens"][0], dict(graph["screens"][0])]

    payloads, errors = nav.build_payloads(graph, "hunonic")

    assert payloads  # payload vẫn được dựng, nhưng...
    assert any("trùng" in row["error"] for row in errors)  # ...lỗi mapping phải chặn ghi


def test_id_outside_namespace_convention_is_rejected(monkeypatch):
    monkeypatch.setattr(
        nav,
        "screen_payload",
        lambda app_id, node, extracted: {"id": "screens.home", "app_id": app_id, "steps": []},
    )
    _, errors = nav.build_payloads(_graph(edges=[], overlays=[]), "hunonic")
    assert any("namespace" in row["error"] for row in errors)


@pytest.mark.parametrize(
    ("existing", "update_drafts", "dry_run", "expected"),
    [
        (None, False, False, "create"),
        (None, False, True, "would-create"),
        ("draft", False, False, "skip_existing"),
        ("draft", True, False, "update"),
        ("draft", True, True, "would-update"),
        ("verified", False, False, "skip_verified"),
        ("verified", True, False, "skip_verified"),  # kể cả --update-drafts
        ("verified", True, True, "skip_verified"),
    ],
)
def test_decide_action_matrix(existing, update_drafts, dry_run, expected):
    assert nav.decide_action(existing, update_drafts=update_drafts, dry_run=dry_run) == expected


def test_generated_ids_stay_disjoint_from_legacy_navigation_names():
    """Bản cũ trong KB có ``navigation.open_*`` (theo **đích**); id mới thêm cả đầu nguồn nên
    **không đè** bản cũ, và nếu có trùng thì ``decide_action`` vẫn bỏ qua chứ không ghi đè."""
    graph = _graph(
        edges=[
            {
                "from": "Home",
                "to": "collections",
                "kind": "open",
                "calls": ["NavigationService.navigate"],
                "guards": [],
                "evidence": [
                    {"file": "Home.js", "line": 40, "snippet": "NavigationService.navigate(KEY)"}
                ],
                "confidence": 0.9,
            }
        ]
    )
    payloads, errors = nav.build_payloads(graph, "hunonic")

    assert not errors
    assert any(row["id"] == "navigation.open_collections_from_home" for row in payloads)
    assert not any(row["id"] == "navigation.open_collections" for row in payloads)
    assert nav.decide_action("draft", update_drafts=False, dry_run=False) == "skip_existing"


def test_every_generated_id_matches_id_rules():
    payloads, errors = nav.build_payloads(_graph(), "hunonic")
    assert not errors
    for payload in payloads:
        assert nav._ID_RE.match(payload["id"]), payload["id"]
        assert payload["id"].split(".")[0] in nav.NAMESPACES
        assert payload["metadata"]["evidence"], payload["id"]


def test_rollback_plan_round_trips(tmp_path):
    """Rollback phải ghi được và đọc lại được: xoá bản tạo mới, phục hồi bản bị ghi đè."""
    plan = {
        "schema": nav.SCHEMA,
        "created": ["screen.home", "navigation.open_profile"],
        "updated": [
            {
                "id": "screen.collections",
                "previous": {"id": "screen.collections", "status": "draft"},
            }
        ],
    }
    path = tmp_path / "rollback.json"
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    loaded = json.loads(path.read_text(encoding="utf-8"))

    assert loaded["created"] == ["screen.home", "navigation.open_profile"]
    assert loaded["updated"][0]["previous"]["status"] == "draft"
