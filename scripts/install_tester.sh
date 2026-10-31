#!/usr/bin/env bash
# Cài ARTEMIS cho một máy chạy console: sinh `.env` **đầy đủ mọi biến**, chỉ thay đúng
# những biến thuộc về máy này — nên không phải chép `.env` đi gửi từng người.
#
#   sh scripts/install_tester.sh --console https://sep21th03.com/artemis --key 21092003
#   sh scripts/install_tester.sh --console http://192.168.1.10:8787 --key 21092003
#
# Mẫu `.env` lấy theo thứ tự: `--from <file>` → `.env` của repo (nếu có) → `.env.example`.
# Trên mẫu đó script ghi đè 5 biến (OPENAI_API_KEY, OPENAI_BASE_URL, CONSOLE_ACCESS_KEY,
# ARTEMIS_KB_API, ARTEMIS_KB_API_KEY) và **bỏ giá trị** các khoá chỉ thuộc máy chạy console
# (GEMINI_API_KEY, ARTEMIS_KB_DSN, KB_ADMIN_PASSWORD, KB_AUTH_SECRET, KB_VPS_KEY…) nên file
# sinh ra gửi cho người khác được: máy nhận không có key LLM thật, không có mật khẩu DB.
#
#   --from FILE      lấy mẫu từ FILE (mặc định: `.env` của repo, không có thì `.env.example`)
#   --minimal        chỉ ghi 4 dòng tối thiểu thay vì cả file
#   --keep-secrets   GIỮ giá trị khoá bí mật trong mẫu (chỉ khi máy này cũng chạy console;
#                    gửi file này cho người khác là lộ key)
#   --dry-run        in `.env` sẽ ghi ra màn hình, không đụng đĩa
#   --force          ghi đè `.env` đang có (mặc định dừng lại; bản cũ được sao lưu `.bak.<giờ>`)
#   --no-sync        bỏ qua `uv sync` (máy đã cài dependency)
#   --no-verify      bỏ qua bước thử gọi console ở cuối
#   --env-file F     ghi `.env` vào đường dẫn khác (mặc định `<repo>/.env`; không được trùng mẫu)
#   -h | --help      in trợ giúp này
#
# Sau khi chạy: cắm điện thoại (bật USB debugging + bấm Allow) rồi `sh apps/test_console/run.sh`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# adb không nằm trên PATH mặc định của macOS (Android Studio cài vào Library).
export PATH="$HOME/Library/Android/sdk/platform-tools:$HOME/.local/bin:$PATH"

CONSOLE_URL=""
ACCESS_KEY=""
ENV_FILE="$REPO_ROOT/.env"
FROM_FILE=""
MINIMAL=0
KEEP_SECRETS=0
DRY_RUN=0
FORCE=0
DO_SYNC=1
DO_VERIFY=1

#: Biến thuộc về máy này — ghi đè giá trị của mẫu.
OVERRIDE_KEYS="OPENAI_API_KEY OPENAI_BASE_URL CONSOLE_ACCESS_KEY ARTEMIS_KB_API ARTEMIS_KB_API_KEY"
#: Khoá chỉ dùng trên máy chạy console — bỏ **giá trị** khi sinh .env cho máy khác.
SECRET_KEYS="GEMINI_API_KEY GOOGLE_API_KEY ARTEMIS_KB_DSN KB_ADMIN_PASSWORD KB_AUTH_SECRET KB_VPS_KEY"

usage() {
  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
}

die() { echo "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --console)   CONSOLE_URL="${2:-}"; shift 2 ;;
    --key)       ACCESS_KEY="${2:-}"; shift 2 ;;
    --env-file)  ENV_FILE="${2:-}"; shift 2 ;;
    --from)      FROM_FILE="${2:-}"; shift 2 ;;
    --minimal)   MINIMAL=1; shift ;;
    --keep-secrets) KEEP_SECRETS=1; shift ;;
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
[ -z "$FROM_FILE" ] || [ -f "$FROM_FILE" ] || die "Không thấy mẫu --from $FROM_FILE"

# Chuẩn hoá URL: bỏ '/' cuối để ghép đường dẫn không thành '//llm/v1'.
CONSOLE_URL="${CONSOLE_URL%/}"

# Mẫu để dựng .env: --from → .env của repo (máy đang cấu hình) → .env.example (máy mới clone).
if [ -n "$FROM_FILE" ]; then
  BASE_FILE="$FROM_FILE"
elif [ -f "$REPO_ROOT/.env" ]; then
  BASE_FILE="$REPO_ROOT/.env"
else
  BASE_FILE="$REPO_ROOT/.env.example"
fi
[ -f "$BASE_FILE" ] || die "Không thấy mẫu .env: $BASE_FILE (chỉ định bằng --from <file>)"

# Đường dẫn tuyệt đối để so ENV_FILE với BASE_FILE (macOS không có `realpath`).
abspath() {
  local d b
  d="$(dirname "$1")"; b="$(basename "$1")"
  if [ -d "$d" ]; then (cd "$d" && printf '%s/%s' "$(pwd)" "$b"); else printf '%s' "$1"; fi
}

# Mẫu trùng đích ⇒ ghi vào là tự che chính .env thật của máy này (mất key thật, mất mật khẩu DB).
# Đây là lỗi dễ mắc nhất khi chạy đúng lệnh này ngay trên máy đang cấu hình.
if [ "$DRY_RUN" != "1" ] && [ "$(abspath "$ENV_FILE")" = "$(abspath "$BASE_FILE")" ]; then
  die "$ENV_FILE vừa là MẪU vừa là ĐÍCH — ghi như vậy sẽ mất key thật của máy này.
  Muốn sinh .env để gửi cho máy khác: --env-file <đường dẫn mới> (hoặc --dry-run để xem trước).
  Muốn cài cho chính máy này: chạy trên máy đó (bản clone mới chưa có .env)."
fi

if [ "$MINIMAL" = "1" ]; then
  MODE="4 dòng tối thiểu"
else
  MODE="đầy đủ (mẫu: ${BASE_FILE#$REPO_ROOT/})"
fi

echo "== Cài ARTEMIS cho máy này =="
echo "   repo    : $REPO_ROOT"
echo "   console : $CONSOLE_URL"
echo "   .env    : $ENV_FILE  [$MODE]"
if [ "$KEEP_SECRETS" = "1" ]; then
  echo "   ⚠ --keep-secrets: khoá bí mật được GIỮ NGUYÊN — đừng gửi file này cho ai." >&2
fi
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

# --- 3. Dựng .env --------------------------------------------------------------
# Giá trị của 5 biến thuộc máy này; mọi biến khác giữ nguyên như mẫu ⇒ máy nhận có cùng
# model, cùng chuỗi xoay quota, cùng hạn mức như máy đang cấu hình.
OV_FILE="$(mktemp)"
TMP_OUT="$(mktemp)"
{
  printf 'OPENAI_API_KEY=%s\n' "$ACCESS_KEY"
  printf 'OPENAI_BASE_URL=%s\n' "$CONSOLE_URL/llm/v1"
  printf 'CONSOLE_ACCESS_KEY=%s\n' "$ACCESS_KEY"
  printf 'ARTEMIS_KB_API=%s\n' "$CONSOLE_URL"
  printf 'ARTEMIS_KB_API_KEY=%s\n' "$ACCESS_KEY"
} > "$OV_FILE"

# Đọc mẫu, ghi ra stdout: (1) thay 5 biến của máy này, (2) bỏ GIÁ TRỊ khoá bí mật,
# (3) thêm vào cuối những biến mẫu không có, đúng thứ tự OVERRIDE_KEYS.
# Giá trị truyền qua file (không qua -v) nên '/', '#', '&' trong URL/khoá không phải escape.
build_env() {
  awk -v ovf="$OV_FILE" -v secrets="$SECRET_KEYS" -v keep="$KEEP_SECRETS" '
    function key_of(s) { return (s ~ /^[A-Za-z_][A-Za-z0-9_]*=/) ? substr(s, 1, index(s, "=") - 1) : "" }
    BEGIN {
      n = split(secrets, s, " "); for (i = 1; i <= n; i++) sec[s[i]] = 1
      while ((getline line < ovf) > 0) {
        k = key_of(line)
        if (k != "") { ov[k] = substr(line, index(line, "=") + 1); order[++m] = k }
      }
      close(ovf)
    }
    {
      k = key_of($0)
      if (k != "" && k in ov) { print k "=" ov[k]; seen[k] = 1; next }
      if (k != "" && k in sec && keep != "1") {
        print "# " k "=   # bỏ giá trị khi sinh .env cho máy khác (--keep-secrets để giữ)"
        next
      }
      print
    }
    END {
      for (i = 1; i <= m; i++) {
        k = order[i]
        if (!(k in seen)) {
          if (!head) { print ""; print "# ── Biến của máy này (scripts/install_tester.sh thêm) ──"; head = 1 }
          print k "=" ov[k]
        }
      }
    }
  ' "$BASE_FILE"
}

if [ "$MINIMAL" = "1" ]; then
  cat > "$TMP_OUT" <<EOF
# Máy tester — sinh bởi scripts/install_tester.sh ($(date '+%Y-%m-%d %H:%M'))
# Không có key LLM thật ở đây: console giữ key và thay vào mỗi request.
OPENAI_API_KEY=$ACCESS_KEY
OPENAI_BASE_URL=$CONSOLE_URL/llm/v1
ARTEMIS_KB_API=$CONSOLE_URL
ARTEMIS_KB_API_KEY=$ACCESS_KEY
EOF
else
  build_env > "$TMP_OUT"
fi

LINES="$(wc -l < "$TMP_OUT" | tr -d ' ')"
KEYS="$(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=' "$TMP_OUT" || true)"
DROPPED_KEYS="$(sed -n 's/^# \([A-Za-z_][A-Za-z0-9_]*\)= *# bỏ giá trị.*/\1/p' "$TMP_OUT" | paste -sd, - | sed 's/,/, /g')"

if [ "$DRY_RUN" = "1" ]; then
  echo "[2/4] --dry-run: $MODE — $LINES dòng, $KEYS biến (chưa đụng đĩa):"
  echo "----------------------------------------"
  cat "$TMP_OUT"
  echo "----------------------------------------"
  [ -z "$DROPPED_KEYS" ] || echo "      (sẽ bỏ giá trị: $DROPPED_KEYS)"
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
  cp "$TMP_OUT" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "[2/4] Đã ghi $ENV_FILE (600): $LINES dòng, $KEYS biến — $MODE."
  [ -z "$DROPPED_KEYS" ] || echo "      Đã bỏ giá trị khoá chỉ thuộc máy chạy console: $DROPPED_KEYS"
  [ "$KEEP_SECRETS" != "1" ] || echo "      (--keep-secrets: khoá bí mật giữ nguyên — KHÔNG gửi file này cho ai)"
fi
rm -f "$OV_FILE" "$TMP_OUT"

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
echo "   Muốn đổi model / hạn mức / ADB… thì mở thẳng $ENV_FILE — mọi biến đều có sẵn ở đó."
