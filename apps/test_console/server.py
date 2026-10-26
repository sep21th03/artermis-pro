#!/usr/bin/env python3
"""Mobile Test Console - local web UI for ARTEMIS.

Vietnamese, app-agnostic wrapper around the Artemis CLI. Binds to loopback
only; the LLM key stays in the Artemis .env file and is never sent to the
browser.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
import ast
import difflib
import hashlib
import hmac
import importlib.util
import json
import math
import os
from contextlib import contextmanager
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import uuid

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field
import httpx

HOME = Path.home()
BASE_DIR = Path(__file__).resolve().parent
# Repo root (apps/test_console -> apps -> <repo>), so the console is
# self-contained: clone the repo anywhere and it drives that same checkout.
REPO_ROOT = BASE_DIR.parents[1]
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
LOG_DIR = DATA_DIR / "logs"
TASKS_FILE = DATA_DIR / "tasks.json"
BUDGET_FILE = DATA_DIR / "budget.json"

def _default_artemis_dir() -> Path:
    """Pick the ARTEMIS checkout to drive.

    Order: explicit ARTEMIS_DIR -> this repo (if it has a runtime) -> the
    classic ~/Developer/artemis install -> this repo (uv fallback).
    Keeps a fresh clone self-contained while still working on machines where
    the runtime lives in another checkout.
    """
    env = os.getenv("ARTEMIS_DIR")
    if env:
        return Path(env).expanduser()
    if (REPO_ROOT / ".venv").exists() or (REPO_ROOT / ".env").exists():
        return REPO_ROOT
    legacy = HOME / "Developer" / "artemis"
    if (legacy / ".venv").exists():
        return legacy
    return REPO_ROOT


ARTEMIS_DIR = _default_artemis_dir()
ARTEMIS_BIN = ARTEMIS_DIR / ".venv" / "bin" / "artemis"
ARTEMIS_ENV = ARTEMIS_DIR / ".env"
ARTEMIS_CONFIG = ARTEMIS_DIR / "config" / "artemis.jsonc"

DEFAULT_ADB = HOME / "Library" / "Android" / "sdk" / "platform-tools" / "adb"
ADB_BIN = Path(os.getenv("ADB_BIN", DEFAULT_ADB)).expanduser()
DEFAULT_PACKAGE = os.getenv("TEST_APP_PACKAGE", "com.example.app")
HOST = os.getenv("ARTEMIS_WEB_HOST", "127.0.0.1")
PORT = int(os.getenv("ARTEMIS_WEB_PORT", "8787"))

# --- Vai trò của tiến trình ---------------------------------------------------
# Quyết định **bộ endpoint nào tồn tại**, không chỉ cái nào được phép gọi.
#
#   server — máy chủ dùng chung (VPS): chỉ AI proxy + API RAG. Không có endpoint
#            điều khiển thiết bị, không có giao diện, không quản trị KB.
#   edge   — máy cắm điện thoại: điều khiển thiết bị + giao diện. Không giữ key
#            LLM thật; LLM và kiến thức đều hỏi máy chủ.
#   all    — cả hai, MẶC ĐỊNH: giữ nguyên hành vi cũ cho máy dev chạy một mình.
#
# Vì sao phải tách chứ không chỉ thêm xác thực: ``/api/run`` chạy ``artemis run``
# và ``/api/stop`` giết tiến trình; đưa chúng lên Internet là lỗ hổng thực thi
# lệnh, còn VPS thì không có điện thoại nào để điều khiển. Tách theo *sự tồn tại*
# của route an toàn hơn chặn bằng middleware: middleware sót một đường là mất hết.
CONSOLE_ROLE = (os.getenv("CONSOLE_ROLE") or "all").strip().lower()
if CONSOLE_ROLE not in {"server", "edge", "all"}:
    print(f"⚠ CONSOLE_ROLE={CONSOLE_ROLE!r} không hợp lệ; dùng 'all'.")
    CONSOLE_ROLE = "all"
SERVES_DEVICE = CONSOLE_ROLE != "server"
SERVES_SHARED = CONSOLE_ROLE != "edge"
#: Quản trị KB (`/api/kb/*`) chỉ có trên máy dev. Nhóm route này sửa và xoá được
#: workflow mà **không có khoá truy cập riêng**, nên nó không được xuất hiện trên
#: máy chủ dùng chung. Người vận hành quản trị KB từ máy dev, qua SSH tunnel tới
#: cùng database đó.
SERVES_KB_ADMIN = CONSOLE_ROLE != "server"
IDLE_TIMEOUT_SECONDS = int(os.getenv("TASK_IDLE_TIMEOUT_SECONDS", "120"))
ARTEMIS_API = os.getenv("ARTEMIS_API_BASE", "http://127.0.0.1:8000").rstrip("/")
# Khoá truy cập API. Console là proxy giữ key LLM thật, nên khi mở ra ngoài
# (VPS/LAN) bắt buộc phải có khoá: nếu không, ai biết URL cũng tiêu hết ngân
# sách của chủ dự án. Giá trị thật lấy từ CONSOLE_ACCESS_KEY (env hoặc .env).
DEFAULT_ACCESS_KEY = "21092003"
SESSION_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def adb_path() -> str:
    return str(ADB_BIN) if ADB_BIN.exists() else "adb"


def run_adb(args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [adb_path(), *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def artemis_launcher() -> list[str] | None:
    """Command prefix for the Artemis CLI.

    Prefers the repo's own venv binary; on a fresh clone without `.venv` it
    falls back to `uv run`, so the console works from any checkout path.
    Windows (and any box without `uv`) has neither, so the last resort is the
    interpreter already serving this console: `python -m artemis` drives the same
    checkout without installing anything.  Returns None when none of the three is
    available, and the caller turns that into a clear message.
    """
    if ARTEMIS_BIN.exists():
        return [str(ARTEMIS_BIN)]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--project", str(ARTEMIS_DIR), "artemis"]
    if importlib.util.find_spec("artemis") is not None:
        return [sys.executable, "-m", "artemis"]
    return None


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").strip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def llm_summary() -> dict:
    env = read_env_file(ARTEMIS_ENV)
    provider, model = "google", "gemini-2.5-flash"
    try:
        text = ARTEMIS_CONFIG.read_text(encoding="utf-8")
        block = re.search(r'"default"\s*:\s*\{(.*?)\n\s*\}', text, re.S)
        scope = block.group(1) if block else text
        if match := re.search(r'"provider"\s*:\s*"([^"]+)"', scope):
            provider = match.group(1)
        if match := re.search(r'"model"\s*:\s*"([^"]+)"', scope):
            model = match.group(1)
    except OSError:
        pass
    key = env.get("OPENAI_API_KEY") or env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")
    if is_local_llm():
        return {
            "provider": "ollama",
            "model": LOCAL_MODEL,
            "base_url": upstream_base(),
            "key_present": True,
            "key_hint": "local",
        }
    return {
        "provider": provider,
        "model": model,
        # Hai thứ khác nhau, và trên máy chủ dùng chung thì khác hẳn nhau:
        #   base_url = nơi client gửi request tới (OPENAI_BASE_URL)
        #   upstream = nơi tiến trình này chuyển tiếp tới (ARTEMIS_UPSTREAM_BASE_URL)
        # Gộp chúng từng làm /api/health báo "api.openai.com" trong khi proxy thực tế
        # gọi "api.vilao.ai" — người debug sẽ đi sai hướng.
        "base_url": env.get("OPENAI_BASE_URL") or f"http://127.0.0.1:{PORT}/llm/v1",
        "upstream": upstream_base(),
        "key_present": bool(key),
        "key_hint": f"…{key[-4:]}" if key and len(key) > 8 else "",
    }


ACCESS_TOKENS_CACHE: set[str] | None = None


def access_key() -> str:
    """Khoá truy cập đang có hiệu lực: biến môi trường > .env > mặc định."""
    env = read_env_file(ARTEMIS_ENV)
    return (
        os.getenv("CONSOLE_ACCESS_KEY") or env.get("CONSOLE_ACCESS_KEY") or DEFAULT_ACCESS_KEY
    ).strip()


def valid_access_tokens() -> set[str]:
    """Các khoá truy cập API được chấp nhận.

    Gồm ``CONSOLE_ACCESS_KEY`` và key upstream trong ``.env`` (để chính máy
    đang chạy proxy không phải cấu hình thêm gì). Đọc một lần rồi cache: đổi
    khoá cần restart console.
    """
    global ACCESS_TOKENS_CACHE
    if ACCESS_TOKENS_CACHE is None:
        tokens = {access_key()}
        env = read_env_file(ARTEMIS_ENV)
        for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            value = env.get(name, "").strip()
            if value:
                tokens.add(value)
        ACCESS_TOKENS_CACHE = {token for token in tokens if token}
    return ACCESS_TOKENS_CACHE


def presented_token(request: Request) -> str:
    """Khoá client gửi lên: ``Authorization: Bearer <khoá>`` hoặc ``x-api-key``."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def access_denied(request: Request, local: bool = False) -> bool:
    """True khi request phải bị từ chối vì thiếu/sai khoá truy cập.

    Chế độ Ollama local miễn kiểm tra: máy đó không tiêu tiền cloud, và
    ``LOCAL_LLM=1 ./run.sh`` phải chạy được mà không cần khoá.
    """
    if local:
        return False
    token = presented_token(request)
    if not token:
        return True
    raw = token.encode("utf-8", "ignore")
    return not any(
        hmac.compare_digest(raw, known.encode("utf-8", "ignore"))
        for known in valid_access_tokens()
    )


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._-")
    return cleaned[:60] or "task"


def list_devices() -> list[dict]:
    try:
        out = run_adb(["devices", "-l"], timeout=10).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise HTTPException(503, f"Không gọi được adb: {exc}") from exc
    devices: list[dict] = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        state = parts[1]
        if state not in {"device", "offline", "unauthorized", "bootloader", "recovery", "sideload"}:
            continue
        model = next((p.split(":", 1)[1] for p in parts if p.startswith("model:")), "")
        devices.append({"serial": parts[0], "state": state, "model": model.replace("_", " ")})
    return devices


# --- Package đã cài trên thiết bị ---------------------------------------------
# Chạy task với package không tồn tại thì ARTEMIS vẫn "thử" 3 lần × 15s rồi mới
# bỏ cuộc, nên tester chỉ thấy task treo mà app không hề mở. Kiểm tra trước khi
# chạy để báo lỗi rõ ràng ngay.

PACKAGE_CACHE_TTL = 30.0
_package_cache: dict[str, tuple[float, list[str]]] = {}


def installed_packages(serial: str = "") -> list[str]:
    """Tên package đã cài trên thiết bị; [] khi không đọc được.

    Cache 30s vì ``pm list packages`` mất ~1s mỗi lần gọi.
    """
    key = serial or "-"
    now = time.time()
    cached = _package_cache.get(key)
    if cached and now - cached[0] < PACKAGE_CACHE_TTL:
        return cached[1]
    args = (["-s", serial] if serial else []) + ["shell", "pm", "list", "packages"]
    try:
        out = run_adb(args, timeout=20).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return cached[1] if cached else []
    names = sorted(
        line.strip().removeprefix("package:")
        for line in out.splitlines()
        if line.strip().startswith("package:")
    )
    _package_cache[key] = (now, names)
    return names


def suggest_packages(package: str, installed: list[str], limit: int = 5) -> list[str]:
    """Package đã cài gần giống tên tester gõ, để gợi ý sửa.

    Ưu tiên cùng tiền tố (``com.iot.`` → ``com.iot.hunonic``), rồi tới chuỗi con,
    rồi tới độ giống chuỗi (difflib). Gợi ý yếu bị bỏ để không đưa ra 5 cái tên rác.
    """
    needle = package.strip().lower()
    if not needle or not installed:
        return []
    prefix = ".".join(needle.split(".")[:2]) + "." if "." in needle else ""
    tail = needle.rsplit(".", 1)[-1]
    scored: dict[str, int] = {}

    def bump(name: str, points: int) -> None:
        scored[name] = scored.get(name, 0) + points

    for name in installed:
        lowered = name.lower()
        if lowered == needle:
            bump(name, 100)
        if prefix and lowered.startswith(prefix):
            bump(name, 8)
        if len(needle) > 6 and needle in lowered:
            bump(name, 15)
        if len(tail) > 3 and lowered.rsplit(".", 1)[-1] == tail:
            bump(name, 6)
    # cutoff 0.8: dưới ngưỡng đó difflib trả về cả loạt tên chỉ chung tiền tố.
    for name in difflib.get_close_matches(needle, installed, n=limit * 2, cutoff=0.8):
        bump(name, 20)
    ordered = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
    return [name for name, score in ordered if score >= 8][:limit]


# --- Metro của app React Native bản debug ------------------------------------
# Bản debug không nhúng JS bundle: thiếu `adb reverse` + Metro là app kẹt ở splash
# mãi mãi. Agent không biết, cứ chờ rồi chạm lung tung — trông như "chạy linh tinh".

RN_METRO_PORT = os.getenv("CONSOLE_RN_METRO_PORT", "8081").strip()


def _metro_check_enabled(port: str) -> bool:
    """Kiểm tra Metro chỉ có nghĩa khi port là số khác 0 (0 = tắt hẳn)."""
    return port.isdigit() and port != "0"


def metro_reverse_ready(serial: str, port: str = RN_METRO_PORT) -> bool:
    """True khi thiết bị đã có ``adb reverse tcp:<port>`` (hoặc không kiểm tra được)."""
    if not _metro_check_enabled(port):
        return True
    args = (["-s", serial] if serial else []) + ["reverse", "--list"]
    try:
        out = run_adb(args, timeout=10).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return True  # không kiểm tra được thì đừng báo động sai
    return f"tcp:{port}" in out


def metro_warning(serial: str, port: str = RN_METRO_PORT) -> str:
    """Cảnh báo tester khi thiếu đường Metro cho app debug (rỗng = không có gì)."""
    if not _metro_check_enabled(port) or metro_reverse_ready(serial, port):
        return ""
    return (
        f"Chưa thấy `adb reverse tcp:{port}` trên thiết bị. Nếu app đang test là bản "
        f"React Native **debug**, app sẽ kẹt ở màn hình splash vì không tải được JS "
        f"bundle. Chạy: `adb reverse tcp:{port} tcp:{port}` và bật Metro (`yarn start`)."
    )


# `--locked-app` của ARTEMIS chỉ đảm bảo app ở foreground **lúc khởi động**; nó
# không ràng buộc hành động sau đó và agent cũng không được thông báo. Đo thực tế:
# agent tự force-stop app khác ("Tuya Smart"), bấm HOME, mở app switcher rồi chạm
# lung tung khi prompt không nêu tên app. Nhắc thẳng trong prompt để chặn việc đó.
APP_LOCK_HINT_ENABLED = os.getenv("CONSOLE_APP_LOCK_HINT", "1") == "1"
APP_LOCK_HEADER = "[APP ĐANG TEST — chỉ thao tác trong app này]"


def app_lock_hint(package: str) -> str:
    """Khối nhắc agent bám đúng app cần test (tắt bằng CONSOLE_APP_LOCK_HINT=0)."""
    if not APP_LOCK_HINT_ENABLED or not package.strip():
        return ""
    return (
        f"{APP_LOCK_HEADER}\n"
        f"- App cần test: {package.strip()}\n"
        "- Chỉ thao tác trong app này. Không tự mở hay tắt app khác, không bấm HOME, "
        "không mở danh sách ứng dụng (app switcher).\n"
        "- Chỉ rời khỏi app khi mục tiêu yêu cầu rõ ràng (ví dụ mở Cài đặt để cấp "
        "quyền), và quay lại app ngay sau đó.\n"
        "- Nếu không thấy màn hình/nút mà mục tiêu nói tới: báo FAILED kèm mô tả màn "
        "hình đang thấy, đừng thử lung tung."
    )


class Budget:
    """Per-task and per-day spend guard for the LLM proxy.

    Defaults are a generic VND price table: 500 VND / 1M input,
    2,000 VND / 1M output, minimum 10 VND per request.
    """

    def __init__(self) -> None:
        self.max_requests = int(os.getenv("BUDGET_MAX_REQUESTS", "40"))
        self.max_output_tokens = int(os.getenv("BUDGET_MAX_OUTPUT_TOKENS", "768"))
        self.max_task_cost_vnd = float(os.getenv("BUDGET_MAX_TASK_COST_VND", "2000"))
        self.max_daily_cost_vnd = float(os.getenv("BUDGET_DAILY_COST_VND", "6000"))
        self.max_task_seconds = int(os.getenv("BUDGET_MAX_TASK_SECONDS", "480"))
        self.max_request_bytes = int(os.getenv("BUDGET_MAX_REQUEST_BYTES", str(8 * 1024 * 1024)))
        self.max_prompt_tokens = int(os.getenv("BUDGET_MAX_PROMPT_TOKENS", "60000"))
        self.price_input = float(os.getenv("BUDGET_PRICE_INPUT_VND_PER_M", "500"))
        self.price_output = float(os.getenv("BUDGET_PRICE_OUTPUT_VND_PER_M", "2000"))
        self.min_request_cost = float(os.getenv("BUDGET_MIN_REQUEST_COST_VND", "10"))
        self._lock = threading.Lock()
        self._task_id = "none"
        self._requests = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._task_cost = 0.0
        self._started = 0.0
        self._last_request_at = 0.0
        # CLI calls that use the proxy directly share one synthetic "external"
        # task. Without an idle reset its 480s timer expires once and then every
        # later request is rejected with 429 forever.
        self.external_idle_reset = float(
            os.getenv("BUDGET_EXTERNAL_IDLE_RESET_SECONDS", "120")
        )
        self._exceeded: str | None = None
        self._daily_spent = 0.0
        self._daily_requests = 0
        self._day = ""
        self._load_daily()

    def _load_daily(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            raw = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if raw.get("day") == today:
            self._daily_spent = float(raw.get("spent_vnd", 0))
            self._daily_requests = int(raw.get("requests", 0))
        self._day = today

    def _save_daily(self) -> None:
        try:
            BUDGET_FILE.write_text(
                json.dumps(
                    {"day": self._day, "spent_vnd": round(self._daily_spent, 2),
                     "requests": self._daily_requests},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def start_task(self, task_id: str) -> None:
        with self._lock:
            self._task_id = task_id
            self._requests = 0
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._task_cost = 0.0
            self._started = time.time()
            self._exceeded = None

    def _spent_today(self) -> float:
        return self._daily_spent if self._day == datetime.now().strftime("%Y-%m-%d") else 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "task_id": self._task_id,
                "requests": self._requests,
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
                "task_cost_vnd": round(self._task_cost, 1),
                "daily_spent_vnd": round(self._spent_today(), 1),
                "exceeded": self._exceeded,
                "limits": {
                    "max_requests": self.max_requests,
                    "max_output_tokens": self.max_output_tokens,
                    "max_task_cost_vnd": self.max_task_cost_vnd,
                    "max_daily_cost_vnd": self.max_daily_cost_vnd,
                    "max_task_seconds": self.max_task_seconds,
                    "price_input_vnd_per_m": self.price_input,
                    "price_output_vnd_per_m": self.price_output,
                    "min_request_cost_vnd": self.min_request_cost,
                },
            }

    def reset(self) -> None:
        """Clear the current task's counters and its block reason."""
        with self._lock:
            self._requests = 0
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._task_cost = 0.0
            self._started = time.time()
            self._last_request_at = self._started
            self._exceeded = None

    def _reset_external_if_idle(self, now: float) -> None:
        """Treat a quiet gap between CLI calls as a new external task."""
        if self._task_id != "external" or not self._last_request_at:
            return
        if now - self._last_request_at >= self.external_idle_reset:
            self._requests = 0
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._task_cost = 0.0
            self._started = now
            self._exceeded = None

    def check(self, body: dict, raw_size: int) -> str | None:
        """Return a block reason, or None when the request may proceed."""
        with self._lock:
            self._reset_external_if_idle(time.time())
            self._last_request_at = time.time()
            if self._exceeded:
                return self._exceeded
            if raw_size > self.max_request_bytes:
                self._exceeded = f"Request {raw_size} bytes vượt giới hạn {self.max_request_bytes} bytes."
            elif self._started and time.time() - self._started > self.max_task_seconds:
                self._exceeded = f"Task chạy quá {self.max_task_seconds}s."
            elif self._requests >= self.max_requests:
                self._exceeded = f"Đã đạt giới hạn {self.max_requests} request cho task này."
            elif self._task_cost >= self.max_task_cost_vnd:
                self._exceeded = f"Task đã tiêu {self._task_cost:.0f}đ, chạm giới hạn {self.max_task_cost_vnd:.0f}đ."
            elif self._spent_today() >= self.max_daily_cost_vnd:
                self._exceeded = f"Hôm nay đã tiêu {self._spent_today():.0f}đ, chạm giới hạn ngày {self.max_daily_cost_vnd:.0f}đ."
            else:
                estimated = _estimate_prompt_tokens(body)
                if estimated > self.max_prompt_tokens:
                    self._exceeded = f"Prompt ước tính {estimated} token, vượt {self.max_prompt_tokens}."
            if self._exceeded:
                return self._exceeded
            self._requests += 1
            return None

    def clamp_output(self, body: dict) -> dict:
        """Force a per-response output cap so one huge answer cannot drain the balance."""
        for key in ("max_tokens", "max_completion_tokens"):
            if key in body:
                if not isinstance(body[key], int) or body[key] > self.max_output_tokens:
                    body[key] = self.max_output_tokens
                return body
        body["max_tokens"] = self.max_output_tokens
        return body

    def record(self, prompt_tokens: int, completion_tokens: int) -> float:
        prompt_tokens = max(0, int(prompt_tokens or 0))
        completion_tokens = max(0, int(completion_tokens or 0))
        if is_local_llm():
            cost = 0.0
        else:
            cost = (prompt_tokens * self.price_input + completion_tokens * self.price_output) / 1_000_000
            cost = max(self.min_request_cost, cost)
        with self._lock:
            if self._day != datetime.now().strftime("%Y-%m-%d"):
                self._day = datetime.now().strftime("%Y-%m-%d")
                self._daily_spent = 0.0
                self._daily_requests = 0
            self._prompt_tokens += prompt_tokens
            self._completion_tokens += completion_tokens
            self._task_cost += cost
            self._daily_spent += cost
            self._daily_requests += 1
            self._save_daily()
            already_blocked = self._exceeded
        if not already_blocked:
            if self._task_cost >= self.max_task_cost_vnd:
                self._exceeded = f"Task đã tiêu {self._task_cost:.0f}đ, chạm giới hạn {self.max_task_cost_vnd:.0f}đ."
            elif self._spent_today() >= self.max_daily_cost_vnd:
                self._exceeded = f"Hôm nay đã tiêu {self._spent_today():.0f}đ, chạm giới hạn ngày."
        if self._exceeded:
            active = globals().get("runner")
            if active is not None:
                threading.Thread(target=active.stop, daemon=True).start()
        return cost

    def record_prepare(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Ghi chi phí một lượt LLM của bước *chuẩn bị prompt* vào hạn mức NGÀY.

        Bước chuẩn bị chạy ngoài một lần chạy test, nên nó không được tiêu vào hạn
        mức của task (``BUDGET_MAX_REQUESTS``/``BUDGET_MAX_TASK_COST_VND``) — làm
        vậy thì một lần bấm "Chuẩn bị" cũng đủ khiến task sau bị 429.  Nhưng tiền
        là tiền: nó vẫn vào hạn mức ngày, và chạm hạn mức ngày thì proxy vẫn chặn.
        """
        prompt_tokens = max(0, int(prompt_tokens or 0))
        completion_tokens = max(0, int(completion_tokens or 0))
        if is_local_llm():
            cost = 0.0
        else:
            cost = (
                prompt_tokens * self.price_input + completion_tokens * self.price_output
            ) / 1_000_000
            cost = max(self.min_request_cost, cost)
        with self._lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if self._day != today:
                self._day = today
                self._daily_spent = 0.0
                self._daily_requests = 0
            self._daily_spent += cost
            self._daily_requests += 1
            self._save_daily()
            if not self._exceeded and self._spent_today() >= self.max_daily_cost_vnd:
                self._exceeded = (
                    f"Hôm nay đã tiêu {self._spent_today():.0f}đ, chạm giới hạn ngày "
                    f"{self.max_daily_cost_vnd:.0f}đ."
                )
        return cost


def _estimate_prompt_tokens(body: dict) -> int:

    total = 0
    for message in body.get("messages", []) or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            total += max(1, len(content) // 4)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += max(1, len(str(part.get("text", ""))) // 4)
                elif part.get("type") in ("image_url", "input_image"):
                    total += 1500
    return total


def stop_daemon_task(session_id: str = "", device_id: str = "") -> bool:
    """Ask the Artemis Daemon (port 8000) to cancel a task.

    ``artemis run`` never executes locally while the daemon is up: it submits
    the task to the daemon and then only waits for the result. Killing the CLI
    therefore detaches the client but leaves the daemon driving the phone
    until the task hits its own turn limit. Stopping for real means telling the
    daemon to cancel, which is what this helper does.
    """
    if not session_id and not device_id:
        return False
    payload = {"session_id": session_id} if session_id else {"device_id": device_id}
    try:
        response = httpx.post(
            f"{ARTEMIS_API}/api/stop", json=payload, timeout=6.0, trust_env=False
        )
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(data, dict) and data.get("status") == "stopped"


@dataclass
class Task:
    id: str
    goal: str
    profile: str
    package: str
    serial: str
    test_name: str
    # Prompt thực sự gửi cho ARTEMIS: goal + khối gợi ý vị trí (nếu khớp).
    # `goal` vẫn là nguyên văn tester nhập, để lịch sử/UI không bị lẫn.
    goal_sent: str = ""
    # "stdout" = đọc từ log của chính lần chạy này (chính xác);
    # "guess" = đoán theo nội dung prompt (chỉ dùng tạm khi chưa có stdout).
    session_source: str = ""
    max_seconds: int = 0
    session_id: str = ""
    cost_vnd: float = 0.0
    requests: int = 0
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    exit_code: int | None = None
    log_path: str = ""


class Runner:
    """Runs one Artemis task at a time and keeps its log for the browser."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._current: Task | None = None
        self._lines: deque[dict] = deque(maxlen=20000)
        self._seq = 0
        self._last_activity = time.time()
        self._exit_seen_at = 0.0
        self._tasks: list[Task] = self._load_tasks()

    def _load_tasks(self) -> list[Task]:
        try:
            raw = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
            return [Task(**item) for item in raw]
        except (OSError, ValueError, TypeError):
            return []

    def _save_tasks(self) -> None:
        try:
            TASKS_FILE.write_text(
                json.dumps([asdict(t) for t in self._tasks[-100:]], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _append(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._seq += 1
            self._last_activity = time.time()
            self._lines.append({"i": self._seq, "t": stamp, "text": text})
            task = self._current
        if task and task.log_path:
            try:
                with open(task.log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"[{stamp}] {text}\n")
            except OSError:
                pass

    def status(self, since: int = 0) -> dict:
        with self._lock:
            first = self._lines[0]["i"] if self._lines else 0
            start = max(since, first - 1)
            lines = [line for line in self._lines if line["i"] > start]
            proc = self._proc
            task = self._current
            # Tiến trình có thể thoát trước khi thread _pump kịp ghi status /
            # ended_at / cost. Nếu lúc đó báo "không chạy", UI sẽ hỏi summary
            # khi task vẫn còn status="running" -> trước đây hoá thành
            # "KHÔNG ĐẠT" dù Artemis đã COMPLETED. Giữ trạng thái "đang chạy"
            # thêm tối đa 3s cho tới khi bản ghi task được chốt.
            fresh_exit = (
                proc is not None
                and proc.poll() is not None
                and task is not None
                and task.ended_at is None
            )
            if fresh_exit:
                if not self._exit_seen_at:
                    self._exit_seen_at = time.time()
                fresh_exit = time.time() - self._exit_seen_at < 3.0
            else:
                self._exit_seen_at = 0.0
            return {
                "running": bool(proc is not None and proc.poll() is None) or fresh_exit,
                "task": asdict(task) if task else None,
                "seq": self._seq,
                "lines": lines,
            }

    def history(self, limit: int = 30) -> list[dict]:
        with self._lock:
            return [asdict(t) for t in reversed(self._tasks[-limit:])]

    def task_log(self, task_id: str) -> Path | None:
        with self._lock:
            task = next((t for t in self._tasks if t.id == task_id), None)
        if not task or not task.log_path:
            return None
        path = Path(task.log_path)
        return path if path.exists() else None

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            return next((t for t in self._tasks if t.id == task_id), None)

    def start(self, req: RunRequest) -> Task:
        goal = req.goal.strip()
        package = req.package.strip() or DEFAULT_PACKAGE
        # Prompt gửi cho ARTEMIS là prompt gốc, TRỪ khi tester đã xem và sửa prompt
        # hoàn chỉnh trong tab Knowledge — lúc đó dùng bản đã sửa và báo ARTEMIS
        # đừng ghép kiến thức lần nữa (xem ARTEMIS_KB_PRESUPPLIED).
        override = req.prompt_override.strip()
        goal_sent = override or goal

        with self._lock:
            if self._proc and self._proc.poll() is None:
                raise RuntimeError("Một task đang chạy. Hãy dừng hoặc đợi task hiện tại xong.")
            launcher = artemis_launcher()
            if launcher is None:
                raise RuntimeError(
                    f"Không tìm thấy Artemis tại {ARTEMIS_DIR}. Chạy `uv sync` trong repo "
                    "hoặc đặt biến ARTEMIS_DIR trỏ tới checkout ARTEMIS khác."
                )

            now = datetime.now()
            task_id = uuid.uuid4().hex[:8]
            test_name = safe_name(req.test_name or f"task_{now.strftime('%d%m_%H%M')}")
            log_path = LOG_DIR / f"{now.strftime('%Y%m%d_%H%M%S')}_{test_name}_{task_id}.log"
            task = Task(
                id=task_id,
                goal=goal,
                goal_sent=goal_sent,
                profile=req.profile,
                package=package,
                serial=req.serial.strip(),
                test_name=test_name,
                max_seconds=req.max_seconds,
                log_path=str(log_path),
            )
            self._current = task
            self._tasks.append(task)
            self._lines.clear()
            self._seq = 0
            self._save_tasks()

        log_path.write_text(
            f"# Task {task_id}\n# goal: {task.goal}\n# profile: {task.profile}\n"
            f"# package: {task.package}\n# serial: {task.serial or 'auto'}\n",
            encoding="utf-8",
        )

        cmd = [
            *launcher, "run", task.goal_sent,
            "--profile", task.profile, "--test-name", task.test_name,
        ]
        if task.package:
            cmd += ["--locked-app", task.package]
        if task.profile == "flash":
            cmd.append("--disable-outputter")
        if task.serial:
            cmd += ["--device-serial", task.serial]

        env = dict(os.environ)
        env["PATH"] = f"{Path(adb_path()).parent}:{HOME / '.local' / 'bin'}:{env.get('PATH', '')}"
        env["PYTHONUNBUFFERED"] = "1"
        if task.serial:
            env["ADB_DEVICE_SERIAL"] = task.serial
        if override:
            # Prompt đã chứa khối kiến thức console ghép sẵn ⇒ ARTEMIS không được
            # lấy thêm lần nữa, nếu không khối đó vào prompt hai lần.
            env["ARTEMIS_KB_PRESUPPLIED"] = "1"

        self._append(f"$ {' '.join(cmd)}")
        if app_lock_hint(task.package):
            self._append(f"🔒 Đã nhắc agent chỉ thao tác trong app: {task.package}")
        if override:
            self._append(
                "📋 Dùng prompt hoàn chỉnh tester đã sửa "
                f"({len(override)} ký tự) — ARTEMIS sẽ không ghép kiến thức lần nữa."
            )
        proc = subprocess.Popen(
            cmd,
            cwd=str(ARTEMIS_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        with self._lock:
            self._proc = proc
        budget.start_task(task.id)
        threading.Thread(target=self._pump, args=(proc, task), daemon=True).start()
        threading.Thread(target=self._watchdog, args=(proc, task), daemon=True).start()
        # Log thật của agent nằm trong file trace, không nằm trên stdout của CLI.
        threading.Thread(target=self._tail_trace, args=(task,), daemon=True).start()
        return task

    def _trace_active(self, started_at: float) -> bool:
        """True when the newest Artemis trace log was written recently."""
        try:
            newest = max(
                (path.stat().st_mtime for path in (ARTEMIS_DIR / "traces").glob("*/stdout.log")),
                default=0.0,
            )
        except OSError:
            return False
        return newest >= started_at and (time.time() - newest) < IDLE_TIMEOUT_SECONDS

    def _watchdog(self, proc: subprocess.Popen[str], task: Task) -> None:
        limit = min(task.max_seconds or budget.max_task_seconds, budget.max_task_seconds)
        while proc.poll() is None:
            time.sleep(5)
            reason = ""
            if time.time() - task.started_at > limit:
                reason = f"đã chạy quá {limit}s"
            elif (
                time.time() - self._last_activity > IDLE_TIMEOUT_SECONDS
                and not self._trace_active(task.started_at)
            ):
                reason = f"treo {IDLE_TIMEOUT_SECONDS}s không có log mới"
            if not reason:
                continue
            self._append(f"⛔ Dừng task: {reason}.")
            with self._lock:
                task.status = "stopped"
            self._cancel_on_daemon(task)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            return

    def current_id(self) -> str | None:
        with self._lock:
            return self._current.id if self._current else None

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._proc and self._proc.poll() is None)

    def note(self, text: str) -> None:
        """Ghi một cảnh báo vào log của task đang chạy để tester thấy trên UI."""
        self._append(f"⚠ {text}")

    def _tail_trace(self, task: Task) -> None:
        """Đọc output thật của agent từ ``traces/<session>/stdout.log``.

        ``artemis run`` giao việc cho Artemis Daemon rồi chỉ in mấy dòng lịch
        trình, nên stdout của tiến trình CLI gần như trống — đo trên một lần chạy
        thật: **12 dòng cho cả task**, không một dòng nào về LLM hay hành động.
        Output thật nằm ở file này. Không đọc nó thì người test chỉ thấy task
        "treo" mà không biết agent đang làm gì, và mọi thứ đi qua VPS (RAG, từng
        lượt LLM) đều vô hình.

        Đọc **từ đầu file** chứ không phải từ cuối, để lấy cả phần đã xảy ra trước
        khi biết session id; và đọc nốt phần còn lại sau khi tiến trình kết thúc.
        """
        path: Path | None = None
        offset = 0
        deadline = time.time() + max(task.max_seconds, 60) + 180
        while True:
            if path is None and task.session_id:
                candidate = ARTEMIS_DIR / "traces" / task.session_id / "stdout.log"
                if candidate.exists():
                    path = candidate
                    self._append(
                        f"--- log agent ({task.session_id[:8]}) — "
                        "nguồn: traces/<session>/stdout.log ---"
                    )
            if path is not None:
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(offset)
                        for raw in handle:
                            self._append(raw.rstrip("\n"))
                        offset = handle.tell()
                except OSError:
                    pass
            if task.ended_at is not None or time.time() > deadline:
                break
            time.sleep(0.7)

    def _pump(self, proc: subprocess.Popen[str], task: Task) -> None:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                text = raw.rstrip("\n")
                self._append(text)
                # stdout là nguồn chính xác: ghi đè cả bản đoán tạm, nếu không
                # chạy lại cùng một prompt sẽ mãi hiển thị session của lần trước.
                if task.session_source != "stdout":
                    match = SESSION_RE.search(text)
                    if match:
                        task.session_id = match.group(0)
                        task.session_source = "stdout"
        finally:
            rc = proc.wait()
            self._append(f"[artemis exited with code {rc}]")
            self._capture_final_screen(task)
            snapshot = budget.snapshot()
            with self._lock:
                task.exit_code = rc
                task.ended_at = time.time()
                task.cost_vnd = snapshot["task_cost_vnd"]
                task.requests = snapshot["requests"]
                if task.status != "stopped":
                    task.status = "failed" if rc != 0 else "success"
                self._proc = None
                self._save_tasks()

    @staticmethod
    def _capture_final_screen(task: Task) -> None:
        if not task.serial:
            return
        target = LOG_DIR / f"{task.id}_final.png"
        try:
            result = subprocess.run(
                [adb_path(), "-s", task.serial, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=12, check=False,
            )
            if result.returncode == 0 and result.stdout:
                target.write_bytes(result.stdout)
        except (subprocess.TimeoutExpired, OSError):
            pass

    def _cancel_on_daemon(self, task: Task | None) -> bool:
        """Cancel the daemon-side run so the phone is released immediately."""
        if not task:
            return False
        stopped = stop_daemon_task(session_id=task.session_id, device_id=task.serial)
        if stopped:
            self._append("⛔ Artemis Daemon đã huỷ task.")
        else:
            self._append(
                "⚠ Không huỷ được task trên Artemis Daemon; chỉ dừng được tiến "
                "trình CLI (daemon có thể vẫn chạy tới hết lượt)."
            )
        return stopped

    def stop(self) -> str:
        with self._lock:
            proc = self._proc
            task = self._current
        if not proc or proc.poll() is not None:
            return "Không có task nào đang chạy."
        if task:
            with self._lock:
                task.status = "stopped"
        cancelled = self._cancel_on_daemon(task)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return "Task đã kết thúc."
        threading.Thread(target=self._force_kill, args=(proc,), daemon=True).start()
        if cancelled:
            return "Đã huỷ task trên Artemis Daemon và dừng tiến trình CLI."
        return "Đã gửi tín hiệu dừng task."

    @staticmethod
    def _force_kill(proc: subprocess.Popen[str]) -> None:
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def _http_json(url: str, timeout: float = 10.0):
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _resolve_session(task: Task) -> str:
    """Session của CHÍNH lần chạy này.

    Chỉ đoán theo nội dung prompt khi stdout chưa kịp in session, và chỉ nhận
    session **bắt đầu sau** khi task này chạy. Trước đây hàm này nhận mọi session
    cùng prompt trong ±180s rồi cache lại, nên chạy lại một prompt sẽ hiển thị
    session (và bước) của lần chạy TRƯỚC đó.
    """
    if task.session_id and task.session_source == "stdout":
        return task.session_id
    try:
        sessions = _http_json(f"{ARTEMIS_API}/api/sessions", timeout=6)
    except Exception:  # noqa: BLE001 - Artemis UI may be down
        return task.session_id if task.session_source == "guess" else ""
    target = task.goal_sent or task.goal
    claimed = {
        str(item.get("session_id"))
        for item in runner.history(500)
        if item.get("session_id") and item.get("id") != task.id
    }
    best: tuple[float, str] | None = None
    for session in sessions:
        session_id = str(session.get("session_id") or "")
        if not session_id or session_id in claimed:
            continue
        if session.get("initial_goal") != target:
            continue
        try:
            start = float(session.get("start_time", 0))
        except (TypeError, ValueError):
            continue
        if start < task.started_at - 5:
            continue
        if best is None or start < best[0]:
            best = (start, session_id)
    if best:
        task.session_id = best[1]
        task.session_source = "guess"
        return task.session_id
    return task.session_id if task.session_source == "guess" else ""


ACTION_LABELS = {
    "click": "Chạm", "tap": "Chạm", "long_press": "Nhấn giữ",
    "swipe": "Vuốt", "scroll": "Cuộn", "press_key": "Nhấn phím",
    "back": "Quay lại", "open_app": "Mở app", "launch_app": "Mở app",
    "input_text": "Nhập nội dung", "type": "Nhập nội dung", "set_text": "Nhập nội dung",
    "wait": "Chờ", "wait_for_delay": "Chờ", "home": "Về màn hình chính",
}

SKIP_STEP_ACTIONS = {
    "report_task_status", "update_note", "save_note", "ask_explorer",
    "update_plan", "read_plan", "finish",
}


def _clean_target(value) -> str:
    text = str(value or "")
    text = "".join(
        ch for ch in text
        if ch.isprintable() and not ("\ue000" <= ch <= "\uf8ff")
    )
    return text.strip()


def _step_label(action: str, args: dict, target: str) -> str:
    base = ACTION_LABELS.get(action, action or "Hành động")
    if action in {"input_text", "type", "set_text"}:
        return base
    if action == "press_key" and args.get("key"):
        return f"{base} {args['key']}"
    target = _clean_target(target)
    if target:
        return f"{base} “{target[:48]}”"
    return base


def _fetch_steps(task: Task) -> list[dict]:
    session_id = _resolve_session(task)
    if not session_id:
        return []
    try:
        raw_steps = _http_json(f"{ARTEMIS_API}/api/sessions/{session_id}/steps", timeout=10)
    except Exception:  # noqa: BLE001
        return []
    steps = []
    for item in raw_steps:
        action = item.get("action_taken") or {}
        action_name = action.get("action", "")
        if action_name in SKIP_STEP_ACTIONS:
            continue
        args = action.get("args") or {}
        target = _clean_target(action.get("target_text") or args.get("text") or "")
        pre_image = item.get("pre_image_name")
        post_image = item.get("post_image_name")
        steps.append({
            "number": item.get("step_number"),
            "action": action_name,
            "label": _step_label(action_name, args, target),
            "before": f"/api/tasks/{task.id}/image/{pre_image}" if pre_image else "",
            "after": f"/api/tasks/{task.id}/image/{post_image}" if post_image else "",
            "result": (item.get("last_execution_result") or {}).get("status", ""),
            "detail": (item.get("last_execution_result") or {}).get("result", ""),
            "tokens": item.get("total_tokens") or 0,
            # Lý do agent chọn hành động này — có bước nhãn rỗng (nút chỉ có icon)
            # nên đây là thứ duy nhất giải thích được nó đang làm gì.
            "thinking": _clean_target(item.get("operator_raw_thinking"))[:300],
        })
    return steps


def _trace_text(task: Task) -> str:
    session_id = _resolve_session(task)
    if not session_id:
        return ""
    path = ARTEMIS_DIR / "traces" / session_id / "stdout.log"
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


TURN_RE = re.compile(r"---\s*Artemis\s+(\w+)\s+Turn\s+(\d+)/(\d+)\s*---")
TOOL_RE = re.compile(r"Executing Flash tool:\s*([a-z_]+)\((.*)")
# Dòng hạ tầng, không phải việc agent làm.
ACTIVITY_SKIP = (
    "HTTP Request:",
    "Processing request of type",
    "Delaying ",
    "Recorded step",
    "LLM usage for session",
    "DataEngine",
    "scrcpy",
    "Compiling trace",
    "Found ",
    "Video created",
    "Trace compiled",
    "Traces located",
    "Session ended:",
    "Waiting for ",
)
ACTIVITY_INFO = (
    "is already in foreground",
    "attempting to launch",
    "Launch attempt",
    "not in foreground",
    "Starting initial app launch",
)
ACTIVITY_MAX_ITEMS = 80


def _activity(task: Task) -> list[dict]:
    """Diễn biến thật của agent, đọc từ trace.

    Có task agent chỉ quan sát rồi kết luận — không có thao tác chạm/vuốt nào —
    nên tab "Các bước" trống trơn dù đã tốn thời gian và tiền. Danh sách này cho
    tester thấy nó đã thực sự làm gì.
    """
    trace = _trace_text(task)
    if not trace:
        return []
    items: list[dict] = []
    for raw in trace.splitlines():
        line = raw.strip()
        if not line or any(skip in line for skip in ACTIVITY_SKIP):
            continue
        if turn := TURN_RE.search(line):
            items.append({"kind": "turn", "label": f"Lượt {turn.group(2)}/{turn.group(3)}", "detail": ""})
            continue
        if tool := TOOL_RE.search(line):
            name, args = tool.group(1), tool.group(2).rstrip(")")
            items.append({
                "kind": "tool",
                "label": ACTION_LABELS.get(name, name),
                "detail": _clean_target(args)[:200],
            })
            continue
        if line.startswith("❌"):
            items.append({"kind": "error", "label": line.lstrip("❌ ").strip()[:240], "detail": ""})
            continue
        if "FlashRunner failed:" in line:
            items.append({
                "kind": "error",
                "label": line.split("FlashRunner failed:", 1)[1].strip()[:240],
                "detail": "",
            })
            continue
        if line.startswith("⚠"):
            items.append({"kind": "warn", "label": line.lstrip("⚠ ").strip()[:240], "detail": ""})
            continue
        if any(marker in line for marker in ACTIVITY_INFO):
            items.append({"kind": "info", "label": line.lstrip("ℹ ").strip()[:240], "detail": ""})
    return items[-ACTIVITY_MAX_ITEMS:]


def _read_log(task: Task) -> str:
    try:
        return Path(task.log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_run_outcome(trace: str) -> dict | None:
    """Đọc dòng ``Run outcome:`` mà exit settlement ghi vào trace.

    Dòng này là **bằng chứng duy nhất** cho biết có bao nhiêu tiêu chí đã được
    Checker chấm. Không có nó nghĩa là run không đi qua graph (profile ``flash``
    dùng ``FlashRunner``) nên không tiêu chí nào được chấm cả.
    """
    match = re.search(
        r"Run outcome: task_status=(\w+),\s*tests\(passed=(\d+),\s*failed=(\d+),"
        r"\s*inconclusive=(\d+),\s*unchecked=(\d+)",
        trace,
    )
    if not match:
        return None
    return {
        "task_status": match.group(1),
        "passed": int(match.group(2)),
        "failed": int(match.group(3)),
        "inconclusive": int(match.group(4)),
        "unchecked": int(match.group(5)),
    }


def verified_verdict(outcome: dict | None) -> tuple[str, str, list[str]]:
    """Kết luận cho một run mà agent **tự báo** đã hoàn thành.

    Đây là chỗ dễ nói dối nhất trong cả hệ thống: agent gọi
    ``report_task_status(status="completed")`` và trước đây console tin ngay,
    nên một task không làm được gì vẫn hiện **ĐẠT**. Nay chỉ ĐẠT khi có tiêu chí
    được Checker xác minh.

    Trả ``(verdict, conclusion, issues)``.
    """
    if outcome is None:
        return (
            "unverified",
            "CHƯA XÁC MINH – agent tự báo hoàn thành nhưng KHÔNG có tiêu chí nào "
            "được kiểm chứng (profile flash không có Checker).",
            [
                "Không có bằng chứng xác minh: chỉ có lời tự báo của agent.",
                "Muốn có kết luận thật, chạy lại bằng profile pro để Planner khai "
                "tiêu chí và Checker chấm chúng.",
            ],
        )
    if outcome["failed"]:
        return (
            "fail",
            f"KHÔNG ĐẠT – {outcome['failed']} tiêu chí đã kiểm chứng và THẤT BẠI.",
            [f"{outcome['failed']} tiêu chí thất bại theo Checker."],
        )
    if outcome["inconclusive"]:
        return (
            "unverified",
            f"CHƯA XÁC MINH – Checker không kết luận được ({outcome['inconclusive']} "
            "tiêu chí chưa rõ; thường do hết ngân sách LLM hoặc thiếu bằng chứng).",
            [f"{outcome['inconclusive']} tiêu chí chưa có kết luận."],
        )
    if outcome["passed"]:
        return (
            "pass",
            f"ĐẠT – {outcome['passed']} tiêu chí đã được Checker xác minh.",
            [],
        )
    return (
        "unverified",
        "CHƯA XÁC MINH – task không khai tiêu chí nào nên không có gì để chấm.",
        ["Task không có tiêu chí kiểm chứng (check line)."],
    )


def _task_summary(task: Task) -> dict:
    steps = _fetch_steps(task)
    trace = _trace_text(task)
    log_text = _read_log(task)

    report: dict = {}
    match = re.search(r"report_task_status\((\{.*?\})\)", trace)
    if match:
        try:
            parsed = ast.literal_eval(match.group(1))
            if isinstance(parsed, dict):
                report = parsed
        except (ValueError, SyntaxError):
            report = {}

    stopped_match = re.search(r"⛔ Dừng task: (.+)", trace + "\n" + log_text)
    failed_match = re.search(r"(?:FlashRunner failed|Task failed):\s*(.+)", trace)

    issues: list[str] = []
    if task.status == "stopped":
        verdict = "stopped"
        reason = stopped_match.group(1).strip() if stopped_match else "Người dùng dừng task."
        issues.append(reason)
        conclusion = f"ĐÃ DỪNG – {reason}"
    elif task.status == "success" and not failed_match:
        # Agent tự báo hoàn thành. KHÔNG tin ngay: phải có tiêu chí được Checker
        # chấm thì mới gọi là ĐẠT, nếu không là CHƯA XÁC MINH.
        verdict, conclusion, verified_issues = verified_verdict(parse_run_outcome(trace))
        issues.extend(verified_issues)
    elif task.status == "running":
        # Chưa chốt xong (worker vừa thoát hoặc task còn chạy): không được
        # kết luận KHÔNG ĐẠT khi chưa có bằng chứng thất bại.
        verdict = "running"
        conclusion = "ĐANG CHẠY – chưa có kết quả cuối."
    else:
        verdict = "fail"
        reason = ""
        if failed_match:
            reason = failed_match.group(1).strip()
        elif report.get("explanation"):
            reason = str(report["explanation"]).strip()
        issues.append(reason or "Agent báo task thất bại, không có mô tả chi tiết.")
        conclusion = "KHÔNG ĐẠT – " + issues[0]

    if (
        report.get("explanation")
        and verdict == "fail"
        and str(report["explanation"]).strip() not in issues
    ):
        issues.append(str(report["explanation"]).strip())

    noise = ("scrcpy", "ChatGoogleGenerativeAI", "StepMemoryService", "step_capsule", "Transcript prompt")
    for line in (trace.splitlines() if verdict == "fail" else []):
        if "❌" not in line or any(word in line for word in noise):
            continue
        text = line.split("❌", 1)[1].strip()
        if text and text not in issues:
            issues.append(text)
        if len(issues) >= 4:
            break

    final_png = LOG_DIR / f"{task.id}_final.png"
    final_image = f"/api/tasks/{task.id}/final.png" if final_png.exists() else (
        steps[-1]["after"] if steps and steps[-1]["after"] else ""
    )
    duration = (task.ended_at - task.started_at) if task.ended_at else 0
    return {
        "verdict": verdict,
        "conclusion": conclusion,
        "explanation": str(report.get("explanation", "") or ""),
        "issues": issues[:4],
        "steps_count": len(steps),
        "highlights": [step["label"] for step in steps[-6:]],
        "duration_seconds": round(duration, 1),
        "cost_vnd": round(task.cost_vnd, 1),
        "requests": task.requests,
        "final_image": final_image,
        "session_id": task.session_id,
    }


class RunRequest(BaseModel):
    goal: str = Field(min_length=5, max_length=4000)
    profile: str = Field(default="flash", pattern="^(flash|pro)$")
    package: str = Field(default=DEFAULT_PACKAGE, max_length=200)
    serial: str = ""
    test_name: str = ""
    max_seconds: int = Field(default=300, ge=60, le=3600)
    #: Prompt hoàn chỉnh tester đã xem và sửa trong tab Knowledge. Có thì dùng nó
    #: thay cho ``goal``, và báo ARTEMIS đừng ghép kiến thức lần nữa.
    prompt_override: str = Field(default="", max_length=12000)


app = FastAPI(title="Mobile Test Console", docs_url=None, redoc_url=None)


def edge_route(method: str, path: str, **kwargs):
    """Endpoint điều khiển thiết bị — chỉ tồn tại trên máy cắm điện thoại."""

    def decorator(func):
        return getattr(app, method)(path, **kwargs)(func) if SERVES_DEVICE else func

    return decorator


def shared_route(method: str, path: str, **kwargs):
    """Endpoint dùng chung (AI proxy, API RAG) — không có trên máy tester."""

    def decorator(func):
        return getattr(app, method)(path, **kwargs)(func) if SERVES_SHARED else func

    return decorator


# Knowledge base (Workflow RAG). Mounted only where the operator manages it (the dev
# machine), never on the shared `server` role: /api/kb/* lets a caller edit and delete
# workflows and carries no access key of its own.
if SERVES_KB_ADMIN:
    try:
        from artemis.knowledge.api import router as knowledge_router

        app.include_router(knowledge_router)
    except Exception as exc:  # noqa: BLE001 - the KB is optional
        print(f"⚠ Không nạp được API knowledge base: {exc}")
runner = Runner()
budget = Budget()


@edge_route("get", "/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


#: Trang trạng thái của vai trò ``server``. Không dùng chung giao diện console vì
#: giao diện đó gắn chặt với việc điều khiển điện thoại (danh sách thiết bị, màn
#: hình trực tiếp, chạy task) — trên VPS mọi mục đều sẽ hỏng.
_SERVER_PAGE = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ARTEMIS — máy chủ RAG + AI proxy</title>
<style>
 body{{font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      margin:0;padding:40px 20px;background:#f7f9fd;color:#1b2333}}
 .box{{max-width:640px;margin:0 auto;background:#fff;border:1px solid #e3e8f0;
      border-radius:14px;padding:26px 30px}}
 h1{{margin:0 0 4px;font-size:19px}} .sub{{color:#6b7688;margin:0 0 22px}}
 table{{border-collapse:collapse;width:100%}} td{{padding:7px 0;vertical-align:top;
      border-bottom:1px solid #eef1f6}} td:first-child{{width:190px;color:#6b7688}}
 code{{background:#f2f5fa;padding:2px 6px;border-radius:5px;font-size:13px}}
 .ok{{color:#1a7f4b;font-weight:600}} .warn{{color:#a15c00;font-weight:600}}
</style></head><body><div class="box">
<h1>ARTEMIS — máy chủ dùng chung</h1>
<p class="sub">Máy này chỉ phục vụ AI proxy và API RAG. Không điều khiển thiết bị.</p>
<table>
 <tr><td>Vai trò</td><td><code>server</code></td></tr>
 <tr><td>AI proxy</td><td><code>POST /llm/v1/chat/completions</code><br>
   <span class="sub">Cần <code>Authorization: Bearer &lt;khoá truy cập&gt;</code></span></td></tr>
 <tr><td>API RAG</td><td><code>POST /api/knowledge/context</code><br>
   <span class="sub">Gửi prompt gốc, nhận prompt hoàn chỉnh. Cũng cần khoá truy cập.</span></td></tr>
 <tr><td>Sức khoẻ</td><td><code>GET /api/health</code></td></tr>
 <tr><td>Knowledge base</td><td>{kb}</td></tr>
 <tr><td>Khoá truy cập</td><td>{access}</td></tr>
</table>
<p class="sub" style="margin-top:20px">Giao diện điều khiển thiết bị nằm trên máy
cắm điện thoại (<code>CONSOLE_ROLE=edge</code> hoặc <code>all</code>).</p>
</div></body></html>
"""


if not SERVES_DEVICE:

    @app.get("/")
    def server_status() -> HTMLResponse:
        """Trang trạng thái thay cho giao diện điều khiển."""
        kb = "<span class='warn'>chưa cấu hình</span>"
        try:
            from artemis.knowledge.service import get_service

            service = get_service()
            if service.available:
                count = len(service._store.list_workflows())
                kb = f"<span class='ok'>sẵn sàng</span> · {count} workflow"
            else:
                kb = f"<span class='warn'>suy giảm</span> · {service.degraded_reason}"
        except Exception as exc:  # noqa: BLE001 - trang trạng thái không được vỡ
            kb = f"<span class='warn'>lỗi</span> · {str(exc)[:120]}"
        access = (
            "<span class='ok'>bắt buộc</span>"
            if not is_local_llm()
            else "<span class='warn'>đang tắt (LOCAL_LLM)</span>"
        )
        return HTMLResponse(_SERVER_PAGE.format(kb=kb, access=access))


def kb_api_base() -> str:
    """Base URL của máy chủ phục vụ AI proxy + API RAG, nếu máy này không tự phục vụ.

    Máy cắm điện thoại (``edge``) không có endpoint chuẩn bị prompt và không giữ key
    LLM; nó được cấu hình ``ARTEMIS_KB_API`` trỏ về máy chủ.  Trả về đây để giao
    diện chỉ đúng địa chỉ cho tester thay vì để họ đoán.
    """
    try:
        from artemis.knowledge.integration import remote_api_url

        return remote_api_url()
    except Exception:  # noqa: BLE001 - thiếu module knowledge thì coi như không có
        return ""


@app.get("/api/health")
def health() -> dict:
    """Sức khoẻ tiến trình. Nội dung phụ thuộc vai trò.

    Vai trò ``server`` không trả đường dẫn adb/ARTEMIS hay danh sách thiết bị: máy
    chủ dùng chung không nên mô tả hệ thống tập tin của mình cho người gọi qua
    Internet, và nó cũng không có thiết bị nào để liệt kê.
    """
    base = {
        "role": CONSOLE_ROLE,
        "llm": llm_summary(),
        # Client phải gửi đúng khoá này (Authorization: Bearer) mới gọi được
        # /llm/v1. Không trả về khoá, chỉ trả về việc có bắt buộc hay không.
        "access": {
            "required": not is_local_llm(),
            "key_hint": f"…{access_key()[-4:]}" if len(access_key()) > 4 else "",
        },
    }
    if not SERVES_DEVICE:
        return base
    return {
        **base,
        "artemis_ok": artemis_launcher() is not None,
        "adb_path": adb_path(),
        "artemis_dir": str(ARTEMIS_DIR),
        # Máy này không phục vụ AI proxy/RAG (vai trò edge) ⇒ chỉ cho biết máy chủ ở đâu.
        "kb_api": kb_api_base(),
        "devices": list_devices(),
    }


@edge_route("get", "/api/devices")
def devices() -> list[dict]:
    return list_devices()


@edge_route("get", "/api/packages")
def packages(serial: str = Query(default="")) -> dict:
    """App đã cài trên thiết bị, cho ô Package chọn thay vì gõ tay."""
    return {"serial": serial, "packages": installed_packages(serial)}


@edge_route("get", "/api/screenshot")
def screenshot(serial: str = Query(default="")) -> Response:
    if not serial:
        ready = [d for d in list_devices() if d["state"] == "device"]
        if not ready:
            raise HTTPException(503, "Không có thiết bị nào sẵn sàng.")
        serial = ready[0]["serial"]
    try:
        result = subprocess.run(
            [adb_path(), "-s", serial, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise HTTPException(503, f"Chụp màn hình thất bại: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        message = result.stderr.decode("utf-8", "replace")[:300] or "Chụp màn hình thất bại."
        raise HTTPException(503, message)
    return Response(
        content=result.stdout, media_type="image/png", headers={"Cache-Control": "no-store"}
    )


@edge_route("get", "/api/status")
def status(since: int = 0) -> dict:
    data = runner.status(since)
    data["budget"] = budget.snapshot()
    return data


@edge_route("get", "/api/tasks")
def tasks(limit: int = 30) -> list[dict]:
    return runner.history(limit)


@edge_route("get", "/api/tasks/{task_id}/log")
def task_log(task_id: str) -> FileResponse:
    path = runner.task_log(task_id)
    if not path:
        raise HTTPException(404, "Không tìm thấy log của task này.")
    return FileResponse(path, media_type="text/plain; charset=utf-8")


@edge_route("get", "/api/tasks/{task_id}/steps")
def task_steps(task_id: str) -> dict:
    task = runner.get_task(task_id)
    if not task:
        raise HTTPException(404, "Không tìm thấy task.")
    return {
        "session_id": _resolve_session(task),
        "steps": _fetch_steps(task),
        "activity": _activity(task),
    }


@edge_route("get", "/api/tasks/{task_id}/summary")
def task_summary(task_id: str) -> dict:
    task = runner.get_task(task_id)
    if not task:
        raise HTTPException(404, "Không tìm thấy task.")
    return _task_summary(task)


@edge_route("get", "/api/tasks/{task_id}/final.png")
def task_final_image(task_id: str) -> FileResponse:
    path = LOG_DIR / f"{task_id}_final.png"
    if not path.exists():
        raise HTTPException(404, "Chưa có ảnh cuối.")
    return FileResponse(path, media_type="image/png")


@edge_route("get", "/api/tasks/{task_id}/image/{image_name}")
def task_image(task_id: str, image_name: str) -> Response:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", image_name):
        raise HTTPException(400, "Tên ảnh không hợp lệ.")
    try:
        import urllib.request

        url = f"{ARTEMIS_API}/api/images/{image_name}"
        with urllib.request.urlopen(url, timeout=15) as response:
            content = response.read()
            content_type = response.headers.get_content_type()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"Không tải được ảnh: {exc}") from exc
    return Response(content=content, media_type=content_type,
                    headers={"Cache-Control": "public, max-age=3600"})


@edge_route("post", "/api/run")
def run(req: RunRequest) -> dict:
    package = req.package.strip() or DEFAULT_PACKAGE
    installed = installed_packages(req.serial.strip())
    if installed and package not in installed:
        # Chạy với package không tồn tại: ARTEMIS thử 3 lần × 15s rồi mới bỏ cuộc,
        # tester chỉ thấy task treo mà app không hề mở. Chặn ngay tại đây.
        hints = suggest_packages(package, installed)
        message = f"App “{package}” chưa cài trên thiết bị."
        if hints:
            message += " Có phải bạn muốn: " + ", ".join(hints) + "?"
        else:
            message += " Chọn app ở ô Package (bấm ↻ Thiết bị để tải lại danh sách)."
        raise HTTPException(400, message)
    try:
        task = runner.start(req)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    warnings = [w for w in (metro_warning(req.serial.strip()),) if w]
    for warning in warnings:
        runner.note(warning)
    return {"ok": True, "task": asdict(task), "warnings": warnings}


@edge_route("post", "/api/stop")
def stop() -> dict:
    return {"ok": True, "message": runner.stop()}


class KnowledgeContextRequest(BaseModel):
    goal: str = Field(default="", max_length=4000)
    app_package: str | None = Field(default=None, max_length=200)
    run_id: str | None = Field(default=None, max_length=80)


@shared_route("post", "/api/knowledge/context")
def knowledge_context(req: KnowledgeContextRequest, request: Request) -> dict:
    """Prompt hoàn chỉnh cho một yêu cầu — máy tester gọi qua mạng.

    Đây là điểm vào duy nhất của luồng mới: tester gửi **prompt gốc**, máy chủ
    console gọi Google Embedding, tìm workflow tương tự trong PostgreSQL rồi ghép
    thành prompt hoàn chỉnh và trả về. Máy tester không cần PostgreSQL, không cần
    khoá embedding, không cần key LLM — chỉ cần ARTEMIS để chạy ADB.

    Có kiểm tra khoá truy cập **khi console đang ở vai trò ``server``**: lúc đó nó
    phục vụ qua LAN/VPS nên phải có khoá. Ở vai trò ``all``/``edge`` console là công
    cụ tại chỗ, phần còn lại của ``/api/*`` cũng không có khoá, nên UI gọi được mà
    không phải nhúng khoá vào trang.
    """
    if CONSOLE_ROLE == "server" and access_denied(request):
        raise HTTPException(401, "Thiếu hoặc sai khoá truy cập.")

    kb_block = ""
    kb_meta: dict = {}
    try:
        from artemis.knowledge.integration import resolve_app_id
        from artemis.knowledge.service import get_service

        service = get_service()
        if not service.available:
            kb_meta = {"degraded": True, "reason": service.degraded_reason}
        else:
            app_id = resolve_app_id(req.app_package, service)
            if app_id is None:
                kb_meta = {"app_id": None, "found": False, "reason": "app chưa có trong KB"}
            else:
                result, kb_block = service.context_for(req.goal, app_id, run_id=req.run_id)
                kb_meta = {
                    "app_id": app_id,
                    "found": result.found,
                    "workflows": [hit.workflow.id for hit in result.hits],
                    "trusted": [hit.workflow.id for hit in result.hits if hit.trusted],
                }
    except Exception as exc:  # noqa: BLE001 - KB hỏng thì trả prompt gốc, không làm gãy chạy
        kb_meta = {"degraded": True, "reason": str(exc)[:200]}

    # Chỉ ghép khi có bằng chứng từ vựng (``found``): khớp chỉ nhờ vector là "máy
    # đoán", và máy đoán không được lái agent. Không ghép được gì thì trả lại đúng
    # prompt gốc — tester vẫn chạy được, y như khi chưa có knowledge base.
    prompt = "\n\n".join(part for part in (req.goal, kb_block if kb_meta.get("found") else "") if part)
    return {
        "prompt": prompt,
        "found": bool(kb_meta.get("found")),
        "kb_prompt": kb_block if kb_meta.get("found") else "",
        "kb": kb_meta,
    }


class PrepareRequest(BaseModel):
    """Yêu cầu chuẩn bị prompt: prompt gốc + app + profile.

    ``app_id`` là id trong knowledge base; ``app_package`` là package Android mà
    tester đang thấy trong ô Package.  Gửi ``app_package`` là đủ — console tra ra
    ``app_id`` bằng đúng hàm mà lúc chạy vẫn dùng.
    """

    prompt: str = Field(min_length=5, max_length=4000)
    app_id: str | None = Field(default=None, max_length=120)
    app_package: str | None = Field(default=None, max_length=200)
    profile: str = Field(default="pro", pattern="^(flash|pro)$")
    version_id: str | None = Field(default=None, max_length=80)
    run_id: str | None = Field(default=None, max_length=80)
    #: Tester bấm "Chuẩn bị lại": bỏ qua cache, gọi LLM mới.
    refresh: bool = False


def prepare_client_key(request: Request) -> str:
    """Khoá rate limit: khoá truy cập nếu có, nếu không thì địa chỉ client.

    Trên VPS luôn có khoá truy cập nên mỗi người dùng một hạn mức; ở máy dev
    (vai trò ``all``, không khoá) thì IP là thứ duy nhất phân biệt được.
    """
    token = presented_token(request)
    if token:
        return (
            "token:" + hmac.new(b"prepare", token.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
        )
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def resolve_model_name(value: str) -> str:
    """Resolve ``${env:NAME:-default}`` trong tên model, đúng như ARTEMIS vẫn làm.

    ``config/artemis.jsonc`` dùng cơ chế nội suy của chính ARTEMIS, còn hàm đọc
    config trong console là regex nên trả về chuỗi **thô** (ví dụ
    ``${env:GEMINI35FLASHLITE_DEFAULT:-ts/gemini-3.5-flash-lite}``). Gửi nguyên
    chuỗi đó làm tên model thì gateway trả 404 — trông như proxy hỏng, trong khi
    thật ra chỉ là một chi tiết cấu hình chưa được nội suy.
    """
    if not value or "${" not in value:
        return value
    try:
        from artemis.config.core.interpolator import interpolate_config_value

        return str(interpolate_config_value(value))
    except Exception:  # noqa: BLE001 - thiếu module thì trả nguyên bản, có lý do fallback
        return value


def console_llm_chat(messages: list[dict], *, max_tokens: int, timeout: float) -> str:
    """Một lượt chat qua **AI proxy hiện có** — đúng upstream mà ``/llm/v1`` phục vụ.

    Không tạo client LLM thứ hai: console này vốn là proxy (nó giữ key thật và
    chuyển tiếp ``/llm/v1/chat/completions``), nên bước chuẩn bị dùng chính
    ``upstream_base()`` + key trong ``.env``.  Nhờ vậy máy tester không cần key LLM,
    và chỉ có một nơi cấu hình model.
    """
    env = read_env_file(ARTEMIS_ENV)
    key = (env.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    local = is_local_llm()
    if not local and not key:
        raise RuntimeError("Thiếu OPENAI_API_KEY trong Artemis .env")
    model = LOCAL_MODEL if local else resolve_model_name(llm_summary().get("model") or "")
    if not model:
        raise RuntimeError("Chưa xác định được model trong config/artemis.jsonc")
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max(1, int(max_tokens)),
        "temperature": 0,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{upstream_base()}/chat/completions", json=body, headers=headers
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Không gọi được AI proxy: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"AI proxy trả {response.status_code}: {response.text[:200]}")
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"AI proxy trả dữ liệu không đọc được: {exc}") from exc
    usage = payload.get("usage") or {}
    try:
        budget.record_prepare(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    except Exception as exc:  # noqa: BLE001 - kế toán hỏng không được chặn tester
        print(f"⚠ Không ghi được chi phí chuẩn bị prompt: {exc}")
    if isinstance(content, list):  # một số gateway trả content dạng mảng part
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


@shared_route("post", "/api/knowledge/prepare")
def knowledge_prepare(req: PrepareRequest, request: Request) -> dict:
    """Chuẩn bị Enhanced Prompt — endpoint trung tâm, chạy trên máy chủ.

    Luồng: prompt gốc → RAG truy xuất workflow → LLM tổng hợp thành một prompt thực
    thi ngắn → trả về cho tester xem/sửa rồi chạy.  Không sao chép workflow, không
    toạ độ, prompt gốc nguyên vẹn (xem ``artemis.knowledge.prepare``).

    Endpoint này **không tồn tại** ở vai trò ``edge``: máy cắm điện thoại không giữ
    key LLM.  Nó có ở ``server`` (VPS) và ``all`` (máy dev chạy một mình).

    Không bao giờ trả 5xx vì lỗi LLM: LLM hỏng thì trả prompt gốc kèm kiến thức rút
    gọn và ``status="fallback"`` — tester luôn có gì đó để chạy.
    """
    if CONSOLE_ROLE == "server" and access_denied(request):
        raise HTTPException(401, "Thiếu hoặc sai khoá truy cập.")

    from artemis.knowledge.prepare import (
        STATUS_DEGRADED,
        STATUS_FALLBACK,
        get_rate_limiter,
        prepare_prompt,
    )

    limiter = get_rate_limiter()
    allowed, retry_after = limiter.allow(prepare_client_key(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Quá nhiều yêu cầu chuẩn bị prompt (giới hạn {limiter.max_events} lần/"
                f"{limiter.window_seconds:.0f}s). Thử lại sau {retry_after:.0f}s."
            ),
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    try:
        from artemis.config.settings import settings as artemis_settings

        prepare_enabled = bool(getattr(artemis_settings, "ARTEMIS_PREPARE_ENABLED", True))
    except Exception:  # noqa: BLE001 - thiếu config thì coi như bật
        prepare_enabled = True

    model = resolve_model_name(llm_summary().get("model") or "")
    outcome = prepare_prompt(
        req.prompt,
        app_id=req.app_id,
        app_package=req.app_package,
        profile=req.profile,
        version_id=req.version_id,
        run_id=req.run_id,
        synthesizer=console_llm_chat if prepare_enabled else None,
        use_cache=not req.refresh,
        model=model,
    )

    if outcome.status == STATUS_FALLBACK:
        print(f"⚠ Chuẩn bị prompt: fallback — {outcome.fallback_reason}")
    elif outcome.status == STATUS_DEGRADED:
        print(f"⚠ Chuẩn bị prompt: knowledge base suy giảm — {outcome.fallback_reason}")

    return {
        **outcome.as_dict(),
        # `found` giữ tương thích với /api/knowledge/context để UI đọc cùng một cách.
        "found": bool(outcome.source.get("found")),
        "llm": {
            "used": outcome.used_llm,
            "model": model if outcome.used_llm else "",
            "synthesis": outcome.source.get("synthesis"),
        },
    }


def upstream_base() -> str:



    env = read_env_file(ARTEMIS_ENV)
    # Biến môi trường của tiến trình phải thắng .env, nếu không thì
    # `LOCAL_LLM=1 ./run.sh` không chuyển được sang Ollama (vì .env đã có sẵn
    # ARTEMIS_UPSTREAM_BASE_URL trỏ upstream).
    explicit = os.getenv("ARTEMIS_UPSTREAM_BASE_URL") or env.get("ARTEMIS_UPSTREAM_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    configured = env.get("OPENAI_BASE_URL", "")
    if configured and "127.0.0.1:8787" not in configured and "localhost:8787" not in configured:
        return configured.rstrip("/")
    return os.getenv("ARTEMIS_DEFAULT_UPSTREAM", "https://api.openai.com/v1")


LOCAL_MODEL = os.getenv("ARTEMIS_LOCAL_MODEL", "artemis-local")


def is_local_llm() -> bool:
    """True when the proxy points at a local Ollama server.

    In local mode the console rewrites the cloud model name (config still says
    ``ts/gemini-3.5-flash-lite``) to the local one, drops parameters Ollama does
    not accept, and prices every call at 0đ -- no key, no cloud bill.
    """
    try:
        return "11434" in upstream_base()
    except Exception:  # noqa: BLE001 - env file may be unreadable mid-edit
        return False


@edge_route("get", "/api/budget")
def budget_status() -> dict:
    return budget.snapshot()


@edge_route("post", "/api/budget/reset")
def budget_reset() -> dict:
    """Clear the current task's spend counters and its 429 block reason."""
    budget.reset()
    return {"ok": True, "budget": budget.snapshot()}


@shared_route("api_route", "/llm/v1/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def llm_proxy(path: str, request: Request) -> Response:
    """OpenAI-compatible proxy that meters every Artemis LLM call."""
    if request.method == "OPTIONS":
        return Response(status_code=204)
    local = is_local_llm()
    if access_denied(request, local):
        # Không trả về khoá trong thông báo lỗi: người gọi sai không được biết khoá.
        return JSONResponse(
            {
                "error": {
                    "message": (
                        "Khoá truy cập API thiếu hoặc sai. Đặt "
                        "OPENAI_API_KEY=<khoá truy cập> trong .env của máy chạy "
                        "ARTEMIS (xin khoá từ quản trị dự án)."
                    ),
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
            status_code=401,
        )
    raw = await request.body()
    env_key = read_env_file(ARTEMIS_ENV).get("OPENAI_API_KEY", "")
    key = env_key or "ollama-local"
    if not local and not env_key:
        return JSONResponse(
            {"error": {"message": "Thiếu OPENAI_API_KEY trong Artemis .env",
                       "type": "configuration_error", "code": "missing_key"}},
            status_code=400,
        )

    body: dict | None = None
    if path.endswith("chat/completions") and request.method == "POST":
        if not runner.is_running() and budget.snapshot()["task_id"] != "external":
            budget.start_task("external")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(
                {"error": {"message": "JSON không hợp lệ", "type": "invalid_request_error"}},
                status_code=400,
            )
        if body.get("stream"):
            body.setdefault("stream_options", {})["include_usage"] = True
        if local:
            # Ollama nhận đúng tên model local; các tham số OpenAI mở rộng
            # (thinking, reasoning_effort, stream_options, ...) sẽ làm nó 400.
            body["model"] = LOCAL_MODEL
            body.pop("max_completion_tokens", None)
            for unsupported in (
                "reasoning_effort",
                "thinking",
                "stream_options",
                "parallel_tool_calls",
                "service_tier",
                "store",
                "metadata",
            ):
                body.pop(unsupported, None)
        reason = budget.check(body, len(raw))
        if reason:
            return JSONResponse(
                {"error": {"message": "BUDGET: " + reason, "type": "budget_exceeded",
                           "code": "budget_exceeded"}},
                status_code=429,
            )
        body = budget.clamp_output(body)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": request.headers.get("content-type", "application/json"),
        "Accept": request.headers.get("accept", "application/json"),
    }
    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=20.0), trust_env=False)
    target = f"{upstream_base()}/{path}"
    upstream = await client.send(
        client.build_request(request.method, target, content=raw, headers=headers), stream=True
    )
    content_type = upstream.headers.get("content-type", "application/json")

    if body and body.get("stream") and "text/event-stream" in content_type:
        async def streamer():
            buffer = ""
            usage = None
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
                    buffer += chunk.decode("utf-8", "ignore")
                    lines = buffer.split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload or payload == "[DONE]":
                            continue
                        try:
                            event = json.loads(payload)
                        except ValueError:
                            continue
                        if event.get("usage"):
                            usage = event["usage"]
            finally:
                await upstream.aclose()
                await client.aclose()
                if upstream.status_code == 200 and body is not None:
                    if usage:
                        budget.record(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
                    else:
                        budget.record(0, 0)
        return StreamingResponse(streamer(), status_code=upstream.status_code, media_type=content_type)

    content = await upstream.aread()
    await upstream.aclose()
    await client.aclose()
    if body is not None and upstream.status_code == 200:
        try:
            data = json.loads(content)
            usage = data.get("usage") or {}
            budget.record(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        except ValueError:
            budget.record(0, 0)
    response_type = "application/json" if body is not None and not body.get("stream") else content_type
    return Response(content=content, status_code=upstream.status_code, media_type=response_type)


@edge_route("post", "/api/llm-test")
def llm_test() -> JSONResponse:
    """GET {OPENAI_BASE_URL}/models to verify the key and endpoint."""
    import urllib.error
    import urllib.request

    env = read_env_file(ARTEMIS_ENV)
    key = env.get("OPENAI_API_KEY", "")
    base = env.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if not key:
        return JSONResponse({"ok": False, "error": "Chưa có OPENAI_API_KEY trong .env"}, 400)
    request = urllib.request.Request(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
        ids = [item.get("id", "") for item in payload.get("data", [])]
        interesting = [
            model for model in ids
            if any(tag in model.lower() for tag in ("claude", "gpt", "gemini", "qwen", "deepseek"))
        ]
        # /models only proves the key is accepted; a tiny chat proves quota.
        # 32 tokens (not 1): thinking models spend the budget on reasoning and
        # would return an empty message -> misleading EMPTY_OUTPUT / 502.
        model = llm_summary()["model"]
        chat_body = json.dumps(
            {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 32}
        ).encode("utf-8")
        chat_request = urllib.request.Request(
            f"{base}/chat/completions",
            data=chat_body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(chat_request, timeout=30) as response:
                response.read()
            return JSONResponse(
                {"ok": True, "base_url": base, "model": model, "count": len(ids),
                 "models": interesting[:60]}
            )
        except urllib.error.HTTPError as exc:
            return JSONResponse(
                {"ok": False, "stage": "chat", "base_url": base, "model": model,
                 "count": len(ids), "status": exc.code,
                 "error": exc.read().decode("utf-8", "replace")[:400]}
            )
    except urllib.error.HTTPError as exc:
        return JSONResponse(
            {"ok": False, "stage": "models", "base_url": base, "status": exc.code,
             "error": exc.read().decode("utf-8", "replace")[:400]}
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        return JSONResponse({"ok": False, "base_url": base, "error": str(exc)[:400]})


if __name__ == "__main__":
    import uvicorn

    print(f"Mobile Test Console: http://{HOST}:{PORT}")
    print(f"  Artemis: {ARTEMIS_DIR}")
    print(f"  adb    : {adb_path()}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
