"""Import bộ workflow JSON vào Artemis KB qua API của console.

Vì sao là script chứ không phải một lệnh curl: bộ dữ liệu KB được sinh theo lô (xem
``artemis-kb-generated/import-instructions.md``) và quy tắc an toàn phải được cưỡng chế
bằng máy, không bằng trí nhớ người chạy:

* **GET trước, PUT sau.** Bỏ qua mọi ID đã tồn tại — tuyệt đối không ghi đè bản
  ``verified`` đang chạy thật.
* **Không import hàng loạt mù quáng**: mỗi payload đi qua đúng một request, kết quả từng
  cái được ghi lại để đối chiếu.
* **Xác nhận embedding**: endpoint ``PUT /api/kb/workflows/{id}`` tự index embedding, và
  script kiểm tra lại trường ``embedded`` của response thay vì tin là xong.

Chạy::

    .venv/bin/python scripts/kb_import_workflows.py ~/Developer/artemis-kb-generated/workflows.json
    .venv/bin/python scripts/kb_import_workflows.py <file.json> --console http://127.0.0.1:8787
    .venv/bin/python scripts/kb_import_workflows.py <file.json> --dry-run
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def api_get(base: str, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def api_put(base: str, path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        return exc.code, {"detail": body}
    except Exception as exc:  # noqa: BLE001
        return 0, {"detail": str(exc)[:200]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="File JSON chứa mảng payload workflow")
    parser.add_argument("--console", default="http://127.0.0.1:8787", help="Base URL console")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ kiểm tra, không ghi")
    parser.add_argument("--report", type=Path, default=None, help="Ghi kết quả ra JSON")
    args = parser.parse_args()

    base = args.console.rstrip("/") + "/api/kb"
    payloads = json.loads(args.source.read_text(encoding="utf-8"))
    if isinstance(payloads, dict):
        payloads = payloads.get("workflows") or []

    print(f"{len(payloads)} payload từ {args.source}")
    results: list[dict] = []
    for payload in payloads:
        workflow_id = payload.get("id")
        if not workflow_id:
            results.append({"id": None, "result": "invalid", "detail": "thiếu id"})
            continue

        status, existing = api_get(base, f"/workflows/{workflow_id}")
        if status == 200:
            # Đã tồn tại: không ghi đè (đặc biệt là bản `verified`).
            results.append(
                {"id": workflow_id, "result": "skipped", "existing_status": existing.get("status")}
            )
            print(f"{workflow_id:34} BỎ QUA (đã có: {existing.get('status')})", flush=True)
            continue
        if status not in (404, 0):
            results.append({"id": workflow_id, "result": f"GET{status}", "detail": existing})
            print(f"{workflow_id:34} LỖI ĐỌC (HTTP {status})", flush=True)
            continue
        if args.dry_run:
            results.append({"id": workflow_id, "result": "would-create"})
            print(f"{workflow_id:34} SẼ TẠO (dry-run)", flush=True)
            continue

        started = time.perf_counter()
        put_status, body = api_put(base, f"/workflows/{workflow_id}", payload)
        entry = {
            "id": workflow_id,
            "result": "created" if put_status == 200 else f"HTTP{put_status}",
            "embedded": body.get("embedded"),
            "status": body.get("status"),
            "revision_no": body.get("revision_no"),
            "ms": round((time.perf_counter() - started) * 1000),
        }
        if put_status != 200:
            entry["detail"] = body.get("detail")
        results.append(entry)
        print(
            f"{workflow_id:34} {entry['result']:8} embedded={entry.get('embedded')} "
            f"{entry['ms']}ms {entry.get('detail') or ''}",
            flush=True,
        )

    created = sum(1 for row in results if row["result"] == "created")
    embedded = sum(1 for row in results if row.get("embedded"))
    skipped = sum(1 for row in results if row["result"] == "skipped")
    print(f"\nTạo mới {created} · bỏ qua {skipped} · đã index embedding {embedded}/{created}")
    if args.report:
        args.report.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Báo cáo: {args.report}")
    return 0 if created == embedded else 1


if __name__ == "__main__":
    raise SystemExit(main())
