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
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
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
IDLE_TIMEOUT_SECONDS = int(os.getenv("TASK_IDLE_TIMEOUT_SECONDS", "120"))
ARTEMIS_API = os.getenv("ARTEMIS_API_BASE", "http://127.0.0.1:8000").rstrip("/")
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
    Returns None when neither is available.
    """
    if ARTEMIS_BIN.exists():
        return [str(ARTEMIS_BIN)]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--project", str(ARTEMIS_DIR), "artemis"]
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
        "base_url": env.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "key_present": bool(key),
        "key_hint": f"…{key[-4:]}" if key and len(key) > 8 else "",
    }


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

    def check(self, body: dict, raw_size: int) -> str | None:
        """Return a block reason, or None when the request may proceed."""
        with self._lock:
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

    def start(self, req: "RunRequest") -> Task:
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
                goal=req.goal.strip(),
                profile=req.profile,
                package=req.package.strip() or DEFAULT_PACKAGE,
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
            *launcher, "run", task.goal,
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

        self._append(f"$ {' '.join(cmd)}")
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

    def _pump(self, proc: subprocess.Popen[str], task: Task) -> None:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                text = raw.rstrip("\n")
                self._append(text)
                if not task.session_id:
                    match = SESSION_RE.search(text)
                    if match:
                        task.session_id = match.group(0)
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
    if task.session_id:
        return task.session_id
    try:
        sessions = _http_json(f"{ARTEMIS_API}/api/sessions", timeout=6)
    except Exception:  # noqa: BLE001 - Artemis UI may be down
        return ""
    for session in sessions:
        if session.get("initial_goal") != task.goal:
            continue
        try:
            delta = abs(float(session.get("start_time", 0)) - task.started_at)
        except (TypeError, ValueError):
            continue
        if delta < 180:
            task.session_id = session.get("session_id", "")
            return task.session_id
    return ""


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


def _read_log(task: Task) -> str:
    try:
        return Path(task.log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


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
        verdict = "pass"
        conclusion = "ĐẠT – Agent báo đã hoàn thành mục tiêu."
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


app = FastAPI(title="Mobile Test Console", docs_url=None, redoc_url=None)
runner = Runner()
budget = Budget()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {
        "artemis_ok": artemis_launcher() is not None,
        "adb_path": adb_path(),
        "artemis_dir": str(ARTEMIS_DIR),
        "devices": list_devices(),
        "llm": llm_summary(),
    }


@app.get("/api/devices")
def devices() -> list[dict]:
    return list_devices()


@app.get("/api/screenshot")
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


@app.get("/api/status")
def status(since: int = 0) -> dict:
    data = runner.status(since)
    data["budget"] = budget.snapshot()
    return data


@app.get("/api/tasks")
def tasks(limit: int = 30) -> list[dict]:
    return runner.history(limit)


@app.get("/api/tasks/{task_id}/log")
def task_log(task_id: str) -> FileResponse:
    path = runner.task_log(task_id)
    if not path:
        raise HTTPException(404, "Không tìm thấy log của task này.")
    return FileResponse(path, media_type="text/plain; charset=utf-8")


@app.get("/api/tasks/{task_id}/steps")
def task_steps(task_id: str) -> dict:
    task = runner.get_task(task_id)
    if not task:
        raise HTTPException(404, "Không tìm thấy task.")
    return {"session_id": _resolve_session(task), "steps": _fetch_steps(task)}


@app.get("/api/tasks/{task_id}/summary")
def task_summary(task_id: str) -> dict:
    task = runner.get_task(task_id)
    if not task:
        raise HTTPException(404, "Không tìm thấy task.")
    return _task_summary(task)


@app.get("/api/tasks/{task_id}/final.png")
def task_final_image(task_id: str) -> FileResponse:
    path = LOG_DIR / f"{task_id}_final.png"
    if not path.exists():
        raise HTTPException(404, "Chưa có ảnh cuối.")
    return FileResponse(path, media_type="image/png")


@app.get("/api/tasks/{task_id}/image/{image_name}")
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


@app.post("/api/run")
def run(req: RunRequest) -> dict:
    try:
        task = runner.start(req)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "task": asdict(task)}


@app.post("/api/stop")
def stop() -> dict:
    return {"ok": True, "message": runner.stop()}


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


@app.get("/api/budget")
def budget_status() -> dict:
    return budget.snapshot()


@app.api_route("/llm/v1/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def llm_proxy(path: str, request: Request) -> Response:
    """OpenAI-compatible proxy that meters every Artemis LLM call."""
    if request.method == "OPTIONS":
        return Response(status_code=204)
    raw = await request.body()
    local = is_local_llm()
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


@app.post("/api/llm-test")
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
        # /models only proves the key is accepted; a 1-token chat proves quota.
        model = llm_summary()["model"]
        chat_body = json.dumps(
            {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
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
