# Review bộ Knowledge Base Hunonic cho HunonicTest

Ngày phân tích: 2026-10-27  
Source app: `app-hunonic`, nhánh `Hoa-check-scan-dali`, commit `00a3ab9e9`  
Schema KB (repo này): commit `c1a1f50` cùng các thay đổi local chưa commit  
Phiên bản thấy trong source: Android `4.0.20`; iOS target chính `4.0.21`

## Tóm tắt

- 66 chức năng và hướng dẫn màn hình được chọn sau khi đối chiếu source, navigation, lịch sử Git, session cũ và dump KB.
- 63 workflow mới có trong `workflows.json`: 62 `draft` và 1 `verified` (`device.card_tap_vs_hold`, có ghi bằng chứng đo trên máy thật).
- 3 workflow đã có trong KB được giữ nguyên ở trạng thái `verified` và không xuất lại để tránh ghi đè.
- 62 workflow `draft` cần ít nhất một lượt xác minh thủ công; 21 workflow điều hướng màn hình có thể review nhanh bằng quan sát UI, còn các workflow thay đổi dữ liệu/quyền hoặc phá hủy cần môi trường test riêng.
- 3 bản trùng ID đã được loại khỏi file import; 9 nhóm gần trùng về ngữ nghĩa được ghi trong `duplicates-report.md`.
- App có hơn 1.600 file JS/TS và hàng trăm screen/route, nhưng không biến mọi screen thành workflow. Bộ này ưu tiên các chức năng người dùng phổ biến, có đường UI đủ rõ và không phụ thuộc tọa độ.

## Ma trận workflow mới

| ID | Chức năng | Bằng chứng chính | Tin cậy source | Cần xác minh |
|---|---|---|---|---|
| `auth.login_password` | Đăng nhập bằng mật khẩu | `LoginScreen.js:620-741` | Cao | Tài khoản hợp lệ, permission lần đầu |
| `auth.forgot_password` | Quên mật khẩu | `LoginScreen.js:730-731`, route trong `App.js` | Trung bình | Các bước xác minh/OTP |
| `auth.register` | Đăng ký tài khoản | `LoginScreen.js:380-399,754-760` | Trung bình | Trường bắt buộc, OTP |
| `home.switch_house` | Chuyển nhà chủ động | `MenuScreen.js:729-733`, `ListHouse.js:462` | Cao | Trạng thái sau khi tải nhà |
| `home.create_house` | Thêm nhà | `ManagerShareHouseScreen.js:76-84`, `AddHouseScreen.js` | Cao | Chọn địa chỉ/quyền vị trí |
| `home.edit_house` | Đổi thông tin nhà | `ListHouse.js:194-203`, `AddHouseScreen.js` | Cao | Nhãn action menu |
| `home.set_default_house` | Đặt nhà mặc định | `ListHouse.js:207-217,506` | Cao | Tài khoản nhiều nhà |
| `home.share_house` | Chia sẻ nhà | `ListHouse.js:220-235,450-451` | Trung bình | Biểu mẫu và quyền thực tế |
| `home.delete_house` | Xóa nhà | `ListHouse.js:259-327` | Cao | Hậu quả với thiết bị/phòng |
| `device.view_all` | Xem tất cả thiết bị | `MenuScreen.js:779-781`, route `App.js` | Cao | Bố cục list thật |
| `device.add_device` | Thêm thiết bị | route config trong `App.js:1149-1151`, `DetailRoom.js:176` | Trung bình | Từng họ thiết bị/phương thức config |
| `device.move_house` | Chuyển thiết bị | `MenuScreen.js:756-764`, `ModalMoveDevice.js` | Trung bình | Thứ tự chọn nguồn/đích |
| `device.share_multiple` | Chia sẻ nhiều thiết bị | `MenuScreen.js:747-751`, route `App.js` | Trung bình | Người nhận/quyền |
| `device.manage_shares` | Quản lý chia sẻ | `ManageShareDeviceScreen.js:38-58` | Cao | Dữ liệu từng tab |
| `room.edit_room` | Chỉnh sửa phòng | `EditRoom.js:691-763` | Cao | Đường mở nút sửa |
| `room.toggle_all_devices` | Bật/tắt cả phòng | `DetailRoom.js:208-272` | Cao | Thiết bị hỗ trợ/không hỗ trợ |
| `room.delete_room` | Xóa phòng | `EditRoom.js:672,769-783` | Cao | Thiết bị sau khi xóa phòng |
| `timer.create_device_timer` | Tạo lịch thiết bị | `ListAlarm.js`, `EditAlarmNew.js:2260-2571` | Cao | Biến thể theo thiết bị/nơi lưu |
| `timer.edit_device_timer` | Sửa lịch thiết bị | `EditAlarmNew.js:2145-2449` | Cao | Server so với Home Server |
| `timer.delete_device_timer` | Xóa lịch thiết bị | `EditAlarmNew.js:1105-1134,2486-2500` | Cao | Hai nhánh xóa local/server |
| `scene.create_press` | Ngữ cảnh chạy khi nhấn | `CollectionPressList.js`, `EditCollectionBase.js` | Cao | Nút thêm trên UI thật |
| `scene.create_automation` | Ngữ cảnh tự động | `CollectionAutoList.js:552-556`, `EditCollectionAuto.js` | Cao | Điều kiện theo loại thiết bị |
| `scene.create_scheduled` | Ngữ cảnh hẹn giờ | `CollectionAutoList.js:595-599`, `EditCollectionTimer.js` | Cao | Lưu server/thiết bị |
| `scene.delete` | Xóa ngữ cảnh | ba màn EditCollection* | Cao | Xóa trong/ngoài folder |
| `settings.change_language` | Đổi ngôn ngữ khi login | `LoginScreen.js:462-547` | Cao | Persistence sau restart |
| `settings.change_wifi` | Đổi Wi-Fi thiết bị | `MenuScreen.js:869-871`, route `App.js` | Trung bình | Từng phương thức cấu hình |
| `notification.view_center` | Xem thông báo | `MenuScreen.js:525-527`, `App.js:1138-1139` | Cao | Empty state và deep link |

## Hướng dẫn màn hình bổ sung

| ID | Màn hình/định hướng | Phạm vi |
|---|---|---|
| `navigation.bottom_tabs_overview` | Bản đồ thanh điều hướng chính | Giải thích Trang chủ, Phòng, Ngữ cảnh, Cửa hàng, Menu và các tab tùy chọn. |
| `navigation.open_home` | Trang chủ | Về tổng quan của nhà hiện tại; nhận biết modal phủ màn hình. |
| `navigation.open_rooms` | Phòng | Mở danh sách và tùy chọn mở chi tiết phòng; không áp dụng nhà trọ. |
| `navigation.open_collections` | Ngữ cảnh | Mở khu vực scene/automation; không áp dụng nhà trọ. |
| `navigation.open_menu` | Menu | Đi tới nơi chứa tài khoản và các chức năng quản lý. |
| `navigation.open_device_settings` | Thông tin/Cài đặt thiết bị | Đường chung qua thẻ thiết bị rồi biểu tượng bánh răng/info; UI thay đổi theo loại thiết bị. |
| `navigation.open_device_timers` | Danh sách hẹn giờ thiết bị | Chỉ mở màn hình lịch, không tạo/sửa/xóa lịch. |
| `navigation.open_all_devices` | Tất cả thiết bị | Mở danh sách mà không điều khiển thiết bị. |
| `navigation.open_house_management` | Quản lý nhà | Bản đồ các tab Danh sách, Nhà đã nhận, Nhà đã chia sẻ. |
| `navigation.open_device_share_manager` | Quản lý chia sẻ thiết bị | Bản đồ các tab Chờ nhận, Đã nhận và Đã chia sẻ. |
| `navigation.open_notifications` | Trung tâm thông báo | Hỗ trợ lối vào từ Menu hoặc Trang chủ. |
| `navigation.open_change_wifi` | Đổi Wi-Fi | Chỉ mở luồng, chưa gửi cấu hình mạng. |
| `navigation.open_device_history` | Lịch sử thiết bị | Có điều kiện theo loại thiết bị. |
| `navigation.open_device_share` | Chia sẻ một thiết bị | Tôn trọng quyền chủ sở hữu. |
| `navigation.open_scene_editor` | Editor ngữ cảnh | Mở đúng editor theo loại ngữ cảnh. |
| `navigation.open_timer_editor` | Editor hẹn giờ | Mở lịch hiện có nhưng chưa chỉnh sửa. |
| `navigation.open_language_settings` | Cài đặt ngôn ngữ sau đăng nhập | Khác bộ chọn tại màn login. |
| `navigation.open_display_settings` | Cài đặt hiển thị | Chế độ sáng/tối và số thiết bị mỗi hàng. |
| `navigation.open_qr_device_scanner` | Quét QR thiết bị | Chỉ mở scanner. |
| `navigation.open_firmware_updates` | Thiết bị cần cập nhật | Chỉ xuất hiện khi có thiết bị đủ điều kiện. |

## Chức năng cơ bản bổ sung

| ID | Chức năng | Lưu ý |
|---|---|---|
| `room.create_room` | Tạo phòng | Chọn tên, biểu tượng và màu ngay trong modal Quản lý phòng. |
| `device.toggle_power` | Bật/tắt một thiết bị | Chỉ áp dụng thiết bị có điều khiển nguồn; vị trí nút phụ thuộc loại thiết bị. |
| `device.rename` | Đổi tên thiết bị | Nhóm DALI dùng nhãn Đổi tên nhóm. |
| `device.delete` | Xóa thiết bị | Thao tác phá hủy, có nhánh riêng cho thiết bị con/gateway. |
| `device.share_one` | Chia sẻ một thiết bị | Yêu cầu quyền chủ sở hữu. |
| `scene.run_press` | Chạy ngữ cảnh thủ công | Dùng icon play; chạm thân dòng sẽ mở editor. |
| `timer.toggle_active` | Bật/tắt một lịch | Không thay đổi nội dung lịch. |
| `timer.toggle_all` | Bật/tắt toàn bộ lịch | Menu gần lựa chọn Xóa tất cả nên cần đối chiếu kỹ. |
| `home.respond_share_invitation` | Chấp nhận/từ chối nhà chia sẻ | Kiểm tra người gửi, tên nhà và quyền trước khi chọn. |
| `settings.change_theme` | Đổi giao diện sáng/tối | Hỗ trợ Theo hệ thống, Sáng và Tối. |

## Workflow bổ sung sau khi rà tiếp màn hình

| ID | Chức năng/hướng dẫn | Lưu ý |
|---|---|---|
| `navigation.open_account_profile` | Mở hồ sơ tài khoản | Tách khỏi workflow chỉ đọc tài khoản hiện tại. |
| `room.sort_rooms` | Sắp xếp thứ tự phòng | Cần xác minh gesture kéo và nhãn lưu trên máy thật. |
| `scene.sort_press` | Sắp xếp ngữ cảnh chạy khi nhấn | Chỉ chủ nhà; không áp dụng cho tab tự động/hẹn giờ. |

## Workflow đã có trong KB

| ID | Status | Bằng chứng |
|---|---|---|
| `auth.logout` | verified | Dump có 5 revision; tài liệu HunonicTest ghi run thiết bị thật ngày 2026-10-26. Run nhận đúng workflow và thao tác đúng, nhưng verdict cuối là inconclusive do hết budget; trạng thái verified đến từ dữ liệu tester hiện hữu, không phải lần phân tích này tự nâng. |
| `auth.switch_account` | verified | Tester cung cấp và đã đối chiếu `LoginScreen.js`; dump revision 1 ngày 2026-10-26. |
| `home.switch_house_modal` | verified | Chuyển từ rule/modal đã dùng thực tế; dump revision 2 ngày 2026-10-26. |

Lưu ý: `auth.logout` đang có alias “đăng nhập tài khoản khác” và “đổi tài khoản”, dễ cạnh tranh với `auth.switch_account`. Không sửa tự động vì bản hiện tại là `verified`; reviewer nên tạo revision thu hẹp alias.

## Dữ liệu từ session cũ đã dùng

- Session 2026-10-26 về xây dựng và kiểm thử Workflow RAG: schema, trust policy exact/keyword/vector, kiểm thử `auth.logout`, `auth.switch_account`, modal chuyển nhà và các lỗi retrieval đa ý định.
- `SESSION-HANDOFF-artemis-test-console.txt`: repo runtime đúng là `artermis-pro`, app package `com.iot.hunonic`, thiết bị thật và giới hạn E2E.
- Session 2026-10-22: phân tích `collectionTimer`, xác nhận ngữ cảnh hẹn giờ có payload/hành vi riêng với lịch thiết bị và có nhánh lưu Home Server/server.
- Các session app Hunonic tháng 10/2026 được dùng để nhận diện khu vực tính năng đã thay đổi (DALI, nhóm LED, camera, timer), nhưng không dùng bug-fix nội bộ làm bằng chứng rằng luồng UI đã verified.

## Nguồn code/tài liệu

- `app-hunonic/App.js`, `src/Common/NavigatorManage.js`: route đang được đăng ký.
- `src/Components/Auth`, `HomeTab`, `House`, `Room`, `Alarm`, `Collection`, `DeviceShare` và `src/Modal/ModalHouse`: UI, nhãn và logic thao tác.
- `src/Translations/Language/vi.json`: thuật ngữ tiếng Việt và cảnh báo.
- Git log hiện tại của `app-hunonic`, đặc biệt các thay đổi mới về DALI; lịch sử được dùng để hiểu tính năng nhưng source HEAD luôn được ưu tiên.
- `artemis/knowledge/{README.md,models.py,api.py,schema.sql}` (trong repo này): schema/import thực tế.
- `artemis_kb_dump.sql`: snapshot KB để chống trùng, không kết nối VPS và không ghi database.

## Nhóm còn thiếu dữ liệu hoặc cố ý chưa tạo workflow

- Điều khiển từng loại thiết bị: công tắc, dimmer, LED/DALI, rèm/cửa, điều hòa IR, camera, khóa, cảm biến, công tơ/sạc. UI và hành vi khác nhau quá nhiều; cần inventory theo `root_type` và chạy thiết bị thật trước.
- Reset/khôi phục thiết bị: có nhiều nhánh theo loại thiết bị và gateway, chưa nên gom thành một workflow chung.
- Nhóm thiết bị, RF group, DLED group, liên kết NeoR6/DALI: lịch sử cho thấy luồng còn thay đổi và có bug gần đây; chưa phù hợp làm tri thức ổn định.
- Chuyển quyền sở hữu nhà và quản lý thành viên chi tiết.
- Camera, SmartLock/TADO, motel, Premium, thanh toán, thống kê điện/sạc, cửa cổng và tính năng nâng cao.
- Các workflow cấu hình sâu SmartConfig/AP/Bluetooth/LAN/QR và recovery device.

## Tiêu chí nâng từ draft lên verified

Mỗi workflow cần chạy trên đúng app version và ghi tối thiểu: nền tảng, thiết bị, loại tài khoản/quyền, nhà/thiết bị thử nghiệm, các nhãn UI thực tế, kết quả từng bước và cleanup. Workflow nguy hiểm phải chạy trên dữ liệu disposable. Chỉ nâng `verified` khi Checker hoặc tester xác nhận cả success criteria; đọc source hoặc thấy route không đủ.
