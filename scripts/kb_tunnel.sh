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

is_up() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
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
  if ! is_up; then
    echo "Tunnel không chạy."
    rm -f "$PID_FILE"
    return 0
  fi
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
  rm -f "$PID_FILE"
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

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  status)  status ;;
  restart) stop; start ;;
  *) echo "Dùng: sh scripts/kb_tunnel.sh start|stop|status|restart" >&2; exit 2 ;;
esac
