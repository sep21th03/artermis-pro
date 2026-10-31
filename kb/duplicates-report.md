# Báo cáo trùng lặp và xung đột retrieval

Ngày đối chiếu: 2026-10-27. Nguồn KB: `artemis_kb_dump.sql` (dump ngày 2026-10-26).

## Kết quả

- KB hiện tại có 3 workflow Hunonic: `auth.logout`, `auth.switch_account`, `home.switch_house_modal`.
- Không có ID mới nào trong `workflows.json` trùng ID hiện tại.
- Đã loại khỏi file import 3 workflow hiện có để tránh ghi đè dữ liệu `verified`.
- Không phát hiện ID trùng bên trong 63 workflow mới.
- Có 9 vùng gần trùng cần theo dõi bằng retrieval test.

| Workflow/alias liên quan | Loại xung đột | Xử lý |
|---|---|---|
| `home.switch_house` và `home.switch_house_modal` | Cùng alias “chuyển nhà”, nhưng một cái là mục tiêu đổi nhà, một cái là rule xử lý modal | Workflow mới mô tả rõ “chọn nhà trong Quản lý nhà”; test bắt buộc không chọn modal làm PRIMARY cho câu “chuyển sang nhà X”. Nên thu hẹp alias `chuyển nhà`, `đổi nhà` ở workflow modal trong một revision do tester duyệt. |
| `auth.login_password`, `auth.switch_account`, `auth.logout` | KB cũ đặt alias “đăng nhập tài khoản khác”, “đổi tài khoản” cho cả logout và switch account | Không sửa workflow verified. Test tách trường hợp có mật khẩu, tài khoản đã lưu và chuỗi nhiều việc “đăng xuất rồi đổi tài khoản”. Khuyến nghị bỏ hai alias đó khỏi `auth.logout` sau review. |
| `timer.create_device_timer` và `scene.create_scheduled` | Đều có “hẹn giờ/lịch/bật đèn” | Mô tả và alias phân biệt một thiết bị với ngữ cảnh nhiều hành động; có cặp positive/negative test riêng. |
| `scene.create_press` và `scene.create_automation` | Đều là “tạo ngữ cảnh” | Alias bổ sung “chạy khi nhấn” so với “nếu-thì/tự động hóa”; câu quá chung “tạo ngữ cảnh” nên được Prompt Preparation hỏi/giữ khoảng trống thay vì tự chọn loại. |
| `navigation.open_device_timers` và nhóm `timer.*` | Một bên chỉ mở/xem màn hình, bên kia tạo/sửa/xóa dữ liệu lịch | Alias navigation ưu tiên “mở”, “xem”, “ở đâu”; retrieval test buộc câu “tạo lịch” không chọn workflow navigation làm PRIMARY. Cách phân tách tương tự áp dụng cho các workflow `navigation.*` khác. |
| `navigation.open_change_wifi` và `settings.change_wifi` | Cùng nhắc “đổi Wi-Fi” nhưng khác ý định mở màn so với thực hiện cấu hình | Test hai chiều dùng các tín hiệu “chỉ mở/chưa cấu hình” và tên mạng đích. |
| `navigation.open_scene_editor` và `scene.run_press` | Cùng chọn một dòng ngữ cảnh nhưng target khác nhau | Chạy ngữ cảnh phải chạm icon play bên phải; mở editor chạm thân dòng/nút sửa. |
| `navigation.open_display_settings` và `settings.change_theme` | Một bên chỉ tìm màn, bên kia thay đổi theme | Test hai chiều dùng “chỉ mở/chưa đổi” so với “bật chế độ tối”. |
| `navigation.open_device_settings` và `device.card_tap_vs_hold` | Cùng alias “mở cài đặt thiết bị”; workflow verified mô tả chính xác chạm so với nhấn giữ | Ưu tiên `device.card_tap_vs_hold` cho truy vấn thao tác thẻ. Cân nhắc bỏ alias trùng khỏi workflow navigation sau khi tester duyệt; không tự sửa workflow verified. |

## Workflow hiện có không được xuất lại

| ID | Trạng thái dump | Lý do giữ nguyên |
|---|---|---|
| `auth.logout` | verified | Có revision và bằng chứng E2E trong tài liệu HunonicTest; import lại sẽ vi phạm quy tắc không ghi đè verified. |
| `auth.switch_account` | verified | Tester cung cấp và đối chiếu `LoginScreen.js`; dump đã có embedding. |
| `home.switch_house_modal` | verified | Rule/modal đã được tester xác nhận; khác chức năng chủ động đổi nhà. |

## Kiểm tra cơ học

- So sánh ID mới với bảng `kb_workflow` trong dump: 0 trùng.
- Alias nội bộ được chuẩn hóa không dấu + chữ thường để soi va chạm. Những alias chung mang tính loại như “thiết bị”, “nhà”, “ngữ cảnh” chỉ nằm trong tags, không dùng làm alias độc lập.
- `workflows.json` chỉ chứa payload tạo mới. Importer phải GET workflow theo ID và bỏ qua nếu đã tồn tại; tuyệt đối không PUT đè bản `verified`.
