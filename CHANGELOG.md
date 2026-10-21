# Changelog — ARTEMIS Pro

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
