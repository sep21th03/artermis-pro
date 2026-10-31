# Hướng dẫn review và import vào KB của HunonicTest

## Định dạng

`workflows.json` là một mảng các payload tương thích `WorkflowRequest` của endpoint:

```text
PUT /api/kb/workflows/{workflow_id}
```

Schema thực tế dùng:

- `version_id`, không dùng `app_version` ở cấp workflow. Nhãn phiên bản nguồn nằm tạm trong `metadata.app_version` vì dump chưa có bản ghi `kb_app_version` cho Hunonic.
- Step dùng `expected_state` và `until`, không dùng `expected_result` hay `until_visible`.
- Action hợp lệ: `tap`, `long_press`, `scroll`, `swipe`, `input_text`, `back`, `confirm_if_present`, `wait`, `assert`.
- Status hợp lệ: `draft`, `verified`, `deprecated`; schema hiện tại không có `reviewed`.
- `change_note` là field của request API và được lưu thành revision; nó không phải field của model `Workflow` trả về.

## Trình tự an toàn

1. Sao lưu KB hoặc tạo database staging từ schema `artemis/knowledge/schema.sql` (repo này).
2. Đảm bảo app `hunonic` tồn tại:

```bash
curl -fsS http://127.0.0.1:8787/api/kb/apps
```

Nếu chưa có, tạo app trước (không cần nếu đang dùng DB từ dump):

```bash
curl -fsS -X POST http://127.0.0.1:8787/api/kb/apps \
  -H 'Content-Type: application/json' \
  --data '{"id":"hunonic","name":"Hunonic","package_name":"com.iot.hunonic"}'
```

3. Review từng workflow `draft`, đặc biệt các mục có `metadata.risk` hoặc `metadata.variant_note`.
4. Với mỗi ID, gọi `GET /api/kb/workflows/{id}` trước:
   - 404: có thể tạo bằng PUT.
   - Đã tồn tại và `status=verified`: bỏ qua, không ghi đè.
   - Đã tồn tại ở trạng thái khác: so diff và yêu cầu tester duyệt revision trước khi PUT.
5. Import từng payload, không import hàng loạt mù quáng. Ví dụ với một file tạm chứa đúng một object:

```bash
workflow_id='device.view_all'
curl -fsS -X PUT "http://127.0.0.1:8787/api/kb/workflows/${workflow_id}" \
  -H 'Content-Type: application/json' \
  --data-binary @device.view_all.json
```

Endpoint tự validate, lưu revision và gọi index embedding. Vì task này cấm gọi embedding trước review, chưa lệnh import nào được chạy.

6. Sau khi reviewer kiểm thử UI thật, đổi riêng workflow đạt yêu cầu từ `draft` sang `verified` bằng một revision có `change_note` nêu thiết bị, app version và ngày test.
7. Chạy bộ truy vấn trong `retrieval-test-cases.json` qua `POST /api/kb/retrieve`; kiểm tra cả `expected_workflow_ids` và `must_not_choose_as_primary`.

## Lưu ý phiên bản

Source hiện tại cho thấy Android `4.0.20`; target iOS chính có `MARKETING_VERSION=4.0.21`. Do chưa có version row trong dump và chưa chắc hai nền tảng có UI hoàn toàn giống nhau, tất cả workflow mới để `version_id=null` và ghi nhãn nguồn trong metadata. Nên tạo hai version ID ổn định rồi gắn workflow sau khi kiểm thử từng nền tảng.

## Không thực hiện trong lần tạo dữ liệu này

- Không gọi API import.
- Không gọi embedding model.
- Không kết nối hoặc ghi PostgreSQL/VPS.
- Không sửa ba workflow `verified` hiện tại.
