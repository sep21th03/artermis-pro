"""Nạp `nav-graph.json` (do ``scripts/nav_analyze.mjs`` sinh) vào Knowledge Base hiện có.

Vì sao script chứ không phải vài lệnh curl: dữ liệu máy trích xuất phải đi vào KB theo đúng luật
an toàn, và luật đó phải được **cưỡng chế bằng máy**:

* **Không ghi đè bản `verified`.** Bản đã được người xác minh là nguồn sự thật; máy trích xuất lại
  chỉ được tạo bản `draft` mới.
* **Không ghi đè bản `draft` đã có** (mặc định): bản cũ có thể do người viết tay. Muốn cập nhật
  thì dùng ``--update-drafts``.
* **Kiểm ID trùng trước khi ghi**: hai node map về cùng một id là lỗi mapping, phải dừng chứ không
  ghi đè lẫn nhau.
* **Kiểm phiên bản**: `schema` của nav-graph phải khớp và `app_id` phải khớp `--app-id`.
* **Rollback**: mọi thay đổi được ghi vào file rollback; ``--rollback <file>`` xoá bản vừa tạo và
  phục hồi bản đã ghi đè.

Id sinh ra theo namespace đã duyệt (Phase 1 — **không** migrate 63 workflow cũ):

    screen.<key>                     định nghĩa màn hình
    navigation.<verb>_<to_key>       cạnh điều hướng có bằng chứng
    modal.<screen>.<name>            modal
    bottomsheet.<screen>.<name>      bottom sheet
    dialog.<screen>.<name>           dialog
    popup.<screen>.<name>            popup

Mỗi entry máy trích xuất: ``status="draft"`` + ``metadata.confidence`` + ``metadata.evidence``
(file:line) + ``metadata.extracted``.  Navigation Graph chỉ là **ngữ cảnh cho Planner**; mọi thao

tác trên APK release vẫn phải xác minh bằng UI thật (Accessibility + screenshot).

Chạy::

    .venv/bin/python scripts/kb_import_nav_graph.py nav-graph.json --app-id hunonic --dry-run
    .venv/bin/python scripts/kb_import_nav_graph.py nav-graph.json --app-id hunonic \\
        --report /tmp/nav-import.json --rollback-out /tmp/nav-rollback.json
    .venv/bin/python scripts/kb_import_nav_graph.py --rollback /tmp/nav-rollback.json
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA = "artemis.nav_graph/1"
NAMESPACES = (
    "screen",
    "navigation",
    "modal",
    "bottomsheet",
    "dialog",
    "popup",
    "action",
    "workflow",
    "auth",
)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
VERB_BY_KIND = {"open": "open", "replace": "replace", "reset": "reset", "dispatch": "dispatch"}


def slugify(text: str, limit: int = 60) -> str:
    folded = unicodedata.normalize("NFD", str(text or ""))
    folded = "".join(char for char in folded if unicodedata.category(char) != "Mn")
    slug = re.sub(r"[^a-z0-9]+", "_", folded.lower()).strip("_")
    return slug[:limit] or "unknown"


# ── API console ────────────────────────────────────────────────────────────────
def api_get(base: str, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def api_send(base: str, path: str, payload: dict | None, method: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        return exc.code, {"detail": body}
    except Exception as exc:  # noqa: BLE001
        return 0, {"detail": str(exc)[:200]}


# ── Ánh xạ nav-graph → payload KB ──────────────────────────────────────────────
def _evidence_list(items: list[dict], limit: int = 6) -> list[dict]:
    out: list[dict] = []
    for item in items[:limit]:
        out.append(
            {
                "file": item.get("file"),
                "line": item.get("line"),
                "snippet": item.get("snippet"),
            }
        )
    return out


def screen_payload(app_id: str, node: dict, extracted: dict) -> dict:
    """Một màn hình → ``screen.<key>``.

    Bước để trống: định nghĩa màn hình **không** phải hướng dẫn thao tác — nhét một bước bịa vào
    đây là dạy agent bấm sai chỗ.
    """
    key = slugify(node.get("key") or node.get("route"))
    components = ", ".join(node.get("components") or [])
    description = (
        f'Màn hình "{node.get("route")}"'
        + (f" · component {components}" if components else "")
        + f" · nguồn: {', '.join(node.get('sources') or [])}"
        + (f" · deep link {node.get('deep_link')}" if node.get("deep_link") else "")
        + ". Ngữ cảnh cho Planner; thao tác trên máy vẫn phải xác minh bằng UI thật."
    )
    return {
        "id": f"screen.{key}",
        "app_id": app_id,
        "name": f"Màn hình {node.get('route')}",
        "description": description,
        "aliases": [str(node.get("route") or "")] + (node.get("components") or []),
        "tags": ["nav-graph", "screen"],
        "status": "draft",
        "steps": [],
        "success_criteria": [],
        "metadata": {
            "type": "screen",
            "screen": key,
            "route": node.get("route"),
            "navigators": node.get("navigators") or [],
            "deep_link": node.get("deep_link"),
            "confidence": 0.9 if "route_table" in (node.get("sources") or []) else 0.6,
            "evidence": _evidence_list(node.get("evidence") or []),
            "extracted": extracted,
        },
    }


def edge_payload(app_id: str, edge: dict, extracted: dict) -> dict:
    """Một cạnh **có bằng chứng** → ``navigation.<verb>_<to_key>_from_<from_key>``.

    Id phải gồm **cả hai đầu**: nhiều màn hình có thể cùng mở một màn hình đích
    (``screen A → collections`` và ``screen B → collections``); nếu chỉ đặt id theo đích thì chúng
    ghi đè nhau — đo trên app-hunonic: 14 id trùng trước khi thêm phần ``from``.

    Bước duy nhất nói thẳng đây là suy ra từ source và phải tìm phần tử trên UI — không bịa nhãn
    UI nào, và điều kiện (guard) nằm trong ``metadata.nav_edge.guards``.
    """
    to_key = slugify(edge.get("to"))
    from_key = slugify(edge.get("from"))
    verb = VERB_BY_KIND.get(str(edge.get("kind")), "open")
    evidence = _evidence_list(edge.get("evidence") or [])
    first = evidence[0] if evidence else {}
    trigger = first.get("snippet") or ""
    guards = edge.get("guards") or []
    name = f"Mở màn hình {edge.get('to')} từ {edge.get('from')}"
    description = (
        f"Điều hướng {edge.get('from')} → {edge.get('to')} (theo source: {trigger})."
        + (
            f" Điều kiện điều hướng: {'; '.join(g.get('text', '') for g in guards)}."
            if guards
            else ""
        )
        + " Ngữ cảnh cho Planner; phải xác minh màn hình thật trước khi thao tác."
    )
    return {
        "id": f"navigation.{verb}_{to_key}_from_{from_key}",
        "app_id": app_id,
        "name": name,
        "description": description,
        "aliases": [str(edge.get("to") or "")],
        "tags": ["nav-graph", "navigation"],
        "status": "draft",
        "steps": [
            {
                "action": "tap",
                "target": (
                    f'phần tử/thao tác mở màn hình "{edge.get("to")}"'
                    f" (suy ra từ source {first.get('file')}:{first.get('line')}, chưa xác minh trên UI)"
                ),
                "expected_state": f'Màn hình "{edge.get("to")}" hiển thị',
            }
        ],
        "success_criteria": [],
        "metadata": {
            "type": "navigation",
            "nav_edge": {
                "from": edge.get("from"),
                "to": edge.get("to"),
                "kind": edge.get("kind"),
                "calls": edge.get("calls") or [],
                "guards": guards,
                "confidence": edge.get("confidence", 0.6),
                "verified": False,
            },
            "confidence": edge.get("confidence", 0.6),
            "evidence": evidence,
            "extracted": extracted,
        },
    }


def overlay_payload(app_id: str, overlay: dict, extracted: dict) -> dict | None:
    """Modal/bottomsheet/dialog/popup → ``<kind>.<screen>.<name>``.

    Phase 1 **không** suy diễn trigger hay cách đóng: không có bằng chứng trong source thì để
    ``steps`` rỗng và ``metadata.trigger = null`` — composer sẽ in "KB chưa ghi các bước cụ thể.
    Quan sát UI để xác định thao tác." thay vì dạy agent bấm bừa.
    """
    kind = str(overlay.get("kind") or "")
    if kind not in {"modal", "bottomsheet", "dialog", "popup"}:
        return None
    name = str(overlay.get("name") or "")
    if not name:
        return None
    screen = slugify(overlay.get("used_in_screen") or "global")
    slug = slugify(name.split(".")[-1])
    return {
        "id": f"{kind}.{screen}.{slug}",
        "app_id": app_id,
        "name": f"{name} ({'định nghĩa' if overlay.get('defined') else 'chỗ dùng'})",
        "description": (
            f'{kind} "{name}" trong source'
            + (
                f" — dùng ở component {overlay.get('used_in_component')}"
                if overlay.get("used_in_component")
                else ""
            )
            + (
                f" với prop hiển thị {overlay.get('visible_prop')}"
                if overlay.get("visible_prop")
                else ""
            )
            + ". Chưa có bằng chứng về cách mở/đóng — phải xác minh trên UI thật."
        ),
        "aliases": [name],
        "tags": ["nav-graph", kind],
        "status": "draft",
        "steps": [],
        "success_criteria": [],
        "metadata": {
            "type": kind,
            "screen": screen,
            "trigger": None,  # Phase 1: không suy diễn
            "visible_prop": overlay.get("visible_prop"),
            "defined": bool(overlay.get("defined")),
            "confidence": 0.5,
            "evidence": [
                {
                    "file": overlay.get("file"),
                    "line": overlay.get("line"),
                    "snippet": overlay.get("evidence"),
                }
            ],
            "extracted": extracted,
        },
    }


def build_payloads(graph: dict, app_id: str) -> tuple[list[dict], list[dict]]:
    """nav-graph → (payloads, lỗi mapping). Id trùng bị coi là lỗi mapping."""
    extracted = {
        "source": "ast",
        "analyzer": (graph.get("source") or {}).get("analyzer") or {},
        "generated_at": graph.get("generated_at"),
        "source_root": (graph.get("source") or {}).get("root"),
    }
    payloads: list[dict] = []
    for node in graph.get("screens") or []:
        payloads.append(screen_payload(app_id, node, extracted))
    for edge in graph.get("edges") or []:
        # Đích không nằm trong route table ⇒ chưa phải màn hình thật: không tạo entry điều hướng
        # (analyzer đã ghi nó vào `unresolved` để người xem lại).
        if edge.get("to_registered") is False:
            continue
        payloads.append(edge_payload(app_id, edge, extracted))
    # Gộp overlay theo (kind, name, file): định nghĩa + chỗ dùng trong **cùng file** là một entry
    # (ưu tiên ngữ cảnh màn hình). Khác file ⇒ là component khác nhau trùng tên, phải tách.
    overlays: dict[tuple[str, str, str], dict] = {}
    for overlay in graph.get("overlays") or []:
        key = (str(overlay.get("kind")), str(overlay.get("name")), str(overlay.get("file") or ""))
        existing = overlays.get(key)
        if existing is None:
            overlays[key] = dict(overlay)
            continue
        merged = dict(existing)
        if overlay.get("used_in_screen"):
            merged["used_in_screen"] = overlay["used_in_screen"]
            merged["used_in_component"] = overlay.get("used_in_component")
            merged["visible_prop"] = overlay.get("visible_prop")
        merged["defined"] = bool(existing.get("defined")) or bool(overlay.get("defined"))
        overlays[key] = merged

    built: list[tuple[dict, str, int | None]] = []
    for overlay in overlays.values():
        payload = overlay_payload(app_id, overlay, extracted)
        if payload:
            built.append((payload, str(overlay.get("file") or ""), overlay.get("line")))
    # Tên component trùng nhau ở nhiều file (đo trên app-hunonic: ModalManageHouse, ModalDateAndTimePicker…)
    # ⇒ thêm hậu tố **đường dẫn** file (không chỉ tên file: hai file khác thư mục có thể trùng stem).
    id_counts = Counter(payload["id"] for payload, _, _ in built)
    for payload, file, _line in built:
        if id_counts[payload["id"]] > 1:
            payload["id"] = f"{payload['id']}_{slugify(file, limit=90)}"
        payloads.append(payload)
    # Lần hai: cùng tên chỉ khác hoa/thường trong **cùng** file (`modalManageHouse` khai báo và
    # `ModalManageHouse` dùng) ⇒ thêm số dòng, cũng chính là dòng bằng chứng.
    id_counts_after = Counter(payload["id"] for payload, _, _ in built)
    for payload, _file, line in built:
        if id_counts_after[payload["id"]] > 1 and line:
            payload["id"] = f"{payload['id']}_l{line}"

    errors: list[dict] = []
    seen: dict[str, int] = {}
    for payload in payloads:
        seen[payload["id"]] = seen.get(payload["id"], 0) + 1
    for workflow_id, count in seen.items():
        if count > 1:
            errors.append({"id": workflow_id, "error": f"id trùng {count} lần trong nav-graph"})
        if not _ID_RE.match(workflow_id):
            errors.append({"id": workflow_id, "error": "id không hợp lệ"})
        if workflow_id.split(".")[0] not in NAMESPACES:
            errors.append({"id": workflow_id, "error": "namespace không nằm trong quy ước"})
    return payloads, errors


def decide_action(
    existing_status: str | None,
    *,
    update_drafts: bool,
    dry_run: bool,
) -> str:
    """Quyết định ghi gì cho một id — tách riêng để test được mà không cần API.

    ``verified`` là nguồn sự thật do người xác minh: máy trích xuất **không bao giờ** ghi đè.
    """
    if existing_status is None:
        return "would-create" if dry_run else "create"
    if existing_status == "verified":
        return "skip_verified"
    if not update_drafts:
        return "skip_existing"
    return "would-update" if dry_run else "update"


# ── Rollback ──────────────────────────────────────────────────────────────────
def do_rollback(base: str, rollback_path: Path) -> int:
    """Xoá bản đã tạo và phục hồi bản đã ghi đè, theo file rollback."""
    plan = json.loads(rollback_path.read_text(encoding="utf-8"))
    created = plan.get("created") or []
    updated = plan.get("updated") or []
    for workflow_id in created:
        status, _ = api_send(base, f"/workflows/{workflow_id}", None, "DELETE")
        print(f"xoá {workflow_id:44} HTTP {status}", flush=True)
    for row in updated:
        status, body = api_send(base, f"/workflows/{row['id']}", row.get("previous"), "PUT")
        print(f"phục hồi {row['id']:40} HTTP {status} {body.get('detail') or ''}", flush=True)
    print(f"rollback xong: xoá {len(created)} · phục hồi {len(updated)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", nargs="?", type=Path, help="nav-graph.json")
    parser.add_argument("--app-id", required=False, help="app_id trong KB (phải khớp nav-graph)")
    parser.add_argument("--console", default="http://127.0.0.1:8787", help="Base URL console")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ kiểm tra, không ghi")
    parser.add_argument("--update-drafts", action="store_true", help="Ghi đè bản draft đã có")
    parser.add_argument("--report", type=Path, default=None, help="Ghi báo cáo JSON")
    parser.add_argument("--rollback-out", type=Path, default=None, help="Ghi kế hoạch rollback")
    parser.add_argument("--rollback", type=Path, default=None, help="Chạy rollback từ file")
    args = parser.parse_args()

    base = args.console.rstrip("/") + "/api/kb"
    if args.rollback:
        return do_rollback(base, args.rollback)
    if not args.graph or not args.app_id:
        parser.error("cần <graph> và --app-id (hoặc dùng --rollback)")

    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    if graph.get("schema") != SCHEMA:
        print(
            f"LỖI: schema {graph.get('schema')!r} khác {SCHEMA!r} — analyzer/importer lệch phiên bản."
        )
        return 2
    graph_app = (graph.get("source") or {}).get("app_id")
    if graph_app and graph_app != args.app_id:
        print(f"LỖI: nav-graph thuộc app {graph_app!r} nhưng --app-id={args.app_id!r}.")
        return 2

    payloads, errors = build_payloads(graph, args.app_id)
    counts = graph.get("counts") or {}
    print(
        f"nav-graph: {counts.get('screens', 0)} màn hình · {counts.get('edges_with_evidence', 0)} cạnh"
        f" · {counts.get('unresolved', 0)} không xác định · {len(payloads)} payload KB"
    )
    if errors:
        for row in errors:
            print(f"LỖI MAPPING {row['id']}: {row['error']}")
        print("Dừng: sửa mapping trước khi ghi (không ghi gì).")
        return 2

    created: list[str] = []
    updated: list[dict] = []
    results: list[dict] = []
    for payload in payloads:
        workflow_id = payload["id"]
        status, existing = api_get(base, f"/workflows/{workflow_id}")
        if status not in (200, 404, 0):
            results.append({"id": workflow_id, "result": f"GET{status}"})
            print(f"{workflow_id:46} LỖI ĐỌC (HTTP {status})", flush=True)
            continue
        action = decide_action(
            existing.get("status") if status == 200 else None,
            update_drafts=args.update_drafts,
            dry_run=args.dry_run,
        )
        if action == "skip_verified":
            results.append({"id": workflow_id, "result": "skipped_verified"})
            print(f"{workflow_id:46} BỎ QUA (đã verified)", flush=True)
            continue
        if action == "skip_existing":
            results.append({"id": workflow_id, "result": "skipped_existing"})
            print(f"{workflow_id:46} BỎ QUA (đã có: {existing.get('status')})", flush=True)
            continue
        if action == "would-create":
            results.append({"id": workflow_id, "result": "would-create"})
            print(f"{workflow_id:46} SẼ TẠO (dry-run)", flush=True)
            continue
        if action == "would-update":
            results.append({"id": workflow_id, "result": "would-update"})
            print(f"{workflow_id:46} SẼ CẬP NHẬT (dry-run)", flush=True)
            continue

        started = time.perf_counter()
        put_status, body = api_send(base, f"/workflows/{workflow_id}", payload, "PUT")
        if action == "update":
            if put_status == 200:
                updated.append({"id": workflow_id, "previous": existing})
            results.append(
                {
                    "id": workflow_id,
                    "result": "updated" if put_status == 200 else f"HTTP{put_status}",
                    "embedded": body.get("embedded"),
                }
            )
            print(
                f"{workflow_id:46} {'CẬP NHẬT':8} HTTP {put_status}"
                f" embedded={body.get('embedded')} {body.get('detail') or ''}",
                flush=True,
            )
            continue

        entry = {
            "id": workflow_id,
            "result": "created" if put_status == 200 else f"HTTP{put_status}",
            "embedded": body.get("embedded"),
            "ms": round((time.perf_counter() - started) * 1000),
        }
        if put_status == 200:
            created.append(workflow_id)
        else:
            entry["detail"] = body.get("detail")
        results.append(entry)
        print(
            f"{workflow_id:46} {entry['result']:8} embedded={entry.get('embedded')}"
            f" {entry['ms']}ms {entry.get('detail') or ''}",
            flush=True,
        )

    summary = {
        "app_id": args.app_id,
        "graph": str(args.graph),
        "dry_run": args.dry_run,
        "payloads": len(payloads),
        "created": len(created),
        "updated": len(updated),
        "skipped_existing": sum(1 for row in results if row["result"] == "skipped_existing"),
        "skipped_verified": sum(1 for row in results if row["result"] == "skipped_verified"),
        "errors": sum(
            1
            for row in results
            if str(row["result"]).startswith("HTTP") or str(row["result"]).startswith("GET")
        ),
        "nav_counts": counts,
        "results": results,
    }
    print(
        f"\nTạo mới {summary['created']} · cập nhật {summary['updated']}"
        f" · bỏ qua (đã có) {summary['skipped_existing']}"
        f" · bỏ qua (verified) {summary['skipped_verified']} · lỗi {summary['errors']}"
    )
    if args.report:
        args.report.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if args.rollback_out:
        args.rollback_out.write_text(
            json.dumps(
                {"schema": SCHEMA, "created": created, "updated": updated},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
