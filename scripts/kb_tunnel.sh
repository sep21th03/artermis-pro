#!/usr/bin/env bash
# SSH tunnel tới PostgreSQL trên VPS — nơi knowledge base thực sự nằm.
#
# Vì sao dùng tunnel thay vì mở cổng 5432: database này không có TLS và cổng mở
# ra Internet là mời người lạ xoá kiến thức. Tunnel giữ 5432 đóng với thế giới,
# mà máy dev vẫn dùng được như một PostgreSQL tại chỗ.
#
#   sh scripts/kb_tunnel.sh start | stop | status | restart
#
# Cấu hình qua biến môi trường (mặc định lấy từ .env nếu có):
#   KB_VPS_HOST   IP VPS            (mặc định 103.199.18.222)
#   KB_VPS_USER   user SSH          (mặc định root)
#   KB_VPS_KEY    khoá SSH          (mặc định ~/.ssh/greencloud)
#   KB_LOCAL_PORT cổng phía máy này (mặc định 5433)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

read_env() {
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2-
}

VPS_HOST="${KB_VPS_HOST:-${ARTEMIS_KB_VPS_HOST:-103.199.18.222}}"
VPS_USER="${KB_VPS_USER:-${ARTEMIS_KB_VPS_USER:-root}}"
VPS_KEY="${KB_VPS_KEY:-$HOME/.ssh/greencloud}"
LOCAL_PORT="${KB_LOCAL_PORT:-5433}"
REMOTE_PORT="${KB_REMOTE_PORT:-5432}"
PID_FILE="${TMPDIR:-/tmp}/artemis_kb_tunnel.pid"
LOG_FILE="${TMPDIR:-/tmp}/artemis_kb_tunnel.log"

port_open() {
  # Kiểm tra cổng **thật sự nhận kết nối**, không chỉ pid còn sống.  Tunnel từng ở trạng thái
  # "pid còn nhưng cổng đã đóng" (kết nối SSH nửa chết): ``status`` báo "đang chạy" trong khi
  # mọi truy vấn KB đều "connection refused", và tester tưởng KB hỏng chứ không phải tunnel.
  if command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$LOCAL_PORT" >/dev/null 2>&1
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 0  # không có công cụ kiểm tra thì đành tin pid
  fi
}

is_up() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null && port_open
}

# Dọn tiến trình ssh cũ còn giữ pid file (kể cả trường hợp pid còn nhưng cổng đã đóng).
kill_stale() {
  if [ -f "$PID_FILE" ]; then
    local old
    old="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
      kill "$old" 2>/dev/null || true
      sleep 1
    fi
    rm -f "$PID_FILE"
  fi
  if command -v lsof >/dev/null 2>&1; then
    for listener in $(lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN -t 2>/dev/null || true); do
      kill "$listener" 2>/dev/null || true
    done
  fi
}

start() {
  if is_up; then
    echo "Tunnel đã chạy (pid $(cat "$PID_FILE"))."
    return 0
  fi
  if [ ! -f "$VPS_KEY" ]; then
    echo "Không thấy khoá SSH: $VPS_KEY" >&2
    echo "Đặt KB_VPS_KEY trỏ tới khoá đúng, rồi thử lại." >&2
    exit 1
  fi
  # Cổng đang bị một ssh cũ (đã chết đường truyền) giữ, hoặc pid file cũ còn lại: dọn trước,
  # nếu không thì ExitOnForwardFailure làm tunnel mới thoát ngay và ta lại tưởng "đang chạy".
  kill_stale
  # ExitOnForwardFailure: cổng đã bị chiếm thì phải báo, đừng chạy câm.
  # ServerAliveInterval: mạng rớt thì tunnel tự hồi thay vì treo.
  ssh -i "$VPS_KEY" -o BatchMode=yes -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
      -N -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "${VPS_USER}@${VPS_HOST}" \
      >>"$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 3
  if ! is_up; then
    echo "Tunnel không lên được. Xem log: $LOG_FILE" >&2
    tail -5 "$LOG_FILE" >&2 || true
    rm -f "$PID_FILE"
    exit 1
  fi
  echo "Tunnel đã mở: 127.0.0.1:${LOCAL_PORT} -> ${VPS_HOST}:${REMOTE_PORT} (pid $(cat "$PID_FILE"))"
}

stop() {
  if ! is_up && [ ! -f "$PID_FILE" ]; then
    echo "Tunnel không chạy."
    return 0
  fi
  kill_stale
  echo "Đã đóng tunnel."
}

status() {
  if is_up; then
    echo "Tunnel đang chạy (pid $(cat "$PID_FILE")): 127.0.0.1:${LOCAL_PORT} -> ${VPS_HOST}:${REMOTE_PORT}"
    if command -v lsof >/dev/null 2>&1; then
      lsof -nP -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -2 || true
    fi
  else
    echo "Tunnel không chạy."
    return 1
  fi
}

# ensure: bảo đảm tunnel đang chạy, im lặng khi mọi thứ đã sẵn sàng.
# Dùng cho script khởi động console và cho tester: chạy bao nhiêu lần cũng được, không
# bao giờ mở hai tunnel (mở trùng thì cổng đã bị chiếm và ssh thoát ngay).
ensure() {
  if is_up; then
    return 0
  fi
  start >/dev/null 2>&1
  is_up
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  status)  status ;;
  restart) stop; start ;;
  ensure)  ensure ;;
  *) echo "Dùng: sh scripts/kb_tunnel.sh start|stop|status|restart|ensure" >&2; exit 2 ;;
esac
