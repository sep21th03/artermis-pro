"""Test end-to-end cho analyzer AST (``scripts/nav_analyze.mjs``) trên fixture.

Vì sao chạy node thật trong unit test: giá trị của analyzer nằm ở **quan hệ đã trích ra** (cạnh nào
có bằng chứng, guard nào được nhận, chỗ nào bị đánh dấu unresolved) — mock AST không chứng minh
được điều đó. Test tự bỏ qua khi máy không có node/@babel, nên suite vẫn chạy được ở môi trường
không có toolchain JS.

Fixture nằm ở ``tests/fixtures/nav_graph``: route tĩnh, deep link, route key (kể cả hằng
placeholder), call site có guard, điều kiện ``&&``, thoát sớm, đích động, đi lùi, modal/bottomsheet.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ANALYZER = REPO / "scripts" / "nav_analyze.mjs"
FIXTURE = REPO / "tests" / "fixtures" / "nav_graph"
BABEL_FALLBACKS = [
    Path("/Users/nguyenhoa/Developer/app-hunonic"),
    REPO,
]


def _find_node() -> str | None:
    explicit = os.environ.get("ARTEMIS_NODE_BIN")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("node")
    if found:
        return found
    for candidate in sorted(Path.home().glob(".nvm/versions/node/*/bin/node"), reverse=True):
        if candidate.exists():
            return str(candidate)
    return None


def _find_babel_from() -> str | None:
    explicit = os.environ.get("ARTEMIS_BABEL_FROM")
    if explicit and (Path(explicit) / "node_modules" / "@babel" / "parser").exists():
        return explicit
    for candidate in BABEL_FALLBACKS:
        if (candidate / "node_modules" / "@babel" / "parser").exists():
            return str(candidate)
    return None


NODE = _find_node()
BABEL_FROM = _find_babel_from()

pytestmark = pytest.mark.skipif(
    NODE is None or BABEL_FROM is None,
    reason="cần node và một node_modules/@babel/parser (đặt ARTEMIS_NODE_BIN / ARTEMIS_BABEL_FROM)",
)


@pytest.fixture(scope="module")
def graph(tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("nav") / "fixture-graph.json"
    result = subprocess.run(  # noqa: S603 - đường dẫn do test kiểm soát
        [
            NODE,
            str(ANALYZER),
            "--source",
            str(FIXTURE),
            "--app-id",
            "fixture",
            "--babel-from",
            BABEL_FROM,
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_analyzer_writes_schema_and_source_metadata(graph):
    assert graph["schema"] == "artemis.nav_graph/1"
    assert graph["source"]["app_id"] == "fixture"
    assert graph["source"]["analyzer"]["mode"] == "ast"
    assert graph["source"]["files_parsed"] > 0
    assert graph["parse_errors"] == []


def _edges(graph) -> set[tuple[str, str]]:
    return {(edge["from"], edge["to"]) for edge in graph["edges"]}


def _edge(graph, from_route: str, to_route: str) -> dict:
    return next(
        edge for edge in graph["edges"] if edge["from"] == from_route and edge["to"] == to_route
    )


def test_static_routes_and_deep_links_become_screen_nodes(graph):
    screens = {screen["route"]: screen for screen in graph["screens"]}

    assert {"Home", "Login", "Collections", "Settings", "HomeTab"} <= set(screens)
    assert "linking" in screens["Settings"]["sources"]
    assert screens["Settings"]["deep_link"] == "settings"
    assert "route_table" in screens["Collections"]["sources"]
    assert screens["Collections"]["deep_link"] == "collections/:id"
    assert all(screen["evidence"] for screen in graph["screens"])


def test_route_keys_and_navigators_are_registered(graph):
    counts = graph["counts"]
    assert counts["route_keys"] == 4
    assert counts["navigators"] == 2


def test_edges_require_evidence_and_merge_call_sites(graph):
    edge = _edge(graph, "Home", "Collections")

    assert edge["kind"] == "open"
    # Hai call site (literal + qua route key) ⇒ hai bằng chứng cho **một** cạnh.
    assert len(edge["evidence"]) == 2
    assert all(item["file"] and item["line"] for item in edge["evidence"])


def test_conditional_navigation_carries_the_condition(graph):
    guarded = _edge(graph, "Home", "Profile")
    assert [guard["text"] for guard in guarded["guards"]] == ["isLoggedIn"]
    assert guarded["guards"][0]["polarity"] == "then"

    logical = _edge(graph, "Home", "Login")
    assert logical["kind"] == "replace"
    assert [guard["text"] for guard in logical["guards"]] == ["ready"]

    early_return = _edge(graph, "Login", "Home")
    assert [guard["polarity"] for guard in early_return["guards"]] == ["early_return"]
    assert early_return["guards"][0]["text"] == "!user"


def test_no_edge_is_invented_from_route_table_membership(graph):
    """Collections/Settings chỉ nằm trong route table: không được có cạnh nào xuất phát từ chúng."""
    assert not {pair for pair in _edges(graph) if pair[0] in {"Collections", "Settings"}}


def test_dynamic_and_history_calls_are_unresolved_not_guessed(graph):
    unresolved = graph["unresolved"]
    kinds = {item["kind"] for item in unresolved}

    assert {"dynamic_target", "history_navigation"} <= kinds
    assert graph["counts"]["unresolved"] == len(unresolved)
    assert graph["counts"]["unresolved_by_kind"]["history_navigation"] == 2
    # Hằng placeholder trỏ tới route không có trong route table: cạnh vẫn được ghi kèm cờ
    # `to_registered=false` **và** một mục unresolved — không âm thầm coi là màn hình thật.
    placeholder_edge = _edge(graph, "Home", "KEY_NAME_PLACEHOLDER")
    assert placeholder_edge["to_registered"] is False
    assert "target_not_registered" in kinds
    assert graph["counts"]["edges_to_unregistered_targets"] == 2


def test_overlays_are_classified_without_inventing_triggers(graph):
    overlays = graph["overlays"]
    kinds = {item["kind"] for item in overlays}

    assert {"modal", "bottomsheet", "dialog"} <= kinds
    assert graph["counts"]["overlays_defined"] >= 3
    for item in overlays:
        if not item["defined"]:
            assert item["trigger"] is None  # Phase 1: không suy diễn trigger


def test_bottomsheet_usage_is_linked_to_its_screen(graph):
    usage = next(
        item
        for item in graph["overlays"]
        if item["kind"] == "bottomsheet" and item.get("used_in_component") == "HomeScreen"
    )
    assert usage["used_in_screen"] == "Home"
    assert usage["visible_prop"] == "{sheetVisible}"
    # Overlay dùng chung (không thuộc màn hình nào) thì không được gán bừa cho một màn hình.
    shared = next(
        item
        for item in graph["overlays"]
        if item["kind"] == "bottomsheet" and item["defined"] and item["name"] == "AppBottomSheet"
    )
    assert shared["file"] == "src/components/AppModal.js"


def test_fixture_graph_imports_into_namespaced_draft_entries(graph):
    """Analyzer → importer: entry sinh ra đúng namespace, đều là draft, và **không** có cạnh bịa."""
    spec = importlib.util.spec_from_file_location(
        "kb_import_nav_graph", REPO / "scripts" / "kb_import_nav_graph.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    payloads, errors = module.build_payloads(graph, "fixture")

    assert not errors
    ids = {payload["id"] for payload in payloads}
    assert {"screen.home", "screen.collections", "navigation.open_collections_from_home"} <= ids
    assert all(payload["status"] == "draft" for payload in payloads)
    assert all(payload["metadata"].get("evidence") for payload in payloads)
    # Cạnh trỏ tới route **không đăng ký** (Profile, KEY_NAME_PLACEHOLDER trong fixture) không
    # sinh entry điều hướng — chúng đã nằm trong `unresolved` để người xem lại.
    assert "navigation.open_profile" not in ids
    assert not {row for row in ids if "key_name_placeholder" in row}
    assert len([row for row in ids if row.startswith("navigation.")]) == 3
