# Knowledge Base của HunonicTest — bộ dữ liệu app Hunonic

Toàn bộ kiến thức về app **Hunonic** (`com.iot.hunonic`) được versioned ở đây: workflow hướng dẫn
từng chức năng, đồ thị điều hướng trích từ source, bộ ca kiểm tra retrieval và các báo cáo review.
Nhờ vậy một máy mới **không cần dump database** cũng dựng lại được KB — chỉ cần vài lệnh import.

> Đây là **nguồn sự thật** của KB: sửa workflow thì sửa file ở đây (hoặc sửa qua tab Knowledge của
> console rồi export lại), đừng sửa tay trong database.

## Có gì trong đây

| File | Nội dung |
| --- | --- |
| `workflows.json` | **63 workflow** (`app_id: hunonic`) — 62 `draft` + 1 `verified` (`device.card_tap_vs_hold`, có bằng chứng đo trên máy thật); mảng payload tương thích `PUT /api/kb/workflows/{id}`, trung bình 3,5 bước/workflow |
| `device-action-workflows.json` | **26 workflow** `device_action.*` — luật chọn thiết bị/hành động khi tạo ngữ cảnh, hẹn giờ |
| `device-action-import-report.json` | Kết quả lần import 26 workflow trên (`created`, `embedded`, `revision_no`) |
| `nav-graph-hunonic.json` | Đồ thị điều hướng trích bằng AST từ `app-hunonic`: 404 màn hình · 433 cạnh có bằng chứng · 429 overlay định nghĩa · 1.084 điểm không phân tích được (`scripts/nav_analyze.mjs`) |
| `retrieval-test-cases.json` | **96 ca kiểm tra retrieval**, trong đó 24 ca có `must_not_choose_as_primary` |
| `workflows-review.md` | Ma trận review từng workflow: chức năng, bằng chứng source (`file:line`), độ tin cậy, việc cần xác minh |
| `duplicates-report.md` | Các nhóm gần trùng về ngữ nghĩa và cách xử lý |
| `import-instructions.md` | Trình tự import an toàn — **đọc trước khi nạp vào KB đang chạy** |

Trong `workflows.json`, `metadata` giữ bằng chứng và mức rủi ro để người review biết cần cẩn thận ở
đâu: 62 mục có `evidence`, 20 có `screen_guide`, 15 có `risk` (5 `destructive`, 4 `permission_change`,
3 `device_control`, 1 `credential`, 1 `state_change`, 1 `bulk_state_change`), 9 có `variant_note`.
Mọi workflow để `version_id = null`; nhãn phiên bản nguồn nằm ở `metadata.app_version`
(60 mục: `Android 4.0.20 / iOS 4.0.21`, 2 mục: `Android 4.0.20`).

## Đã nạp gì, còn gì

Đo trên KB đang chạy (`GET /api/kb/status` → `workflow_count`): **89 workflow** = 60 mục của
`workflows.json` + 26 `device_action.*` + 3 workflow `verified` có trước (giữ nguyên, không nằm trong
bộ này).

**Còn lại 3 workflow trong `workflows.json` chưa có trong KB** — lần import tới sẽ tạo đúng 3 mục này:
`navigation.open_account_profile`, `room.sort_rooms`, `scene.sort_press`.

## Nạp vào KB

Cần console đang chạy và KB dùng được (`GET /api/kb/status` → `"available": true`; console đọc
PostgreSQL qua SSH tunnel, kiểm bằng `sh scripts/kb_tunnel.sh status`).

```bash
cd ~/Developer/artermis-pro

# 1) Luôn xem trước: chỉ GET, không ghi, không gọi embedding
.venv/bin/python scripts/kb_import_workflows.py kb/workflows.json --dry-run
.venv/bin/python scripts/kb_import_workflows.py kb/device-action-workflows.json --dry-run
.venv/bin/python scripts/kb_import_nav_graph.py kb/nav-graph-hunonic.json --app-id hunonic --dry-run

# 2) Nạp thật (mỗi payload đúng một request; endpoint tự index embedding)
.venv/bin/python scripts/kb_import_workflows.py kb/workflows.json --report /tmp/kb-import.json
.venv/bin/python scripts/kb_import_nav_graph.py kb/nav-graph-hunonic.json --app-id hunonic
```

> Nav graph tạo **nhiều** mục `draft` (mỗi màn hình / cạnh / overlay một mục — bộ hiện tại báo
> ~2.000 payload KB) và chạy tuần tự nên mất vài phút: luôn `--dry-run` trước, và dùng `--report`
> để xem đúng danh sách sẽ ghi.

Luật an toàn đã được **cưỡng chế bằng máy**, không bằng trí nhớ người chạy:

- **GET trước, PUT sau** — ID đã tồn tại thì bỏ qua, **không bao giờ ghi đè** bản `verified` đang chạy thật.
- Mỗi payload một request, kết quả từng cái ghi lại được (`--report`).
- Kiểm lại `embedded` trong response thay vì tin là xong: ghi được mà chưa index thì script thoát mã 1.
- Nav graph: không tạo cạnh chỉ vì cùng nằm trong route table; mọi entry mới là `draft` kèm
  confidence/evidence; `--rollback-out` ghi kế hoạch rollback trước khi sửa.

## Kiểm tra sau khi nạp

```bash
# 1) Số lượng workflow tăng đúng
curl -s http://127.0.0.1:8787/api/kb/status | python3 -c 'import json,sys;print(json.load(sys.stdin)["workflow_count"])'

# 2) Một ca retrieval (mỗi lần gọi tốn 1 embedding): phải trả về workflow nằm trong expected_workflow_ids
python3 -c 'import json;c=json.load(open("kb/retrieval-test-cases.json",encoding="utf-8"))["cases"][0];print(c)'
curl -s -X POST http://127.0.0.1:8787/api/kb/retrieve -H 'Content-Type: application/json' \
  --data '{"query":"<câu trong ca vừa in>","app_id":"hunonic","top_k":3}' | head -c 400
```

Chạy hết 96 ca thì tốn 96 embedding — làm khi cần đo chất lượng retrieval, không phải bước bắt buộc.

## Ghi chú

- `artemis.nav_graph/1` trong `nav-graph-hunonic.json` là **schema kỹ thuật** mà
  `scripts/kb_import_nav_graph.py` kiểm tra — đừng đổi chuỗi này.
- Sinh lại nav graph: `node scripts/nav_analyze.mjs` (đường dẫn source khai trong
  `config/nav_sources.json`; hiện trỏ `~/Developer/app-hunonic`).
- App phải có trong KB trước khi import (`POST /api/kb/apps`), xem `import-instructions.md`.
