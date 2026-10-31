<p align="center">
  <img src="./docs/assets/artemis-banner.png?v=7" alt="ARTEMIS Banner" width="100%" />
</p>

<h1 align="center">ARTEMIS Pro</h1>

<p align="center">
  <strong>Điều khiển điện thoại Android thật bằng AI: CLI, Web UI và MCP server cho IDE.</strong>
</p>

<p align="center">
  <a href="./README.md"><b>Tiếng Việt</b></a> •
  <a href="./README_EN.md">English</a> •
  <a href="./README_CN.md">中文文档</a> •
  <a href="#8-chạy-task-bằng-cli">Chạy nhanh</a> •
  <a href="#10-kết-nối-mcp-cho-ide">MCP</a> •
  <a href="#11-dừnghuỷ-task">Dừng task</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB.svg?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License Apache-2.0">
  <img src="https://img.shields.io/badge/MCP-Native%20Server-8A2BE2.svg" alt="MCP Native">
</p>

> **ARTEMIS Pro** là bản build cá nhân của ARTEMIS (dự án gốc Apache-2.0: [google/artemis](https://github.com/google/artemis)),
> được tinh chỉnh trong quá trình làm việc trên workspace `Developer`: sửa lỗi **bấm dừng nhưng agent vẫn tiếp tục chạy**,
> và chuyển toàn bộ model routing sang một gateway OpenAI-compatible.
> Toàn bộ hướng dẫn cài đặt – cấu hình – chạy nằm trong tài liệu này; bản dịch tiếng Anh ở [README_EN.md](./README_EN.md).

## ⚡ Chạy nhanh (3 lệnh)

```bash
# 1) Clone
git clone <repo-url> artermis-pro && cd artermis-pro

# 2) Cài dependency + tạo .env (điền API key; thêm ARTEMIS_KB_DSN nếu dùng Knowledge)
uv sync --extra knowledge
cp .env.example .env && open -e .env

# 3) Chạy web + tunnel Knowledge — tự mở http://127.0.0.1:8787
sh apps/test_console/run.sh
```

- `run.sh` **tự mở tunnel KB rồi mới khởi động web** ⇒ bình thường chỉ cần chạy lệnh số 3.
- Tunnel riêng (khi cần kiểm tra / mở lại): `sh scripts/kb_tunnel.sh status | start | restart | ensure`
- **Knowledge base cần 2 thứ, không cần cài gì thêm**: (1) **file khoá SSH** tới VPS ở
  `~/.ssh/greencloud` (hoặc khai `KB_VPS_KEY=<đường dẫn khoá>` trong `.env`) — `run.sh` dùng khoá này
  để mở tunnel `127.0.0.1:5433 → VPS:5432`; (2) dòng `ARTEMIS_KB_DSN=postgresql://…@127.0.0.1:5433/artemis_kb`
  trong `.env` (xin mật khẩu từ người giữ DB — `.env.example` **không** có sẵn dòng này).
- **Thiếu khoá SSH hoặc DSN thì web vẫn chạy bình thường**, chỉ tab **Knowledge** báo không dùng được;
  khi có khoá rồi thì mở lại bằng `sh scripts/kb_tunnel.sh start`.
- Cần có: `adb` trong PATH và điện thoại Android đã bật **USB debugging** (đã bấm *Allow*).
- Chưa có `uv`: `brew install uv` (macOS) hoặc `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- Muốn chạy UI gốc của ARTEMIS (cổng 8000) thay vì Test Console: `./start.sh`.

Phần còn lại của tài liệu này là chi tiết đầy đủ (cấu hình model, Knowledge base, thiết bị, CLI, MCP, xử lý lỗi).

## Mục lục

1. [ARTEMIS Pro làm được gì](#1-artemis-pro-làm-được-gì)
2. [Thay đổi so với upstream](#2-thay-đổi-so-với-upstream)
3. [Yêu cầu hệ thống](#3-yêu-cầu-hệ-thống)
4. [Cài đặt](#4-cài-đặt)
5. [Cấu hình biến môi trường (.env)](#5-cấu-hình-biến-môi-trường-env)
6. [Cấu hình model (config/artemis.jsonc)](#6-cấu-hình-model-configartemisjsonc)
7. [Kết nối Knowledge base](#7-kết-nối-knowledge-base-bắt-buộc-cho-tab-knowledge)
8. [Kết nối thiết bị Android](#8-kết-nối-thiết-bị-android)
9. [Chạy task bằng CLI](#9-chạy-task-bằng-cli)
10. [Web UI & Admin Console](#10-web-ui--admin-console)
11. [Kết nối MCP cho IDE](#11-kết-nối-mcp-cho-ide)
12. [Dừng/huỷ task](#12-dừnghuỷ-task)
13. [Kiểm tra sức khoẻ & xử lý lỗi](#13-kiểm-tra-sức-khoẻ--xử-lý-lỗi)
14. [Chạy test & phát triển](#14-chạy-test--phát-triển)
15. [Cấu trúc thư mục](#15-cấu-trúc-thư-mục)
16. [Giấy phép & ghi công](#16-giấy-phép--ghi-công)
17. [Test Console tiếng Việt](#17-test-console-tiếng-việt)
18. [ADB server từ xa (máy DEV + máy tester)](#18-adb-server-từ-xa-máy-dev--máy-tester)


## 1. ARTEMIS Pro làm được gì

- **Tự động hoá điện thoại Android bằng ngôn ngữ tự nhiên**: mở app, đăng nhập, điền form, chụp màn hình, đọc logcat.
- **Hai chế độ chạy**:
  - `flash` — vòng lặp phản ứng *Observe → Think → Act*, nhanh, phù hợp tác vụ ngắn, đường đi rõ ràng.
  - `pro` — đồ thị đa agent (Planner → Operator → Checker → Outputter), phù hợp tác vụ dài, nhiều nhánh, cần verify và báo cáo.
- **MCP server native**: cắm điện thoại thật vào Cline, Claude Code, Cursor, Windsurf, Codex, Antigravity… để agent trong IDE điều khiển máy.
- **Web UI**: xem màn hình trực tiếp, gửi prompt, xem lại replay từng bước (mặc định `http://localhost:8000`).
- **Trace & replay**: lưu toàn bộ ảnh chụp, hành động, lý do và kết quả của từng bước để debug.

## 2. Thay đổi so với upstream

| File | Thay đổi | Lý do |
| --- | --- | --- |
| `artemis/interfaces/cli/commands/run.py` | Cầu nối `SIGTERM → KeyboardInterrupt` được cài **trước** khi route task sang Artemis Daemon | Trước đây `artemis run` chỉ đẩy task vào daemon rồi chờ; nếu bấm Stop/Ctrl+C thì tiến trình CLI chết nhưng **daemon vẫn lái điện thoại** tới khi hết turn. Nay mọi tín hiệu dừng đều đi vào nhánh `except KeyboardInterrupt` → gọi `stop_task_on_daemon()` và thoát với mã `130`. |
| `config/artemis.jsonc` | `default` + hầu hết node dùng `provider: openai`, `model: ts/gemini-3.5-flash-lite` (gateway OpenAI-compatible) | Chạy qua gateway/proxy nội bộ thay vì gọi trực tiếp Google. |
| `config/artemis.jsonc` | `profiles.flash.max_turns = 25`, `explorer_mode = "pro"` | Giới hạn số turn của Flash và tăng độ sâu quan sát màn hình. |
| `config/artemis.jsonc` | Tắt `flash.step_summarizer` và `pro.*.context` (chunk compression) | Hai tính năng này phụ thuộc Gemini key native; tắt để tránh lỗi khi chỉ có gateway OpenAI. |
| `config/artemis.jsonc` | `operator.thinking_level = "low"`, `include_thoughts = false` | Giảm token, tăng tốc mỗi bước. |

Muốn quay lại hành vi gốc: đặt `"step_summarizer": { "enabled": true }`, bật lại block `context`, và đổi `default.provider`/`default.model` sang `google` + model Gemini mà bạn có key.

## 3. Yêu cầu hệ thống

| Thành phần | Ghi chú |
| --- | --- |
| **Python** | `>= 3.12` (repo đã pin trong `pyproject.toml`) |
| **uv** | Trình quản lý package/venv — `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **ADB** | `brew install android-platform-tools` (macOS) hoặc `sudo apt install android-tools-adb` (Linux) |
| **Thiết bị** | Điện thoại Android thật đã bật **USB Debugging**, hoặc emulator đang chạy |
| **API key** | Ít nhất một key: Gemini, OpenAI (hoặc gateway OpenAI-compatible), Anthropic, OpenRouter, xAI |
| **Tuỳ chọn** | `ffmpeg`, `scrcpy` cho ghi hình / mirror màn hình (script `start.sh` tự cài) |

## 4. Cài đặt

### 4.1. Cách nhanh nhất (một lệnh)

```bash
git clone https://github.com/sep21th03/artermis-pro.git
cd artermis-pro

./start.sh        # macOS / Linux
# .\start.bat     # Windows PowerShell
```

`start.sh` sẽ tự kiểm tra và cài toolchain (uv, ADB, ffmpeg, scrcpy), tạo `.venv`, cài dependency,
hỏi bạn cấu hình API key + MCP cho IDE, rồi mở Web UI.

### 4.2. Cài thủ công

```bash
git clone https://github.com/sep21th03/artermis-pro.git
cd artermis-pro

# 1) Cài dependency Python (tạo .venv + uv.lock)
uv sync --dev

# 2) Tạo file cấu hình bí mật
cp .env.example .env
# mở .env và điền API key

# 3) Kiểm tra môi trường
uv run artemis doctor
```

> Không muốn gõ `uv run` mỗi lần? Cài CLI ra toàn hệ thống:
> ```bash
> uv tool install -e .
> artemis --help
> ```

### 4.3. Chạy bằng Docker (tuỳ chọn)

```bash
docker build -t artemis-pro .
docker run --rm -it --env-file .env -p 8080:8080 artemis-pro
```

Container expose **cổng 8080** (Admin Console + API). Lưu ý: ADB nằm ngoài container, nên muốn điều khiển
điện thoại thật bạn cần mount `~/.android` / expose ADB server hoặc chạy trực tiếp trên máy host.
Cách chạy native ở mục 4.1 / 4.2 được khuyến nghị.

## 5. Cấu hình biến môi trường (.env)

`cp .env.example .env` rồi sửa. Các biến quan trọng:

| Biến | Ý nghĩa |
| --- | --- |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Key Google AI Studio (dùng cho model `google/*`, OCR, video analyzer) |
| `OPENAI_API_KEY` | Key OpenAI **hoặc** key của gateway OpenAI-compatible |
| `OPENAI_BASE_URL` | Base URL của gateway, ví dụ `https://your-gateway/v1`. Bỏ trống → `https://api.openai.com/v1` |
| `ANTHROPIC_API_KEY`, `OPEN_ROUTER_API_KEY`, `XAI_API_KEY` | Các provider khác (tuỳ chọn) |
| `OCR_API_KEY` | Google Cloud Vision OCR (tuỳ chọn, cho OCR nâng cao) |
| `ADB_HOST` / `ADB_PORT` | Mặc định `127.0.0.1:5037` |
| `ADB_DEVICE_SERIAL` | Ghim cứng serial thiết bị khi cắm nhiều máy |
| `ARTEMIS_DEFAULT_PROFILE` | `flash` hoặc `pro` (mặc định cho CLI/MCP) |
| `ARTEMIS_HIERARCHY_BACKEND` | `auto` \| `helper` \| `uiautomator` — backend đọc cây UI |
| `ARTEMIS_HELPER_AUTO_INSTALL` | Cho phép task tự cài Accessibility Helper APK |
| `ARTEMIS_KEEP_DEVICE_AWAKE` | Giữ màn hình sáng trong lúc chạy task |

`.env` đã nằm trong `.gitignore` — **không bao giờ commit key**. Kiểm tra nhanh:

```bash
uv run artemis doctor            # kiểm tra tổng thể
uv run artemis doctor --help     # các cờ nâng cao
```

## 6. Cấu hình model (config/artemis.jsonc)

File này quyết định model cho từng "node" của agent. Cấu trúc:

```jsonc
{
  "default": {                       // model mặc định cho mọi node
    "provider": "openai",
    "model": "ts/gemini-3.5-flash-lite",
    "fallback": { "provider": "openai", "model": "ts/gemini-3.5-flash-lite" }
  },
  "presets": {                       // bộ cấu hình đặt sẵn, đổi nhanh
    "gemini-flagship": { "provider": "google", "model": "gemini-3.8-flash" }
  },
  "nodes": {                         // override riêng từng node
    "planner":  { "thinking_level": "high" },
    "operator": { "thinking_level": "low", "include_thoughts": false },
    "object_detector": { "provider": "openai", "model": "ts/gemini-3.5-flash-lite" }
  },
  "profiles": {
    "flash": { "max_turns": 25, "explorer_mode": "pro" },
    "pro":   { "explorer": { "mode": "pro" } }
  }
}
```

Lưu ý khi đổi model:

- `object_detector` cần model có khả năng **grounding toạ độ** (Gemini ER / robotics) để trả `[x, y]` chính xác.
- `flash.step_summarizer` và block `context` (chunk compression) hiện **đang tắt** vì chúng gọi Gemini native.
  Nếu bạn có `GEMINI_API_KEY`, có thể bật lại.
- Đổi `default` là đổi toàn bộ hệ thống; muốn thử nhanh một preset thì sửa `default` trỏ tới preset tương ứng.

## 7. Kết nối Knowledge base (bắt buộc cho tab Knowledge)

Knowledge base là **PostgreSQL trên VPS**, không mở cổng ra Internet. Máy tester kết nối qua
**SSH tunnel** `127.0.0.1:5433 → VPS:5432`. Không có tunnel thì console báo
*"Không kết nối được PostgreSQL: port 5433 → Connection refused"* và tab Knowledge bị khoá
(prompt vẫn chạy được, chỉ là không ghép kiến thức).

**Tester chỉ cần một lệnh** — tunnel được mở tự động khi khởi động console:

```bash
sh apps/test_console/run.sh          # tự mở tunnel rồi chạy console ở http://127.0.0.1:8787
```

Dòng `Knowledge base: tunnel sẵn sàng (127.0.0.1:5433).` là dấu hiệu KB dùng được. Nếu thấy
`CẢNH BÁO: chưa mở được tunnel tới Knowledge base` thì chạy lệnh bên dưới để xem lý do.

**Chuẩn đoán / chạy tay:**

```bash
sh scripts/kb_tunnel.sh ensure     # mở nếu chưa có, im lặng khi đã sẵn sàng (idempotent)
sh scripts/kb_tunnel.sh status     # đang chạy? cổng nào? pid nào?
sh scripts/kb_tunnel.sh start      # mở tunnel
sh scripts/kb_tunnel.sh restart    # đóng rồi mở lại (dùng khi status báo lỗi)
sh scripts/kb_tunnel.sh stop       # đóng tunnel

nc -z 127.0.0.1 5433 && echo "KB port OK"        # cổng thật sự nhận kết nối?
curl -s http://127.0.0.1:8787/api/kb/workflows | head -c 80   # KB trả dữ liệu?
```

Không cần khởi động lại console sau khi mở tunnel: nếu KB không tới được lúc console khởi động,
nó tự thử lại sau mỗi ~15 giây và tự nhận KB ngay khi tunnel lên (đo thực tế: mở tunnel xong,
tab Knowledge sống lại trong vòng chưa tới 20 giây).

**Điều kiện một lần cho mỗi máy:**

```bash
# 1) Khoá SSH tới VPS (mặc định ~/.ssh/greencloud)
ls -l ~/.ssh/greencloud && chmod 600 ~/.ssh/greencloud

# 2) Nếu VPS/user/cổng khác mặc định, đặt trong .env (hoặc biến môi trường)
KB_VPS_HOST=103.199.18.222
KB_VPS_USER=root
KB_VPS_KEY=$HOME/.ssh/greencloud
KB_LOCAL_PORT=5433
```

**Khi tunnel không lên được**, thứ tự kiểm tra:

| Triệu chứng | Nguyên nhân thường gặp |
|---|---|
| `ssh: connect to host … port 22: Operation timed out` | VPS tắt hoặc mạng tới VPS bị chặn — không lệnh nào trên máy tester sửa được, cần VPS sống lại |
| `Permission denied (publickey)` | Sai/thiếu khoá SSH, hoặc khoá chưa được thêm cho user đó |
| `bind: Address already in use` | Còn tunnel cũ giữ cổng — `sh scripts/kb_tunnel.sh restart` |
| `status` báo "đang chạy" mà KB vẫn lỗi | Kết nối SSH nửa chết (pid còn, cổng đóng). Từ bản này `status` kiểm tra **cổng thật** và tự dọn tiến trình cũ |

Máy chạy vai `server` (VPS, nối PostgreSQL trực tiếp) đặt `KB_TUNNEL=0` để bỏ bước này:
`KB_TUNNEL=0 sh apps/test_console/run.sh`.

## 8. Kết nối thiết bị Android

```bash
# 1) Cắm cáp, bật Developer options → USB debugging, chấp nhận "Allow USB debugging"
adb devices -l
# Output phải có trạng thái "device" (không phải "unauthorized"/"offline")

# 2) Nếu có nhiều máy, chọn serial và ghim vào .env
export ADB_DEVICE_SERIAL=<serial-cua-ban>

# 3) Kiểm tra ARTEMIS nhìn thấy thiết bị
uv run artemis doctor
```

Với emulator: bật AVD trước, sau đó `adb devices` sẽ thấy `emulator-5554`.

Nếu cây UI không đọc được, cài Accessibility Helper (nhanh và ổn định hơn UIAutomator2):

```bash
uv run artemis helper install      # cài/cập nhật APK helper trên máy đang cắm
uv run artemis helper --help       # các lệnh con khác
```

## 9. Chạy task bằng CLI

```bash
# 1) Task đơn giản — profile flash (vòng lặp phản ứng, nhanh)
uv run artemis run "Mở Settings, vào Battery và cho tôi biết mức pin hiện tại" -p flash

# 2) Task phức tạp — profile pro (Planner → Operator → Checker → Outputter)
uv run artemis run "Mở app demo, đăng nhập bằng tài khoản test, kiểm tra popup bất thường và chụp màn hình cuối" -p pro

# 3) Ghim thiết bị + ghim app + lưu trace để replay
uv run artemis run "Điền form đăng ký với email test@example.com" \
  --device-serial <serial-cua-ban> \
  --locked-app com.example.app \
  --test-name signup_flow \
  --traces-path traces \
  -p pro --verification-level checkpoints

# 4) Cài APK trước rồi chạy
uv run artemis run "Mở app vừa cài và kiểm tra màn hình đầu tiên" --app-path ./build/app-debug.apk

# 5) Bỏ qua daemon, chạy trực tiếp trong process (tiện debug bằng breakpoint)
uv run artemis run "Mở YouTube và tìm 'Coldplay'" --standalone
```

Các flag chính của `artemis run`:

| Flag | Mô tả |
| --- | --- |
| `-p, --profile` | `flash` (nhanh, phản ứng) hoặc `pro` (đồ thị đầy đủ). Mặc định `pro` |
| `--device-serial` | Serial thiết bị đích khi cắm nhiều máy |
| `--session-id` | Đặt session id cố định (mặc định tự sinh UUID) |
| `-a, --locked-app` | Chỉ cho phép agent thao tác trong package này |
| `--app-path` | Đường dẫn APK cần cài trước khi chạy |
| `--output-description` | Mô tả output mong muốn để Outputter tổng hợp báo cáo |
| `--test-name` | Tên test, dùng để đặt tên thư mục trace |
| `--traces-path` | Thư mục ghi trace/replay |
| `--verification-level` | Mức audit của Checker: `off` \| `final` \| `checkpoints` \| `strict` |
| `--explorer-version`, `--explorer-flash-mode`, `--explorer-pro-mode` | Độ sâu quan sát màn hình (`flash` / `pro` / `ultra`) |
| `--enable-checker/--disable-checker`, `--enable-outputter/--disable-outputter`… | Bật/tắt từng node của graph |
| `--with-video-recording-tools/--without-video-recording-tools` | Phân tích video màn hình (cần `ffmpeg`) |
| `--standalone` | Chạy local, không đẩy task vào Artemis Daemon |

Chạy hàng loạt task từ file và xem lại trace:

```bash
uv run artemis batch --file tasks.json -p pro --delay 2
uv run artemis batch --help        # xem định dạng file và các cờ
uv run artemis trace list
uv run artemis trace view <trace_id>
```

Mọi lệnh đều có `--help`, ví dụ `uv run artemis run --help`.

## 10. Web UI & Admin Console

```bash
uv run artemis ui --open          # mở http://localhost:8000
uv run artemis ui -p 8080         # đổi cổng
uv run artemis status             # server có đang chạy?
uv run artemis restart --force    # kill & khởi động lại
uv run artemis stop --force       # dừng server
```

Trên UI có: wizard kiểm tra kết nối thiết bị, mirror màn hình trực tiếp, ô prompt để gửi task,
danh sách session/task đang chạy và trình replay từng bước (ảnh + hành động + lý do).

## 11. Kết nối MCP cho IDE

```bash
uv run artemis mcp --install all             # cài cho mọi IDE được hỗ trợ
uv run artemis mcp --install antigravity     # hoặc chỉ định từng IDE
uv run artemis mcp --generate-config codex   # in snippet cấu hình thủ công
```

Ví dụ cấu hình Claude Desktop (`claude_desktop_config.json`) — nhớ thay `/path/to/artermis-pro`:

```json
{
  "mcpServers": {
    "artemis": {
      "command": "/path/to/artermis-pro/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/artermis-pro",
      "env": { "PYTHONUNBUFFERED": "1", "PYTHONPATH": "/path/to/artermis-pro" }
    }
  }
}
```

Bộ tool MCP có sẵn:

| Tool | Công dụng |
| --- | --- |
| `mobile_run_task` | Chạy task bất đồng bộ (`Flash`/`Pro`), có `device_serial`, `verification_level`, `explorer_mode` |
| `mobile_manage_task` | `status` / `inject_instruction` (can thiệp giữa chừng) / `stop` (dừng khẩn cấp) |
| `mobile_get_device_state` | Ảnh chụp màn hình hoặc cây UI (hierarchy) realtime |
| `mobile_inspect_trace` | Xem summary, search log, xem ảnh/lý do từng bước |
| `mobile_diagnose` | Bác sĩ môi trường: Python, config, key, ADB, thiết bị, cổng daemon… |

Nên mount thêm `mcp_server/rules.md` vào rules của IDE để agent tuân thủ quy trình test
(khám phá UI trước khi viết test, chọn Flash/Pro hợp lý, locator "dynamic-first, coordinate-fallback").

## 12. Dừng/huỷ task

Đây là nội dung được sửa trong session này — **bấm Stop/Ctrl+C phải làm agent dừng thật**.

**Nguyên nhân lỗi cũ:** `artemis run` chỉ *submit* task vào Artemis Daemon rồi ngồi chờ kết quả.
Khi bạn Ctrl+C (SIGINT) hoặc IDE gửi Stop (SIGTERM), chỉ tiến trình CLI chết; daemon vẫn tiếp tục
lái điện thoại cho tới khi task tự hết turn hoặc hết `timeout`.

**Cách sửa:** trong `artemis/interfaces/cli/commands/run.py`, cầu nối `SIGTERM → KeyboardInterrupt`
được cài **trước** khối routing daemon, nên mọi tín hiệu dừng đều rơi vào `except KeyboardInterrupt`,
gọi `stop_task_on_daemon(session_id)` để daemon huỷ task (finalize trace + video) rồi thoát với mã `130`.

Cách dừng đúng:

```bash
# Cách 1: Ctrl + C ngay trong terminal đang chạy task
# Cách 2: gửi SIGTERM (nút Stop của IDE thường làm việc này)
kill -TERM <pid_cua_artemis_run>

# Cách 3: dừng qua daemon / MCP
uv run artemis status
# trong IDE: mobile_manage_task(action="stop", trace_id="<session_id>")
```

Lưu ý:

- Chỉ `SIGKILL` (`kill -9`) là **không bắt được** — khi đó daemon vẫn có thể đang chạy task;
  hãy dừng qua MCP `mobile_manage_task(action="stop", ...)` hoặc `uv run artemis restart --force`.
- Task bị huỷ vẫn để lại trace/screenshot để bạn xem lại bước cuối.
- Muốn theo dõi dài hạn rồi kết thúc êm: `mobile_manage_task(action="inject_instruction", release_loop=true)`.

## 13. Kiểm tra sức khoẻ & xử lý lỗi

```bash
uv run artemis doctor        # Python, uv, ADB, thiết bị, key, toolchain
uv run artemis doctor -v     # chi tiết hơn
```

| Triệu chứng | Cách xử lý |
| --- | --- |
| `artemis: command not found` | Dùng `uv run artemis ...`, hoặc `uv tool install -e .` |
| `No devices found` / `unauthorized` | Cắm lại cáp, bật USB debugging, bấm *Allow*; `adb kill-server && adb devices` |
| Lỗi `API key` / `requires ...` | Điền key trong `.env`, chạy lại `artemis doctor` |
| Provider trả JSON lỗi (`429`, `model not found`, `401`, HTML) | `uv run artemis llm check` — in nguyên văn body + gợi ý sửa |
| `429 budget_exceeded` từ proxy 8787 | `curl -X POST http://127.0.0.1:8787/api/budget/reset` hoặc tăng `BUDGET_MAX_*` khi chạy console |
| Model trả lỗi 404/400 | Kiểm tra `provider`/`model` trong `config/artemis.jsonc` khớp gateway (`OPENAI_BASE_URL`) |
| Không đọc được cây UI | `uv run artemis helper install`, đặt `ARTEMIS_HIERARCHY_BACKEND=helper` |
| Cổng 8000 bị chiếm | `uv run artemis ui -p 8080` hoặc `uv run artemis restart --force` |
| Bấm dừng mà vẫn chạy | Xem mục 11 (bản này đã vá; nếu vẫn gặp, kiểm tra đang chạy đúng `run.py` mới) |
| MCP không start trong IDE | Gọi `mobile_diagnose`; kiểm tra `command` trỏ đúng `.venv/bin/python` |

## 14. Chạy test & phát triển

```bash
make test              # test không cần thiết bị / key
make test-integration  # test tích hợp (cần key model)
make test-device       # test cần thiết bị Android thật
make lint              # ruff format --check + ruff check + quality ratchet
make format            # tự format
make typecheck         # pyright
make build-ui          # build Angular showcase UI
make setup             # cài deps + build UI + pre-commit hooks
```

## 15. Cấu trúc thư mục

```
artermis-pro/
├── artemis/                 # Core agent: graph, llm router, tools, runtime daemon, CLI
│   ├── interfaces/cli/      # Lệnh `artemis` (run, ui, mcp, doctor, helper, batch, trace…)
│   ├── agents/              # Planner, Operator, Checker, Explorer, Outputter
│   ├── llm/                 # Router đa provider + cấu hình model
│   └── runtime/             # Artemis Daemon, queue thiết bị, IPC
├── apps/
│   ├── admin_console/       # FastAPI: session, replay, stream màn hình
│   └── showcase_ui/         # Angular frontend
├── mcp_server/              # MCP server (mobile_* tools) + rules.md
├── third_party/mobile_use/  # Vòng lặp automation gốc + helper CLI
├── packages/artemis-client/ # SDK Python để gọi Artemis từ ngoài
├── config/artemis.jsonc     # Cấu hình model & profile
├── scripts/                 # installer, quality ratchet…
├── tests/                   # unit / integration / e2e
├── traces/                  # (runtime) trace & replay — không commit
├── start.sh | start.bat     # One-click launcher
├── Dockerfile · Makefile · pyproject.toml · uv.lock
└── README.md · README_EN.md · README_CN.md
```

## 16. Giấy phép & ghi công

- Phát hành theo **Apache License 2.0** — xem [LICENSE](./LICENSE).
- Dựa trên dự án ARTEMIS của Google ([google/artemis](https://github.com/google/artemis)); toàn bộ bản quyền
  và ghi công thuộc về các tác giả gốc. Repo này giữ nguyên license và bổ sung các thay đổi ở [mục 2](#2-thay-đổi-so-với-upstream).
- Tài liệu gốc: [README_EN.md](./README_EN.md) (English) · [README_CN.md](./README_CN.md) (中文).

## 17. Test Console tiếng Việt

Nằm trong repo tại [`apps/test_console/`](./apps/test_console/) — web UI tiếng Việt cho tester,
chạy song song với UI gốc (cổng 8000). Console chỉ gọi `artemis run ... --locked-app <package>`,
**không cần source app Android**, nên dùng được cho bất kỳ app nào.

```bash
# chạy được từ bất kỳ thư mục nào; mặc định dùng chính .venv/.env của repo này
sh apps/test_console/run.sh            # http://127.0.0.1:8787

ARTEMIS_WEB_PORT=8788 sh apps/test_console/run.sh          # đổi cổng
TEST_APP_PACKAGE=com.example.app sh apps/test_console/run.sh # đổi app mặc định
ARTEMIS_DIR=/path/to/checkout sh apps/test_console/run.sh   # drive checkout khác
LOCAL_LLM=1 sh apps/test_console/run.sh                     # Ollama, 0đ
```

- **Tính năng**: timeline ảnh Trước → Sau với nhãn hành động tiếng Việt, kết luận ĐẠT / KHÔNG ĐẠT /
  ĐÃ DỪNG, màn hình trực tiếp + log realtime, lịch sử từng lần chạy, kiểm tra LLM, chặn ngân sách,
  và nút **Dừng** huỷ thật trên daemon.
- **App đích**: ô **Package** trên UI (mặc định `com.example.app`), hoặc `TEST_APP_PACKAGE`. 7 kịch bản
  mẫu là prompt chung (mở app, đăng nhập, danh sách, bật/tắt, giọng nói, thêm thiết bị, thông báo);
  với app khác dùng nút **Prompt tự do**.
- **Cài app**: console **không cài app giúp bạn** — cài trước bằng `adb install -r <apk>` (hoặc
  `./gradlew installDebug`), app React Native bản debug thì phải chạy Metro + `adb reverse tcp:8081`.
  Hướng dẫn đầy đủ: [`apps/test_console/README.md`](./apps/test_console/README.md#cài-app-lên-điện-thoại).
- **Không kèm dữ liệu riêng**: không có package/app cụ thể, tài khoản, serial hay key nào trong repo.
- **Proxy LLM**: console mở `/llm/v1` tại cổng 8787. Nếu `.env` của Artemis đặt
  `OPENAI_BASE_URL=http://127.0.0.1:8787/llm/v1` thì console phải chạy, nếu không task sẽ không gọi
  được model (ngược lại, đặt thẳng `OPENAI_BASE_URL=https://api.openai.com/v1` nếu không cần chặn ngân sách).
- **Khoá truy cập API**: `/llm/v1` yêu cầu `Authorization: Bearer <CONSOLE_ACCESS_KEY>` (mặc định
  `21092003`). Máy chỉ chạy ARTEMIS (không chạy proxy) đặt `OPENAI_API_KEY=<khoá>` +
  `OPENAI_BASE_URL=http://<ip-máy-chạy-console>:8787/llm/v1` — key LLM thật chỉ nằm trên máy chạy console.
  Chế độ `LOCAL_LLM=1` (Ollama) miễn khoá.
- **Knowledge — Workflow RAG**: tester nhập prompt gốc, console gọi Google Embedding, tìm workflow
  gần nhất trong PostgreSQL rồi trả về **prompt hoàn chỉnh** (prompt gốc + hướng dẫn từng bước).
  Chỉ máy chạy console cần database và khoá embedding; máy tester chỉ cần ARTEMIS và điện thoại.
  Xem `apps/test_console/README.md`.
- **Tài liệu**: [`apps/test_console/README.md`](./apps/test_console/README.md).
- **Wrapper CLI**: [`scripts/artemis.sh`](./scripts/artemis.sh) (tự thêm `adb` vào PATH, tự dùng
  `.venv` hoặc `uv run`).

## 18. ADB server từ xa (máy DEV + máy tester)

Mô hình: **máy DEV** chạy ARTEMIS backend + Web UI, **máy tester** cắm điện thoại qua USB và chạy
ADB server mở ra LAN. Mọi thao tác thiết bị (screenshot, tap, swipe, input, đọc UI, UIAutomator2)
đều đi qua ADB server đó; không có đường nào tự rơi về `localhost`.

### 17.1. Máy tester (nơi cắm điện thoại)

```bash
# Bật USB debugging trên điện thoại, cắm cáp, chấp nhận "Allow USB debugging"
adb devices -l                      # phải thấy trạng thái "device"

# Chạy ADB server nghe trên LAN (foreground, giữ terminal này)
adb kill-server
adb -a -P 5037 nodaemon server

# Chỉ mở firewall cho máy DEV (ví dụ ufw)
sudo ufw allow from <DEV_IP> to any port 5037 proto tcp
```

Tuyệt đối **không** mở cổng 5037 ra Internet — ADB không có xác thực kênh truyền, chỉ dùng LAN/VPN.

### 17.2. Máy DEV (chạy ARTEMIS)

```bash
cd ~/Developer/artermis-pro
cat >> .env <<'EOF'
ADB_HOST=<TESTER_IP>
ADB_PORT=5037
EOF

uv run artemis adb check                 # kết nối + liệt kê thiết bị (exit != 0 nếu lỗi)
uv run artemis adb devices               # bảng serial / state / model
uv run artemis adb check --json          # cho script/CI
uv run artemis doctor                    # kiểm tra tổng thể (đã gồm endpoint + thiết bị)

# Web UI cho tester truy cập qua LAN
uv run artemis ui --host 0.0.0.0 --port 8000
# → tester mở http://<DEV_IP>:8000 (URL LAN được in ra khi khởi động)
```

Admin Console cho phép Host là **IP literal** (ví dụ `http://192.168.2.79:8000`). Nếu tester truy cập
bằng tên miền nội bộ, khai báo thêm để không bị chặn 403:

```bash
# .env
ARTEMIS_ALLOWED_HOSTS=dev-box.local,dev-box
```

Có thể override tạm cho một lệnh mà không sửa `.env`:

```bash
uv run artemis adb devices --host 10.0.0.25 --port 5037
ADB_HOST=10.0.0.25 uv run artemis run "Mở Settings, cho biết mức pin" -p flash
```

### 17.3. Kiến trúc & giới hạn cần biết

| Hạng mục | Trạng thái với ADB remote |
| --- | --- |
| `adb devices`, screenshot (`exec-out screencap`), tap/swipe/input, `dumpsys`, `run_adb_command` | ✅ đi qua `adb -H <host> -P <port>` |
| UIAutomator2 (đọc UI) | ✅ adbutils/uiautomator2 được rebind sang endpoint (`ANDROID_ADB_SERVER_HOST/PORT`) |
| Device list trong Web UI, live screen, replay | ✅ dùng endpoint đã cấu hình |
| Accessibility Helper | ⚠️ **tự động tắt** ở chế độ remote: `adb forward` chỉ bind trên loopback của máy chạy ADB server, nên máy DEV không gọi được. ARTEMIS tự chuyển sang UIAutomator2. Muốn dùng helper phải có SSH tunnel (`ssh -L <port>:127.0.0.1:<port>`) rồi đặt `ARTEMIS_ALLOW_REMOTE_HELPER=1` |
| Ghi video bằng scrcpy | ⚠️ scrcpy tự gọi `adb` của máy DEV → cần wrapper (xem 17.4) |
| `adb start-server` / `kill-server` | ⚠️ chỉ chạy khi endpoint là local; với remote thì ARTEMIS chỉ kiểm tra kết nối (không tạo server mới trên máy DEV) |
| Đổi endpoint từ Admin Console | Mặc định chỉ cho client local; muốn đổi từ xa đặt `ARTEMIS_ALLOW_REMOTE_ADB_CONFIGURATION=true` |

### 17.4. scrcpy với ADB remote (tuỳ chọn)

```bash
cat > ~/bin/adb-remote <<'EOF'
#!/usr/bin/env bash
exec adb -H "$ADB_HOST" -P "$ADB_PORT" "$@"
EOF
chmod +x ~/bin/adb-remote
export ADB="$HOME/bin/adb-remote"     # scrcpy dùng biến ADB này
```

### 17.5. Kiểm thử end-to-end

```bash
# 1) Endpoint + thiết bị
uv run artemis adb check

# 2) Đọc UI + screenshot + thao tác (task thật qua LLM)
uv run artemis run "Mở Settings, vào Battery và cho tôi biết mức pin hiện tại" -p flash

# 3) Kiểm tra từng thao tác phần cứng (không cần LLM)
SERIAL=$(adb -H $ADB_HOST -P $ADB_PORT devices | awk 'NR==2{print $1}')
adb -H $ADB_HOST -P $ADB_PORT -s $SERIAL exec-out screencap -p > /tmp/screen.png
adb -H $ADB_HOST -P $ADB_PORT -s $SERIAL shell input tap 540 1200
adb -H $ADB_HOST -P $ADB_PORT -s $SERIAL shell input swipe 540 1600 540 600 300
adb -H $ADB_HOST -P $ADB_PORT -s $SERIAL shell input text "artemis"
adb -H $ADB_HOST -P $ADB_PORT -s $SERIAL shell uiautomator dump /sdcard/ui.xml

# 4) Rút cáp USB khi đang chạy task → ARTEMIS báo device offline/unauthorized rõ ràng,
#    task dừng và nhả device lock; cắm lại rồi chạy lại.
```


