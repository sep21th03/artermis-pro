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

"""Router của bộ nén ngữ cảnh: không còn 404, không âm thầm đổi provider.

Nguyên nhân gốc được khoá bằng test ở đây: bộ nén từng gọi ``get_google_llm`` — hàm đó
*ép* ``provider=google`` — trong khi tên model đến từ ``config/artemis.jsonc`` và là tên
của gateway OpenAI-compatible.  Kết quả đo được: mọi lượt nén trả 404.

Bốn tính chất được khoá:

1. Provider/endpoint/khoá lấy từ cấu hình, ``model_name`` chỉ ghi đè *tên model*.
2. Không resolve được ⇒ trả ``None`` để giữ nguyên context, không raise, **không** tự
   chuyển sang provider khác.
3. Lỗi nén không làm gãy tác vụ chính: lens trả ``None`` (không có capsule) và ghi lại
   lý do vào performance log.
4. Số đo nén (token trước/sau, thời lượng, lỗi) vào performance log và **ghi rõ** token
   ở đây là ước lượng, không phải số provider báo.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from artemis.memory import chunking as chunking_module
from artemis.perf import PerfRecorder, PerfStage, set_recorder
from artemis.services import llm as llm_module

REPO = Path(__file__).resolve().parents[2]


# ── Đối tượng giả cho cấu hình ───────────────────────────────────────────
class FakeCfg:
    def __init__(self, provider: str = "openai", model: str = "ts/gemini-3.5-flash-lite"):
        self.provider = provider
        self.model = model
        self.temperature = 0.0
        self.timeout = 45.0


class FakeLLMConfig:
    def __init__(self, cfg: FakeCfg):
        self._cfg = cfg

    def get_utils(self, name: str):
        return self._cfg

    def get_agent(self, name: str):
        return self._cfg


def fake_ctx(provider: str = "openai", model: str = "ts/gemini-3.5-flash-lite"):
    return SimpleNamespace(llm_config=FakeLLMConfig(FakeCfg(provider, model)), data_engine=None)


class FakeModel:
    """Model thô tối thiểu: ``with_config`` trả về chính nó."""

    def with_config(self, *args, **kwargs):
        return self


@pytest.fixture
def captured_endpoint(monkeypatch):
    """Chặn ở tầng factory để bắt ``ModelEndpoint`` mà router thực sự dùng."""
    seen: dict = {}

    def fake_get_model(endpoint):
        seen["endpoint"] = endpoint
        return FakeModel()

    monkeypatch.setattr(llm_module.ModelFactory, "get_model", staticmethod(fake_get_model))
    monkeypatch.setattr(llm_module, "DataEngineCallbackHandler", lambda ctx: None)
    return seen


# ── Router ───────────────────────────────────────────────────────────────
def test_routed_llm_uses_the_configured_provider_not_google(captured_endpoint):
    wrapper = llm_module.get_routed_llm(fake_ctx(provider="openai"), role="summarizer")

    assert wrapper is not None
    endpoint = captured_endpoint["endpoint"]
    assert endpoint.provider.value == "openai"  # KHÔNG phải google
    assert endpoint.model_name == "ts/gemini-3.5-flash-lite"


def test_a_model_override_changes_only_the_model_name(captured_endpoint):
    llm_module.get_routed_llm(
        fake_ctx(provider="openai", model="ts/gemini-3.5-flash-lite"),
        role="summarizer",
        model_name="ts/gemini-3.5-flash-lite-small",
    )

    endpoint = captured_endpoint["endpoint"]
    assert endpoint.model_name == "ts/gemini-3.5-flash-lite-small"
    assert endpoint.provider.value == "openai"  # provider vẫn theo cấu hình
    assert endpoint.timeout_seconds == 45.0  # timeout của cấu hình được giữ


def test_routed_llm_keeps_a_configured_google_provider_when_that_is_the_config(captured_endpoint):
    """Nếu cấu hình *thật sự* là google thì vẫn dùng google — không tự đổi ngược lại."""
    llm_module.get_routed_llm(fake_ctx(provider="google", model="gemini-3.8-flash"))

    assert captured_endpoint["endpoint"].provider.value == "google"


def test_routed_llm_returns_none_instead_of_switching_provider(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("config hỏng")

    monkeypatch.setattr(llm_module, "_resolve_endpoint", boom)

    assert llm_module.get_routed_llm(fake_ctx(), role="summarizer") is None


def test_routed_llm_without_ctx_is_none():
    assert llm_module.get_routed_llm(None, role="summarizer") is None


def test_the_compression_pipeline_no_longer_calls_google_directly():
    """Chốt chặn hồi quy: không file nào của đường nén được *gọi* ``get_google_llm``.

    Dùng AST chứ không tìm chuỗi: docstring có nhắc tên hàm này để giải thích lỗi cũ, và
    một phép so chuỗi sẽ báo nhầm chính phần giải thích đó.
    """
    import ast

    files = [
        REPO / "artemis" / "memory" / "chunking.py",
        REPO / "artemis" / "agents" / "flash" / "summarizer.py",
        REPO / "artemis" / "agents" / "flash" / "runner.py",
    ]
    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "get_google_llm":
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], f"còn gọi thẳng Google: {offenders}"


# ── Lens nén: giữ nguyên context khi không nén được ──────────────────────
PAYLOAD = {"start_step": 1, "end_step": 1, "turns": [], "steps": [], "milestone_label": ""}


class FakeResponse:
    def __init__(self, content: str):
        self.content = content


class FakeChat:
    def __init__(self, *, content: str = "{}", error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return FakeResponse(self.content)


@pytest.fixture
def recorder(tmp_path):
    rec = PerfRecorder("compress", path=tmp_path / "perf_events.jsonl", enabled=True)
    set_recorder(rec)
    yield rec
    set_recorder(None)


def make_lens(monkeypatch, *, routed_llm, model_name="ts/gemini-3.5-flash-lite"):
    """Lens với router bị chặn; ``get_google_llm`` bị cấm gọi."""
    lens = chunking_module.StepCapsuleLens(
        model_name=model_name, ctx=fake_ctx(), fallback_model_name=None
    )

    def routed(*args, **kwargs):
        return routed_llm

    def forbidden(*args, **kwargs):  # pragma: no cover - chỉ chạy nếu code quay lại lỗi cũ
        raise AssertionError("Đường nén không được gọi get_google_llm nữa")

    monkeypatch.setattr(llm_module, "get_routed_llm", routed)
    monkeypatch.setattr(llm_module, "get_google_llm", forbidden)
    return lens


def test_without_a_routable_model_the_lens_keeps_the_context(recorder, monkeypatch):
    lens = make_lens(monkeypatch, routed_llm=None)

    result = asyncio.run(lens.render("1-1", PAYLOAD))

    assert result is None  # không có capsule ⇒ context gốc được giữ
    event = recorder.snapshot()[-1]
    assert event.stage == PerfStage.COMPRESSION
    assert event.status == "skipped"
    assert event.detail.get("reason")
    assert lens.unavailable_reason


def test_a_successful_capsule_records_the_size_reduction(recorder, monkeypatch):
    big_transcript = "x" * 4000
    payload = {**PAYLOAD, "turns": [{"steps": [], "transcript": big_transcript}]}
    lens = make_lens(monkeypatch, routed_llm=FakeChat(content='{"ok": true}'))
    monkeypatch.setattr(
        chunking_module.StepCapsuleLens, "parse_capsule", lambda self, text, payload: {"ok": True}
    )

    capsule = asyncio.run(lens.render("1-1", payload))

    assert capsule
    event = recorder.snapshot()[-1]
    assert event.status == "success"
    assert event.input_tokens is not None and event.output_tokens is not None
    assert event.input_tokens > event.output_tokens
    assert event.detail["saved_tokens"] > 0
    assert "ước lượng" in event.detail["estimate"]
    assert event.model == "ts/gemini-3.5-flash-lite"


def test_a_failing_compression_call_is_recorded_and_never_swallows_the_error(recorder, monkeypatch):
    lens = make_lens(monkeypatch, routed_llm=FakeChat(error=RuntimeError("gateway 503")))
    monkeypatch.setattr(
        chunking_module.StepCapsuleLens, "parse_capsule", lambda self, text, payload: {"ok": True}
    )

    with pytest.raises(RuntimeError):
        asyncio.run(lens.render("1-1", PAYLOAD))

    event = recorder.snapshot()[-1]
    assert event.status == "error"
    assert "gateway 503" in event.detail["reason"]


def test_an_unparseable_capsule_keeps_the_context_and_is_recorded(recorder, monkeypatch):
    lens = make_lens(monkeypatch, routed_llm=FakeChat(content="không phải JSON"))
    monkeypatch.setattr(
        chunking_module.StepCapsuleLens, "parse_capsule", lambda self, text, payload: None
    )

    assert asyncio.run(lens.render("1-1", PAYLOAD)) is None

    event = recorder.snapshot()[-1]
    assert event.status == "error"
    assert "parse" in event.detail["reason"]


# ── Breakdown ngữ cảnh trong performance log ─────────────────────────────
def test_context_breakdown_counts_chars_per_source_and_images():
    """Mỗi lượt gọi tự khai báo token đi đâu; ảnh đếm riêng, không tính là text."""
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content="s" * 400),
        HumanMessage(
            content=[
                {"type": "text", "text": "h" * 200},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "z" * 900}},
            ]
        ),
    ]

    breakdown = llm_module._context_breakdown(messages)

    assert breakdown["chars_by_role"]["system"] == 400
    assert breakdown["chars_by_role"]["human"] == 200
    assert breakdown["images"] == 1
    assert breakdown["chars_total"] == 600  # base64 của ảnh không bị tính thành text
    assert "ước lượng" in breakdown["estimate"]


def test_context_breakdown_never_raises_on_odd_input():
    assert llm_module._context_breakdown(None)["chars_total"] == 0
    assert llm_module._context_breakdown([object()])["chars_total"] == 0
