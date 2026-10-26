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

"""Shared step-memory runtime for background summarization across profiles."""

from artemis.memory.chunking import (
    ChunkCapsuleService,
    HistoryChunkManager,
    StepCapsuleLens,
    build_action_ledger,
)
from artemis.memory.context_policy import (
    CONTEXT_POLICIES,
    ContextPolicy,
    build_history_for,
    resolve_policy,
)
from artemis.memory.step_memory import StepLens, StepMemoryService
from artemis.memory.transcript import TranscriptLedger, format_session_offset
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


def ensure_step_memory(ctx):
    """Return the session's shared step-memory service, or ``None`` when it is off.

    Composition root for ``ctx.step_memory`` (history redesign §6.2): the
    first caller — FlashRunner, the Pro SummarizerNode, or the Pro operator's
    transcript path — instantiates the visual-transition lens service and
    publishes it on the context so every profile shares one runtime.

    Returns ``None`` when ``flash.step_summarizer.enabled`` is false.  That flag
    used to be honoured **only** by ``FlashRunner``; every other profile built the
    service anyway and paid for it.  Measured on a Pro run: 15 steps x 4 attempts
    = **60 vision calls, every one a 404**, because the service calls Google
    directly (``get_google_llm``) with a model name that endpoint does not know.
    Callers must handle ``None``.
    """
    service = getattr(ctx, "step_memory", None)
    if service is not None:
        return service

    from artemis.agents.flash.summarizer import VisualStepSummarizer

    kwargs: dict = {}
    model_name = None
    enabled = True
    try:
        from artemis.config import load_agent_config

        cfg = load_agent_config()
        summarizer_cfg = cfg.flash.step_summarizer
        enabled = bool(summarizer_cfg.enabled)
        model_name = summarizer_cfg.model
        kwargs = {
            "retry_limit": cfg.memory.runtime.retry_limit,
            "max_concurrency": cfg.memory.runtime.max_concurrency,
            "flush_timeout_s": cfg.memory.runtime.flush_timeout_s,
        }
    except Exception as exc:
        logger.debug(
            f"Agent config unavailable; using step-memory service defaults: {exc}",
            exc_info=True,
        )

    if not enabled:
        logger.info(
            "Step summarizer tắt (flash.step_summarizer.enabled=false): "
            "không tạo dịch vụ tóm tắt bước."
        )
        return None

    service = VisualStepSummarizer(ctx, model_name=model_name, **kwargs)
    try:
        ctx.step_memory = service
    except (AttributeError, TypeError, ValueError):
        pass
    return service


__all__ = [
    "CONTEXT_POLICIES",
    "ChunkCapsuleService",
    "ContextPolicy",
    "HistoryChunkManager",
    "StepCapsuleLens",
    "build_history_for",
    "resolve_policy",
    "StepLens",
    "StepMemoryService",
    "TranscriptLedger",
    "build_action_ledger",
    "ensure_step_memory",
    "format_session_offset",
]
