# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""`artemis llm check` - prove the configured model endpoint really answers.

Prints the raw response body when the call fails, which is where provider
"JSON errors" (401, model-not-found, tool-calling unsupported, budget 429,
HTML from a wrong base URL) become readable instead of a generic stack trace.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib import error, request

from rich.console import Console
from rich.table import Table
import typer

from artemis.config.settings import is_placeholder_key, settings

llm_app = typer.Typer(
    help="Check the configured LLM endpoint (base URL, key, model, tool calling).",
    no_args_is_help=True,
)
console = Console()

#: Error substrings that identify a provider-side problem, checked in order.
_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("invalid api key", "incorrect api key", "unauthorized", "authentication"),
        "API key bị từ chối. Kiểm tra OPENAI_API_KEY (đúng key của provider đang trỏ tới).",
    ),
    (
        ("model not found", "does not exist", "unknown model", "no such model"),
        "Model không tồn tại ở provider này. Sửa `default.model` trong config/artemis.jsonc.",
    ),
    (
        ("tools", "function calling", "tool_choice"),
        "Model không hỗ trợ tool calling; ARTEMIS cần model có tools + vision.",
    ),
    (
        ("context length", "too many tokens", "maximum context"),
        "Prompt vượt context. Giảm XML UI trong prompt hoặc bật step summarizer.",
    ),
    (("quota", "insufficient", "billing", "credit"), "Hết quota/credit của provider."),
    (
        ("budget_exceeded", "429"),
        "Chạm hạn mức ngân sách (proxy 8787) hoặc rate limit của provider.",
    ),
    (
        ("<!doctype", "<html"),
        "Endpoint trả HTML, không phải JSON: BASE URL sai (thiếu /v1) hoặc bị proxy chặn.",
    ),
)

@dataclass
class LlmCheckResult:
    provider: str
    model: str
    base_url: str
    key_present: bool
    key_hint: str
    chat_status: int | None = None
    chat_error: str | None = None
    reply: str | None = None
    elapsed_seconds: float | None = None
    ok: bool = False

    def hint(self) -> str | None:
        haystack = (self.chat_error or "").lower()
        if not haystack:
            return None
        for needles, hint in _HINTS:
            if any(needle in haystack for needle in needles):
                return hint
        return "Xem nguyên văn body lỗi ở trên để biết provider từ chối vì lý do gì."

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "hint": self.hint()}


def _default_model_from_config() -> tuple[str, str]:
    """(provider, model) from artemis.jsonc `default`, with safe fallbacks."""
    try:
        from artemis.config.paths import get_config_path
        from third_party.mobile_use.utils.file import load_jsonc

        with open(Path(get_config_path("artemis.jsonc")), encoding="utf-8") as handle:
            payload = load_jsonc(handle)
        block = payload.get("default") or {}
        provider = str(block.get("provider") or "openai")
        model = str(block.get("model") or "")
        if model:
            return provider, model
    except Exception:  # noqa: BLE001 - diagnostics must never crash on config issues
        pass
    return "openai", "gpt-4o-mini"


def _api_key() -> str:
    key = settings.OPENAI_API_KEY
    value = key.get_secret_value() if key is not None else os.environ.get("OPENAI_API_KEY", "")
    value = (value or "").strip()
    return "" if is_placeholder_key(value) else value


def _call_chat(base_url: str, model: str, key: str, timeout: float) -> tuple[int, float, str]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 32,
    }
    req = request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    started = time.time()
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, round(time.time() - started, 2), body
    except error.HTTPError as exc:
        return exc.code, round(time.time() - started, 2), exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - network failures are the point
        return 0, round(time.time() - started, 2), f"{type(exc).__name__}: {exc}"


def run_llm_check(timeout: float = 45.0) -> LlmCheckResult:
    provider, model = _default_model_from_config()
    base_url = str(settings.OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
    key = _api_key()
    result = LlmCheckResult(
        provider=provider,
        model=model,
        base_url=base_url,
        key_present=bool(key),
        key_hint=(f"…{key[-4:]}" if len(key) > 8 else ("local" if key else "-")),
    )
    if not key:
        result.chat_error = "Chưa có OPENAI_API_KEY trong .env"
        return result

    status, elapsed, body = _call_chat(base_url, model, key, timeout)
    result.chat_status = status
    result.elapsed_seconds = elapsed
    if status != 200:
        result.chat_error = body.strip()[:1200] or f"HTTP {status}"
        return result

    try:
        payload = json.loads(body)
        message = (payload.get("choices") or [{}])[0].get("message") or {}
        result.reply = (message.get("content") or "").strip()[:200]
        result.ok = True
    except (ValueError, AttributeError, IndexError):
        result.chat_error = body.strip()[:1200]
    return result


@llm_app.command("check")
def check_command(
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    timeout: float = typer.Option(45.0, "--timeout", help="Request timeout in seconds."),
) -> None:
    """Call the configured model once and show the raw response."""
    result = run_llm_check(timeout=timeout)
    if as_json:
        console.print_json(json.dumps(result.to_dict(), ensure_ascii=False))
        if not result.ok:
            raise typer.Exit(code=1)
        return

    table = Table(show_header=False, box=None)
    table.add_row("Provider", result.provider)
    table.add_row("Model", result.model)
    table.add_row("Base URL", result.base_url)
    table.add_row("API key", f"có ({result.key_hint})" if result.key_present else "[red]thiếu[/red]")
    console.print(table)

    if result.ok:
        console.print(
            f"[green]✓ chat/completions 200[/green] trong {result.elapsed_seconds}s · "
            f"trả lời: {result.reply or '(rỗng)'}"
        )
        return

    console.print(f"[bold red]✖ Gọi model thất bại[/bold red] (HTTP {result.chat_status})")
    console.print(f"[dim]Body provider trả về:[/dim]\n{result.chat_error}")
    hint = result.hint()
    if hint:
        console.print(f"[yellow]💡 {hint}[/yellow]")
    raise typer.Exit(code=1)

