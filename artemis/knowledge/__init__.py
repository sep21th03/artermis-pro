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

"""Workflow knowledge base (RAG) for ARTEMIS.

Entry point: :func:`artemis.knowledge.service.get_service`, then
``retrieve(query, app_id)`` or ``context_for(...)``.  Everything degrades to
"no guidance available" rather than raising into a run.
"""

from artemis.knowledge.context import build_context
from artemis.knowledge.integration import (
    KnowledgeContext,
    attach,
    context_meta,
    context_text,
    record_outcome,
    retrieve_for_run,
    usage_report,
)
from artemis.knowledge.models import (
    RetrievalResult,
    Workflow,
    WorkflowHit,
    WorkflowStep,
)
from artemis.knowledge.service import (
    KnowledgeService,
    get_service,
    reset_service,
)
from artemis.knowledge.stores.base import KnowledgeUnavailable

__all__ = [
    "KnowledgeContext",
    "KnowledgeService",
    "KnowledgeUnavailable",
    "RetrievalResult",
    "Workflow",
    "WorkflowHit",
    "WorkflowStep",
    "attach",
    "build_context",
    "context_meta",
    "context_text",
    "get_service",
    "record_outcome",
    "reset_service",
    "retrieve_for_run",
    "usage_report",
]
