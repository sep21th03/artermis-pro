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

"""Rank fusion for the knowledge base.

Pure functions over ranked id lists: no database, no network, no model.  That is
deliberate -- relevance policy is the part that needs to be reasoned about and
tested, so it must not be entangled with SQL.

Three channels feed in:

* ``exact``   -- the query *is* a workflow id or one of its aliases.
* ``keyword`` -- full-text search over the normalised projection.
* ``vector``  -- cosine nearest neighbours.

They are combined with Reciprocal Rank Fusion rather than a weighted sum of
scores, because the three channels produce values on incomparable scales
(``ts_rank`` is unbounded, cosine lives in [-1, 1]) and RRF only needs ranks.

**Why a fixed similarity threshold is not the gate.**  Measured against
``gemini-embedding-2`` on real screens, unrelated requests still score 0.55-0.65
against a workflow while genuine paraphrases score 0.59-0.73 -- the distributions
overlap, so no cutoff separates them.  ``found`` is therefore decided by the
*lexical* channels, and a vector-only match is returned flagged as
``semantic_only`` so the context builder can label it as a guess instead of
presenting it as an instruction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from artemis.knowledge.models import LEXICAL_CHANNELS, WorkflowHit

#: The value from the original RRF paper (Cormack et al., 2009).  Large enough
#: that rank 1 and rank 2 in a channel are close, which is what makes the fusion
#: robust to one channel being noisy.
DEFAULT_RRF_K = 60


@dataclass
class FusedHit:
    """A workflow id with its fused score and per-channel ranks."""

    workflow_id: str
    score: float
    ranks: dict[str, int] = field(default_factory=dict)
    similarities: dict[str, float] = field(default_factory=dict)

    @property
    def semantic_only(self) -> bool:
        """True when nothing but the vector channel found this workflow."""
        return bool(self.ranks) and not (set(self.ranks) & LEXICAL_CHANNELS)


def reciprocal_rank_fusion(
    channels: dict[str, list[str]],
    k: int = DEFAULT_RRF_K,
    similarities: dict[str, dict[str, float]] | None = None,
) -> list[FusedHit]:
    """Merge ranked id lists into one ranking.

    ``channels`` maps a channel name to workflow ids best-first.  A workflow that
    appears in several channels accumulates ``1/(k + rank)`` from each, so
    agreement between channels outranks a single strong channel -- which is the
    behaviour that makes hybrid search worth the extra query.
    """
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for channel, ids in channels.items():
        # Rank by position among *distinct* ids, so a channel that repeats an id
        # (a bug in a backend, a UNION without DISTINCT) cannot push every later
        # result down a place.
        position = 0
        seen_in_channel: set[str] = set()
        for workflow_id in ids:
            if workflow_id in seen_in_channel:
                continue
            seen_in_channel.add(workflow_id)
            position += 1
            ranks.setdefault(workflow_id, {})[channel] = position
            scores[workflow_id] = scores.get(workflow_id, 0.0) + 1.0 / (k + position)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [
        FusedHit(
            workflow_id=workflow_id,
            score=score,
            ranks=ranks[workflow_id],
            similarities=(similarities or {}).get(workflow_id, {}),
        )
        for workflow_id, score in ordered
    ]


def split_hits(hits: list[WorkflowHit]) -> tuple[list[WorkflowHit], list[WorkflowHit]]:
    """Split into (trusted, guessed) preserving each group's order."""
    trusted = [hit for hit in hits if hit.trusted]
    guessed = [hit for hit in hits if not hit.trusted]
    return trusted, guessed


def decide_found(hits: list[WorkflowHit]) -> tuple[bool, str]:
    """Whether the knowledge base has guidance worth giving the agent.

    Decided from the hits that can actually be served, not from the fused
    ranking: a ranked id whose workflow has since been deleted would otherwise
    report "found" while handing over nothing.

    A vector-only hit is deliberately not enough on its own -- the
    nearest-neighbour query always returns rows, so treating it as a hit would
    tell the agent "here is the logout procedure" for any request at all.
    """
    if any(hit.trusted for hit in hits):
        return True, ""
    if hits:
        return False, (
            "Không có workflow nào khớp id/từ khoá đủ chắc; chỉ có vài mục gần nhau về "
            "ngữ nghĩa hoặc khớp một phần (chưa đủ tin cậy để coi là hướng dẫn)."
        )
    return False, "Không tìm thấy workflow nào cho chức năng này trong knowledge base."
