#!/usr/bin/env bash
# Cài ARTEMIS lên MÁY TESTER — máy chỉ cắm điện thoại, chạy console ở máy khác.
#
# Máy tester KHÔNG cần PostgreSQL, KHÔNG cần khoá embedding, KHÔNG cần key LLM thật:
# mọi request LLM đi qua proxy của console và mọi truy vấn kiến thức đi qua API RAG
# của console, cả hai đều chỉ cần một **khoá truy cập** dùng chung.
#
#   sh scripts/install_tester.sh --console https://sep21th03.com/artemis --key 21092003
#   sh scripts/install_tester.sh --console http://192.168.1.10:8787 --key 21092003
#
#   --dry-run      chỉ IN .env sẽ ghi, không đụng đĩa (dùng để xem trước)
#   --force        ghi đè .env đang có (mặc định: dừng lại và báo, .env cũ được giữ)
#   --no-sync      bỏ qua `uv sync` (máy đã cài dependency)
#   --no-verify    bỏ qua bước thử gọi console ở cuối
#   --env-file F   ghi .env vào đường dẫn khác (mặc định <repo>/.env)
#   -h | --help    in trợ giúp
#
# Sau khi chạy xong: cắm điện thoại (đã bật USB debugging + bấm Allow), rồi
#   sh apps/test_console/run.sh
# Chạy trên chính máy đó thì console của máy đó là `http://127.0.0.1:8787`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# adb không nằm trên PATH mặc định của macOS (Android Studio cài vào Library).
export PATH="$HOME/Library/Android/sdk/platform-tools:$HOME/.local/bin:$PATH"

CONSOLE_URL=""
ACCESS_KEY=""
ENV_FILE="$REPO_ROOT/.env"
DRY_RUN=0
FORCE=0
DO_SYNC=1
DO_VERIFY=1

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

die() { echo "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --console)   CONSOLE_URL="${2:-}"; shift 2 ;;
    --key)       ACCESS_KEY="${2:-}"; shift 2 ;;
    --env-file)  ENV_FILE="${2:-}"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --force)     FORCE=1; shift ;;
    --no-sync)   DO_SYNC=0; shift ;;
    --no-verify) DO_VERIFY=0; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "Tham số lạ: $1 (xem --help)" ;;
  esac
done

[ -n "$CONSOLE_URL" ] || die "Thiếu --console <url console>. Ví dụ: --console https://sep21th03.com/artemis"
[ -n "$ACCESS_KEY" ] || die "Thiếu --key <khoá truy cập>. Khoá này do quản trị console đưa (mặc định 21092003)."

# Chuẩn hoá URL: bỏ '/' cuối để ghép đường dẫn không thành '//llm/v1'.
CONSOLE_URL="${CONSOLE_URL%/}"

echo "== Cài ARTEMIS cho máy tester =="
echo "   repo    : $REPO_ROOT"
echo "   console : $CONSOLE_URL"
echo "   .env    : $ENV_FILE"
echo

# --- 1. Công cụ cần có ---------------------------------------------------------
missing=()
command -v adb >/dev/null 2>&1 || missing+=("adb  (brew install android-platform-tools, hoặc Android Studio → SDK Manager → Platform-Tools)")
if [ ! -x "$REPO_ROOT/.venv/bin/python" ] && ! command -v uv >/dev/null 2>&1; then
  missing+=("uv   (brew install uv, hoặc curl -LsSf https://astral.sh/uv/install.sh | sh)")
fi
if [ "${#missing[@]}" -gt 0 ]; then
  echo "Thiếu công cụ bắt buộc:" >&2
  # ${missing[@]+...}: bash 3.2 (mặc định trên macOS) + `set -u` coi mảng rỗng là biến chưa đặt.
  printf '  - %s\n' ${missing[@]+"${missing[@]}"} >&2
  exit 1
fi

# --- 2. Dependency Python ------------------------------------------------------
if [ "$DO_SYNC" = "1" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    echo "[1/4] (bỏ qua) uv sync"
  elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    echo "[1/4] Đã có .venv — bỏ qua uv sync (dùng --no-sync để im lặng hoàn toàn)."
  else
    echo "[1/4] uv sync (lần đầu sẽ tải dependency)…"
    (cd "$REPO_ROOT" && uv sync)
  fi
else
  echo "[1/4] (bỏ qua) uv sync — theo --no-sync"
fi

# --- 3. Ghi .env ---------------------------------------------------------------
# Máy tester chỉ 4 dòng. Cố ý KHÔNG ghi key LLM thật, KHÔNG ghi ARTEMIS_KB_DSN:
# tester không đọc database, không cần SSH tunnel.
ENV_BODY="$(cat <<EOF
# Máy tester — sinh bởi scripts/install_tester.sh ($(date '+%Y-%m-%d %H:%M'))
# Không có key LLM thật ở đây: console giữ key và thay vào mỗi request.
OPENAI_API_KEY=$ACCESS_KEY
OPENAI_BASE_URL=$CONSOLE_URL/llm/v1
ARTEMIS_KB_API=$CONSOLE_URL
ARTEMIS_KB_API_KEY=$ACCESS_KEY
EOF
)"

if [ "$DRY_RUN" = "1" ]; then
  echo "[2/4] --dry-run: .env sẽ ghi như sau (chưa đụng đĩa):"
  echo "----------------------------------------"
  printf '%s\n' "$ENV_BODY"
  echo "----------------------------------------"
else
  if [ -f "$ENV_FILE" ] && [ "$FORCE" != "1" ]; then
    die "$ENV_FILE đã tồn tại — giữ nguyên. Dùng --force để ghi đè (bản cũ sẽ được sao lưu .bak.<thời-gian>)."
  fi
  if [ -f "$ENV_FILE" ]; then
    backup="$ENV_FILE.bak.$(date '+%Y%m%d-%H%M%S')"
    cp "$ENV_FILE" "$backup"
    echo "[2/4] Đã sao lưu .env cũ → $backup"
  fi
  mkdir -p "$(dirname "$ENV_FILE")"
  printf '%s\n' "$ENV_BODY" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "[2/4] Đã ghi $ENV_FILE (600)."
fi

# --- 4. Thử gọi console --------------------------------------------------------
verify() {
  local label="$1" url="$2" method="$3" body="$4" want="$5" code
  if [ "$method" = "POST" ]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 20 -X POST \
      -H "Authorization: Bearer $ACCESS_KEY" -H 'Content-Type: application/json' \
      -H 'User-Agent: ARTEMIS/1.0' -d "$body" "$url" 2>/dev/null || echo 000)"
  else
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 20 \
      -H "Authorization: Bearer $ACCESS_KEY" -H 'User-Agent: ARTEMIS/1.0' \
      "$url" 2>/dev/null || echo 000)"
  fi
  if [ "$code" = "$want" ]; then
    echo "      ✅ $label → $code"
    return 0
  fi
  echo "      ❌ $label → $code (mong đợi $want)" >&2
  return 1
}

if [ "$DO_VERIFY" = "1" ] && [ "$DRY_RUN" != "1" ]; then
  echo "[3/4] Thử gọi console…"
  ok=1
  verify "LLM proxy /llm/v1/models" "$CONSOLE_URL/llm/v1/models" GET "" 200 || ok=0
  verify "API RAG /api/knowledge/context" "$CONSOLE_URL/api/knowledge/context" POST \
    '{"goal":"kiểm tra kết nối","app_package":"com.iot.hunonic"}' 200 || ok=0
  if [ "$ok" != "1" ]; then
    echo "   → Console chưa trả lời được. Kiểm tra: URL đúng chưa, khoá truy cập đúng chưa," >&2
    echo "     máy chạy console có mở cổng không (ARTEMIS_WEB_HOST=0.0.0.0 khi trong LAN)." >&2
    echo "     .env vẫn đã ghi — sửa URL/khoá rồi chạy lại script (kèm --force)." >&2
    exit 1
  fi
else
  echo "[3/4] (bỏ qua) thử gọi console"
fi

# --- 5. Việc còn lại của người dùng -------------------------------------------
echo "[4/4] Xong. Việc còn lại:"
echo "      export PATH=\"\$HOME/Library/Android/sdk/platform-tools:\$PATH\""
echo "      adb devices -l          # phải thấy 'device' (không phải 'unauthorized')"
echo "      cd $REPO_ROOT && sh apps/test_console/run.sh"
echo "   Giao diện: http://127.0.0.1:8787 (run.sh tự mở)"
