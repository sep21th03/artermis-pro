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

"""Model per function, selected from .env.

``config/artemis.jsonc`` reads each node's model from a ``<MODEL>_<FUNCTION>``
environment variable (``${env:VAR:-fallback}``) so a model can be swapped by
editing one line of ``.env`` — no config edit, no code edit. With the variable
unset the old hard-coded model must still apply, otherwise simply upgrading
would change behaviour (and cost) silently.
"""

import json
from pathlib import Path

from artemis.config.core.engine import _strip_json_comments
from artemis.config.core.interpolator import interpolate_config_value

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "artemis.jsonc"
DEFAULT_MODEL = "ts/gemini-3.5-flash-lite"

MODEL_ENV_VARS = (
    "GEMINI35FLASHLITE_DEFAULT",
    "GEMINI35FLASH_OPERATOR",
    "GEMINI35FLASH_OPERATOR_PROVIDER",
    "GEMINI35FLASH_EXPLORER",
    "GEMINI35FLASH_EXPLORER_PROVIDER",
    "GEMINI35FLASH_PLANNER",
    "GEMINI35FLASH_PLANNER_PROVIDER",
    "GEMINI35FLASH_CHECKER",
    "GEMINI35FLASH_CHECKER_PROVIDER",
    "GEMINIER_OBJECT_DETECTOR",
    "GEMINIER_OBJECT_DETECTOR_PROVIDER",
    "GEMINI35FLASHLITE_HOPPER",
    "GEMINI35FLASHLITE_SUMMARIZER",
)


def _resolved_config(monkeypatch, **env):
    """Config đã interpolate, với các biến model ở trạng thái chỉ định."""
    for name in MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    cleaned = _strip_json_comments(CONFIG_PATH.read_text(encoding="utf-8"))
    return interpolate_config_value(json.loads(cleaned))


def _all_models(config):
    models = [config["default"]["model"], config["default"]["fallback"]["model"]]
    for node in config["nodes"].values():
        if isinstance(node, dict) and node.get("model"):
            models.append(node["model"])
        if isinstance(node, dict) and isinstance(node.get("fallback"), dict):
            if node["fallback"].get("model"):
                models.append(node["fallback"]["model"])
    return models


def test_every_model_falls_back_to_the_previous_value(monkeypatch):
    config = _resolved_config(monkeypatch)
    assert config["default"]["model"] == DEFAULT_MODEL
    for node in ("planner", "operator", "explorer", "object_detector", "hopper", "checker"):
        assert config["nodes"][node]["model"] == DEFAULT_MODEL, node
    assert config["agent"]["flash"]["step_summarizer"]["model"] == DEFAULT_MODEL
    assert config["agent"]["memory"]["chunking"]["model"] == DEFAULT_MODEL


def test_env_swaps_only_the_named_function(monkeypatch):
    config = _resolved_config(monkeypatch, 
        GEMINI35FLASH_OPERATOR="ts/gemini-3.5-flash",
        GEMINI35FLASH_EXPLORER="ts/gemini-3.5-flash",
        GEMINIER_OBJECT_DETECTOR="ts/gemini-robotics-er-2-preview",
    )
    assert config["nodes"]["operator"]["model"] == "ts/gemini-3.5-flash"
    assert config["nodes"]["explorer"]["model"] == "ts/gemini-3.5-flash"
    assert config["nodes"]["object_detector"]["model"] == "ts/gemini-robotics-er-2-preview"
    # Việc phụ vẫn dùng model rẻ: đổi model không được kéo theo chi phí ở đây.
    assert config["nodes"]["hopper"]["model"] == DEFAULT_MODEL
    assert config["agent"]["memory"]["chunking"]["model"] == DEFAULT_MODEL


def test_no_unresolved_placeholder_is_left_in_a_model_name(monkeypatch):
    config = _resolved_config(monkeypatch)
    for model in _all_models(config):
        assert isinstance(model, str)
        assert "${" not in model, model


def test_object_detector_fallback_stays_on_vilao(monkeypatch):
    """Free tier có thể hết hạn mức: fallback phải về Vilao, không phụ thuộc Google."""
    config = _resolved_config(monkeypatch, 
        GEMINIER_OBJECT_DETECTOR_PROVIDER="google",
        GEMINIER_OBJECT_DETECTOR="gemini-robotics-er-2-preview",
    )
    node = config["nodes"]["object_detector"]
    assert node["provider"] == "google"
    assert node["model"] == "gemini-robotics-er-2-preview"
    assert node["fallback"] == {"provider": "openai", "model": DEFAULT_MODEL}


def test_provider_is_interpolated_per_function(monkeypatch):
    config = _resolved_config(monkeypatch, GEMINI35FLASH_OPERATOR_PROVIDER="google")
    assert config["nodes"]["operator"]["provider"] == "google"
    # Không đụng tới chức năng khác.
    assert config["nodes"]["explorer"]["provider"] == "openai"
    assert config["nodes"]["hopper"]["provider"] == "openai"


def test_every_provider_defaults_to_vilao(monkeypatch):
    config = _resolved_config(monkeypatch)
    for node in ("planner", "operator", "explorer", "object_detector", "hopper", "checker"):
        assert config["nodes"][node]["provider"] == "openai", node


# --- Loader thật phải interpolate --------------------------------------------
# Regression: `parse_llm_config` / `load_agent_config` đọc file trực tiếp nên
# `${env:...}` lọt thẳng vào các field Literal → "5 validation errors for
# LLMConfig" và task KHÔNG CHẠY ĐƯỢC. Test dưới gọi đúng loader của runtime.


def test_llm_config_loader_interpolates_placeholders():
    from artemis.config.llm import parse_llm_config

    config = parse_llm_config()  # trước khi sửa: ném ValidationError
    for node in (config.utils.object_detector, config.operator, config.utils.hopper):
        assert "${" not in str(node.provider)
        assert "${" not in str(node.model)


def test_llm_config_loader_honours_env_override(monkeypatch):
    from artemis.config.llm import parse_llm_config

    monkeypatch.setenv("GEMINI35FLASH_OPERATOR_PROVIDER", "google")
    monkeypatch.setenv("GEMINI35FLASH_OPERATOR", "gemini-3.5-flash")
    config = parse_llm_config()
    assert config.operator.provider == "google"
    assert config.operator.model == "gemini-3.5-flash"


def test_agent_config_loader_interpolates_placeholders():
    from artemis.config.agent import load_agent_config

    config = load_agent_config()
    assert "${" not in config.flash.step_summarizer.model
    assert "${" not in config.memory.chunking.model
