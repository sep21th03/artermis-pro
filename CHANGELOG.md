# Changelog — ARTEMIS Pro

## [0.3.1] — 2026-10-21

### Added

- `artemis llm check [--json] [--timeout]`: gọi model một lần và in **nguyên văn body** khi lỗi
  (401/404/429/HTML) kèm gợi ý sửa, để đọc được các "JSON error" của provider/proxy.
- `POST /api/budget/reset` trên Test Console để gỡ trạng thái `429 budget_exceeded`.

### Fixed

- Test Console proxy khoá vĩnh viễn sau 480s với các lệnh CLI: mọi request CLI dùng chung một task
  `external` và timer không bao giờ reset → sau 8 phút mọi request trả
  `{"error":{"code":"budget_exceeded"}}`. Nay task `external` tự reset sau
  `BUDGET_EXTERNAL_IDLE_RESET_SECONDS` (mặc định 120s) không có request.


## [0.3.0] — 2026-10-21

### Added

- **Remote ADB server support** (DEV machine runs ARTEMIS, tester machine runs
  `adb -a -P 5037 nodaemon server` with the phone on USB):
  - `artemis/runtime/adb_status.py`: endpoint reachability (TCP) + `adb devices -l`
    inventory with `device` / `unauthorized` / `offline` states and one actionable
    hint per failure mode.
  - `artemis adb devices` / `artemis adb check` (with `--host`, `--port`, `--json`).
  - `AdbEndpoint.apply_to_environment` now also exports `ANDROID_ADB_SERVER_HOST/PORT`
    (the names adbutils/uiautomator2 actually read) and `apply_current_endpoint()`
    rebinds the module-level `adbutils.adb` client.
  - `apply_current_endpoint()` is applied at CLI, MCP server and Admin Console startup.
  - `artemis doctor` reports the endpoint, its reachability and a remote-specific fix list.
  - `artemis ui --host 0.0.0.0` prints the LAN URLs testers can open.

### Fixed

- Device enumeration (`device_pool`), live screen + device list (`device_stream_service`),
  replay (`replay_manager`), `run_adb_command` (`command_tool`), `dumpsys` display state
  (`utils/video.py`) and MCP device utils no longer run bare `adb`: every call goes through
  `adb -H <ADB_HOST> -P <ADB_PORT>`, so nothing silently falls back to `localhost`.
- `adb start-server` / `kill-server` only run for a local endpoint; with a remote server
  ARTEMIS just verifies connectivity instead of spawning a server on the DEV machine.

### Changed

- Accessibility Helper is skipped automatically with a remote ADB server (`adb forward`
  binds on the tester's loopback) and falls back to UIAutomator2; override with
  `ARTEMIS_ALLOW_REMOTE_HELPER=1` when the forward port is reachable (SSH tunnel).
- scrcpy video recording logs a warning under a remote endpoint: scrcpy needs
  `ADB` to point at a wrapper script (documented in README 17.4).

## [0.2.0] — 2026-10-21

### Added

- **Mobile Test Console** (web UI tiếng Việt cho tester) nằm ngay trong repo tại
  `apps/test_console/`: `server.py`, `static/index.html`, `run.sh`, `README.md`,
  `Modelfile.artemis-local`. Console **self-contained**: mặc định dùng chính
  `.venv` / `.env` / `config/artemis.jsonc` của repo (override bằng `ARTEMIS_DIR`),
  nên clone ở đâu chạy cũng được.
- `scripts/artemis.sh` — wrapper CLI tự thêm `adb` vào PATH, dùng `.venv/bin/artemis`
  hoặc fallback `uv run`.
- Mục 16 trong `README.md` hướng dẫn chạy console, đổi cổng/package, cảnh báo proxy LLM.

### Changed

- `apps/test_console/server.py`: thêm `artemis_launcher()` — dùng `.venv/bin/artemis`
  nếu có, fallback `uv run --project <repo> artemis`; `/api/status` báo `artemis_ok`
  theo launcher này. Package mặc định đọc từ `TEST_APP_PACKAGE` (mặc định `com.example.app`).
- `apps/test_console/run.sh`: `ARTEMIS_DIR` mặc định là repo root, fallback `uv run`
  khi chưa có `.venv`.
- Gốc repo `.gitignore` thêm `apps/test_console/data/` (log, history, budget là runtime).

## [0.1.0] — 2026-10-21

Bản đầu tiên được đóng gói từ workspace `Developer`.

### Fixed

- **Ctrl+C / Stop không dừng agent**: `artemis run` chỉ submit task vào Artemis Daemon rồi chờ,
  nên tín hiệu dừng chỉ giết tiến trình CLI và daemon vẫn tiếp tục điều khiển điện thoại.
  Cầu nối `SIGTERM → KeyboardInterrupt` nay được cài **trước** khối routing daemon
  (`artemis/interfaces/cli/commands/run.py`), để mọi tín hiệu dừng đi vào nhánh
  `except KeyboardInterrupt` → `stop_task_on_daemon(session_id)` → `SystemExit(130)`.

### Changed

- `config/artemis.jsonc`: model mặc định và hầu hết node chuyển sang `provider: openai`
  với gateway OpenAI-compatible (`model: ts/gemini-3.5-flash-lite`); cần đặt `OPENAI_BASE_URL` + `OPENAI_API_KEY`.
- `profiles.flash.max_turns` = 25, `explorer_mode` = `pro`.
- Tắt `flash.step_summarizer` và block `context` (chunk compression) vì phụ thuộc Gemini key native.
- `operator.thinking_level` = `low`, `include_thoughts` = `false` để giảm token mỗi bước.

### Docs

- `README.md`: hướng dẫn tiếng Việt đầy đủ (cài đặt → cấu hình → chạy → MCP → dừng task → troubleshooting).
- README gốc tiếng Anh đổi tên thành `README_EN.md`, bản tiếng Trung giữ nguyên `README_CN.md`,
  cập nhật liên kết chéo và URL clone của repo này.
