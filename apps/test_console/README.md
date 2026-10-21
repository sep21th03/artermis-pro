# Mobile Test Console

Web UI tiếng Việt cho tester điều khiển ARTEMIS trên điện thoại Android thật.
Nằm ngay trong repo ARTEMIS (`apps/test_console/`), dùng song song với UI gốc
(`artemis ui`, cổng 8000).

Không cần source app Android: console chỉ gọi `artemis run ... --locked-app <package>`,
nên dùng được cho bất kỳ app nào bạn nhập ở ô **Package**.

## Tính năng

- **Timeline bước chạy**: ảnh Trước → Sau, nhãn hành động tiếng Việt, bấm ảnh phóng to.
- **Kết luận rõ ràng**: ĐẠT / KHÔNG ĐẠT / ĐÃ DỪNG, lỗi chính, số bước, thời gian, chi phí.
- **Thông báo khi xong**: toast + âm thanh + Web Notification.
- Ô **Prompt / Mục tiêu** tự do: nhập prompt bất kỳ, sửa prompt mẫu, nút
  **Prompt tự do**, lưu nháp, `⌘/Ctrl+Enter` để chạy.
- Chọn thiết bị đang cắm USB (adb); package mặc định `com.example.app` (đổi ở ô **Package**).
- 7 kịch bản mẫu dựng sẵn: smoke khởi động, màn hình đăng nhập, danh sách
  thiết bị, bật/tắt công tắc, voicebox/học lệnh, thêm thiết bị QR/BLE, thông báo.
- Chạy **Flash** (nhanh) hoặc **Pro** (có plan/verify).
- Xem **màn hình điện thoại trực tiếp** (chụp 2 giây/lần) và **log realtime**.
- Dừng task **thật sự**: huỷ cả trên Artemis Daemon (nơi thực thi) lẫn tiến
  trình CLI, xem **lịch sử** và log từng lần chạy.
- Kiểm tra LLM (`/models` + 1 chat 1 token) — phát hiện key hết hạn/hết credit.
- **Chặn ngân sách**: giới hạn request, `max_tokens`, chi phí task/ngày, thời gian.

## Chạy

Console nằm trong repo ARTEMIS và mặc định dùng chính `.venv`, `.env`,
`config/artemis.jsonc` của repo đó.

```bash
# Chạy được từ bất kỳ thư mục nào
cd ~/Developer/artermis-pro
sh apps/test_console/run.sh          # tự mở http://127.0.0.1:8787
```

- Clone mới chưa có `.venv`? Chạy `uv sync` một lần, hoặc để `run.sh` tự gọi `uv run`.
- Muốn drive một checkout ARTEMIS khác: `ARTEMIS_DIR=/path/to/artemis sh apps/test_console/run.sh`.
- Đổi app đích: `TEST_APP_PACKAGE=com.example.app sh apps/test_console/run.sh`.
- Đổi cổng: `ARTEMIS_WEB_PORT=8788 sh apps/test_console/run.sh`.

Console chỉ gọi `artemis run` nên **không cần source app**. Chỉ khi app là bản
React Native **debug** thì vẫn phải có Metro + `adb reverse`:

```bash
# (tuỳ chọn, chỉ với app debug RN)
cd /path/to/your-app
export PATH="$HOME/.nvm/versions/node/v20.19.5/bin:/opt/homebrew/bin:$PATH"
yarn start                              # giữ terminal này chạy
adb reverse tcp:8081 tcp:8081
adb shell monkey -p com.example.app -c android.intent.category.LAUNCHER 1
```

Yêu cầu: ARTEMIS đã cài dependency (`.venv`), có `adb`, điện thoại bật USB debugging.
Hướng dẫn tổng thể + cấu hình LLM: [README.md](../../README.md) mục 16.

## Cấu hình LLM + proxy

Sửa `.env` của checkout ARTEMIS đang dùng (mặc định `.env` trong repo này):

```env
OPENAI_API_KEY=sk-...                                  # key của bạn
OPENAI_BASE_URL=http://127.0.0.1:8787/llm/v1           # đi qua proxy chặn chi phí
ARTEMIS_UPSTREAM_BASE_URL=https://api.openai.com/v1    # upstream thật
```

Model trong `artemis/config/artemis.jsonc` phải là model có vision + tool
calling; cấu hình hiện tại dùng `ts/gemini-3.5-flash-lite` cho tất cả node.
Sau khi đổi `.env`/model: restart Artemis (`artemis restart`) để nạp lại.

> Nếu tắt console 8787, Artemis sẽ không gọi được LLM vì base URL trỏ vào proxy.

## Ngân sách

Proxy ghi nhận `usage` từng request và chặn khi vượt:

| Giới hạn | Mặc định |
| --- | --- |
| Request / task | 40 |
| Output token / request | 768 |
| Chi phí / task | 2.000đ |
| Chi phí / ngày | 6.000đ |
| Thời gian / task | 480s |

Đổi bằng biến môi trường khi chạy console, ví dụ:

```bash
BUDGET_MAX_TASK_COST_VND=1000 BUDGET_MAX_REQUESTS=25 \
BUDGET_DAILY_COST_VND=4000 sh apps/test_console/run.sh
```

Khi chạm hạn mức: proxy trả `429 budget_exceeded`, task tự dừng, badge ngân sách
đỏ. Chi tiêu ngày lưu ở `data/budget.json`. Xem nhanh:
`curl -s http://127.0.0.1:8787/api/budget`.

## Ghi chú

- Server chỉ bind `127.0.0.1`, không lộ ra mạng LAN.
- Key LLM không bao giờ gửi xuống trình duyệt; UI chỉ hiển thị 4 ký tự cuối.
- Mỗi task chạy `artemis run ... --locked-app <package> --profile <flash|pro>`.
- Log lưu tại `apps/test_console/data/logs/`, lịch sử tại `data/tasks.json`.
- Task gửi ảnh màn hình + XML UI lên nhà cung cấp LLM: dùng tài khoản test.
