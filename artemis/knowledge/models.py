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

"""Structured workflow knowledge: the shape of what the agent may be told.

A workflow is *intent*, never a macro.  Every step describes what to look for
("tab cuối cùng trên bottom navigation"), not where to tap, so the Operator
resolves it against the live UI hierarchy and a layout change degrades the
workflow instead of breaking it.

Everything here is a validation boundary: workflow JSON coming from a tester,
an API client or a learned session must survive ``model_validate`` before it can
reach a prompt.  ``extra="forbid"`` is deliberate -- a misspelled key is a
silent behaviour change otherwise.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Actions a workflow step may declare.  A closed vocabulary: an unknown action
#: would reach the Operator as free text, which is the ambiguity the structured
#: context exists to remove.
STEP_ACTIONS = frozenset(
    {
        "tap",
        "long_press",
        "scroll",
        "swipe",
        "input_text",
        "back",
        "confirm_if_present",
        "wait",
        "assert",
    }
)

WORKFLOW_STATUSES = ("draft", "verified", "deprecated")
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: Retrieval channels whose agreement counts as evidence that a workflow really
#: is the one being asked about.  The vector channel is excluded on purpose: a
#: nearest-neighbour query always returns rows, so it can rank but never testify.
LEXICAL_CHANNELS = frozenset({"exact", "keyword", "filtered"})
"""Kênh chứng minh "đúng là mục này", không phải "gần nghĩa".

``filtered`` nằm ở đây vì bộ lọc metadata là một phát biểu **có cấu trúc** của người gọi
("màn hình `home`, loại `modal`"), không phải một phỏng đoán ngữ nghĩa — nếu xếp nó vào kênh ngữ
nghĩa thì kết quả đúng bị gắn nhãn "chỉ là phỏng đoán" và bị :func:`decide_found` từ chối.
"""


class WorkflowStep(BaseModel):
    """One intent in a workflow, resolved against the live UI at run time."""

    model_config = ConfigDict(extra="forbid")

    action: str
    target: str = ""
    expected_state: str = ""
    direction: Literal["up", "down", "left", "right"] | None = None
    until: str = ""
    value: str = ""
    timeout_s: float | None = None
    optional: bool = False

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        action = (value or "").strip().lower()
        if action not in STEP_ACTIONS:
            raise ValueError(
                f"action không hợp lệ: {value!r}. Dùng một trong: {sorted(STEP_ACTIONS)}"
            )
        return action


class Workflow(BaseModel):
    """A named, app-scoped way to perform one function."""

    model_config = ConfigDict(extra="forbid")

    id: str
    app_id: str
    version_id: str | None = None
    name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[WorkflowStep] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    status: Literal["draft", "verified", "deprecated"] = "draft"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision_no: int = 0
    created_at: str = ""
    updated_at: str = ""

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        text = (value or "").strip()
        if not _ID_PATTERN.match(text):
            raise ValueError(
                f"id workflow không hợp lệ: {value!r}. Chỉ dùng chữ thường, số và . _ - "
                "(ví dụ: auth.logout)"
            )
        return text

    @field_validator("app_id")
    @classmethod
    def _valid_app_id(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("app_id không được rỗng")
        return text

    @field_validator("aliases", "tags", "preconditions", "success_criteria")
    @classmethod
    def _clean_strings(cls, value: list[str]) -> list[str]:
        """Trim, drop blanks and de-duplicate while keeping the author's order."""
        seen: set[str] = set()
        out: list[str] = []
        for item in value or []:
            text = str(item).strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
        return out

    def embedding_text(self) -> str:
        """The text this workflow is embedded from.

        Aliases carry the synonym signal ("thoát tài khoản" for "đăng xuất") and
        step targets carry the wording a tester would use, so both belong here.
        """
        parts = [self.name]
        if self.description:
            parts.append(self.description)
        if self.aliases:
            parts.append("cách gọi khác: " + ", ".join(self.aliases))
        if self.tags:
            parts.append("tags: " + ", ".join(self.tags))
        targets = [step.target for step in self.steps if step.target]
        if targets:
            parts.append("thao tác: " + "; ".join(targets))
        return ". ".join(part for part in parts if part).strip()

    def search_text(self) -> str:
        """The text keyword/FTS search runs over (no step payload noise)."""
        parts = [self.id, self.name, self.description]
        parts.extend(self.aliases)
        parts.extend(self.tags)
        parts.extend(step.target for step in self.steps if step.target)
        parts.extend(step.expected_state for step in self.steps if step.expected_state)
        return " ".join(part for part in parts if part).strip()


class WorkflowHit(BaseModel):
    """One retrieved workflow, with the evidence for why it ranked where it did."""

    model_config = ConfigDict(extra="forbid")

    workflow: Workflow
    score: float
    channels: dict[str, int] = Field(default_factory=dict)
    """Channel name -> 1-based rank in that channel (absent = no hit there)."""

    trusted: bool = True
    """Whether this hit is evidence the workflow really is the one asked about.

    False means it is a guess: it may be shown to the agent, but only under a
    label that says so.  ``channels`` explains *how* it was found; ``trusted``
    records whether that is enough.
    """

    @property
    def matched_channels(self) -> list[str]:
        return sorted(self.channels)

    @property
    def semantic_only(self) -> bool:
        """True when nothing but the vector channel found this workflow."""
        return bool(self.channels) and not (set(self.channels) & LEXICAL_CHANNELS)


class RetrievalResult(BaseModel):
    """Outcome of a knowledge lookup.

    ``found=False`` is a first-class answer, not an error: it means "there is no
    guidance for this function", which the caller must surface to the agent
    instead of letting it invent a workflow.
    """

    model_config = ConfigDict(extra="forbid")

    found: bool
    query: str
    app_id: str
    version_id: str | None = None
    hits: list[WorkflowHit] = Field(default_factory=list)
    reason: str = ""
    timings_ms: dict[str, float] = Field(default_factory=dict)
    degraded: bool = False
    """True when the knowledge base could not be consulted at all."""
    filters: dict[str, Any] = Field(default_factory=dict)
    """Bộ lọc metadata đã dùng cho lần truy hồi này (rỗng = truy hồi theo câu hỏi).

    Đi cùng kết quả để nơi gọi (log, UI "AI đã biết gì", tool trả lời agent) trả lời được câu
    "đã hỏi gì" — thiếu nó thì không phân biệt được "KB không có" với "lọc sai".
    """
