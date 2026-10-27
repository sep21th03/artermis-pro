#!/usr/bin/env bash
# Chạy Mobile Test Console ở http://127.0.0.1:8787
# Chạy được từ bất kỳ thư mục nào: sh apps/test_console/run.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Console nằm trong chính repo ARTEMIS: apps/test_console -> repo root
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$SCRIPT_DIR"
if [ -z "${ARTEMIS_DIR:-}" ]; then
  # Ưu tiên repo hiện tại nếu đã có runtime, rồi tới checkout cài kiểu cũ.
  if [ -x "$REPO_ROOT/.venv/bin/python" ] || [ -f "$REPO_ROOT/.env" ]; then
    ARTEMIS_DIR="$REPO_ROOT"
  elif [ -x "$HOME/Developer/artemis/.venv/bin/python" ]; then
    ARTEMIS_DIR="$HOME/Developer/artemis"
  else
    ARTEMIS_DIR="$REPO_ROOT"
  fi
fi
export ARTEMIS_DIR
export PATH="$HOME/Library/Android/sdk/platform-tools:$HOME/.local/bin:$PATH"

# --- LLM local (Ollama) -------------------------------------------------------
# Bật bằng: LOCAL_LLM=1 sh apps/test_console/run.sh
# Console sẽ chuyển mọi request sang Ollama và tính 0đ (không cần mạng, không key).
if [ "${LOCAL_LLM:-0}" = "1" ]; then
  export ARTEMIS_UPSTREAM_BASE_URL="${ARTEMIS_UPSTREAM_BASE_URL:-http://127.0.0.1:11434/v1}"
  export ARTEMIS_LOCAL_MODEL="${ARTEMIS_LOCAL_MODEL:-artemis-local}"
  # Model local chậm hơn cloud ~10x và luôn "thinking": nới hạn mức để task
  # không bị watchdog cắt giữa đường.
  export BUDGET_MAX_OUTPUT_TOKENS="${BUDGET_MAX_OUTPUT_TOKENS:-1536}"
  export BUDGET_MAX_TASK_SECONDS="${BUDGET_MAX_TASK_SECONDS:-1800}"
  export BUDGET_MAX_REQUESTS="${BUDGET_MAX_REQUESTS:-80}"
  export TASK_IDLE_TIMEOUT_SECONDS="${TASK_IDLE_TIMEOUT_SECONDS:-240}"
  if ! curl -sf --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "CẢNH BÁO: Ollama chưa chạy (mở app Ollama rồi thử lại)." >&2
  fi
  echo "LLM local: $ARTEMIS_UPSTREAM_BASE_URL · model $ARTEMIS_LOCAL_MODEL"
fi

PYTHON="$ARTEMIS_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  if command -v uv >/dev/null 2>&1; then
    echo "Chưa có .venv trong $ARTEMIS_DIR — dùng 'uv run' (lần đầu sẽ cài dependency)." >&2
    PYTHON_CMD=(uv run --project "$ARTEMIS_DIR" python)
  else
    echo "Không tìm thấy Artemis runtime tại $ARTEMIS_DIR/.venv/bin/python" >&2
    echo "Chạy 'uv sync' trong repo, hoặc đặt ARTEMIS_DIR trỏ tới checkout ARTEMIS khác." >&2
    exit 1
  fi
else
  PYTHON_CMD=("$PYTHON")
fi

if [ "${NO_OPEN:-0}" != "1" ] && command -v open >/dev/null 2>&1; then
  ( sleep 1; open "http://127.0.0.1:${ARTEMIS_WEB_PORT:-8787}" ) >/dev/null 2>&1 &
fi

exec "${PYTHON_CMD[@]}" server.py


# --- LLM local (Ollama) -------------------------------------------------------
# Bật bằng: LOCAL_LLM=1 sh tools/artemis-web/run.sh
# Console sẽ chuyển mọi request sang Ollama và tính 0đ (không cần mạng, không key).
if [ "${LOCAL_LLM:-0}" = "1" ]; then
  export ARTEMIS_UPSTREAM_BASE_URL="${ARTEMIS_UPSTREAM_BASE_URL:-http://127.0.0.1:11434/v1}"
  export ARTEMIS_LOCAL_MODEL="${ARTEMIS_LOCAL_MODEL:-artemis-local}"
  # Model local chậm hơn cloud ~10x và luôn "thinking": nới hạn mức để task
  # không bị watchdog cắt giữa đường.
  export BUDGET_MAX_OUTPUT_TOKENS="${BUDGET_MAX_OUTPUT_TOKENS:-1536}"
  export BUDGET_MAX_TASK_SECONDS="${BUDGET_MAX_TASK_SECONDS:-1800}"
  export BUDGET_MAX_REQUESTS="${BUDGET_MAX_REQUESTS:-80}"
  export TASK_IDLE_TIMEOUT_SECONDS="${TASK_IDLE_TIMEOUT_SECONDS:-240}"
  if ! curl -sf --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "CẢNH BÁO: Ollama chưa chạy (mở app Ollama rồi thử lại)." >&2
  fi
  echo "LLM local: $ARTEMIS_UPSTREAM_BASE_URL · model $ARTEMIS_LOCAL_MODEL"
fi

# --- Knowledge base: bảo đảm tunnel tới PostgreSQL trên VPS --------------------
# KB nằm ở PostgreSQL trên VPS, chỉ mở qua SSH tunnel. Tunnel hay đứt (mạng, hoặc tiến
# trình ssh bị kill) và khi đó console báo "Không kết nối được PostgreSQL: port 5433" —
# tester phải nhớ chạy lại lệnh. Ở đây tự mở lại trước khi khởi động.
#
#   KB_TUNNEL=0 sh apps/test_console/run.sh   # tắt bước này (ví dụ máy chạy vai server)
#   sh scripts/kb_tunnel.sh status|start|restart|ensure
if [ "${KB_TUNNEL:-1}" = "1" ] && [ -f "$REPO_ROOT/scripts/kb_tunnel.sh" ]; then
  if sh "$REPO_ROOT/scripts/kb_tunnel.sh" ensure >/dev/null 2>&1; then
    echo "Knowledge base: tunnel sẵn sàng (127.0.0.1:${KB_LOCAL_PORT:-5433})."
  else
    echo "CẢNH BÁO: chưa mở được tunnel tới Knowledge base." >&2
    echo "         Console vẫn chạy nhưng tab Knowledge sẽ báo KB không dùng được." >&2
    echo "         Kiểm tra khoá SSH (~/.ssh/greencloud) rồi chạy: sh scripts/kb_tunnel.sh start" >&2
  fi
fi

if [ ! -x "$ARTEMIS_DIR/.venv/bin/python" ]; then
  echo "Không tìm thấy Artemis tại $ARTEMIS_DIR" >&2
  echo "Đặt ARTEMIS_DIR hoặc xem Documents/artemis.md." >&2
  exit 1
fi

if [ "${NO_OPEN:-0}" != "1" ] && command -v open >/dev/null 2>&1; then
  ( sleep 1; open "http://127.0.0.1:8787" ) >/dev/null 2>&1 &
fi

exec "$ARTEMIS_DIR/.venv/bin/python" server.py
