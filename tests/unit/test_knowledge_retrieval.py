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

"""Rank fusion, the "found" decision, and graceful degradation.

No database and no network: the fusion and the decision rule are pure functions
precisely so they can be tested like this.  The behaviour under test is the part
that decides whether the agent is handed guidance, so it is the part that must
not regress silently.
"""

import pytest

from artemis.knowledge.models import RetrievalResult, Workflow, WorkflowHit, WorkflowStep
from artemis.knowledge.retrieval import (
    FusedHit,
    decide_found,
    reciprocal_rank_fusion,
    split_hits,
)


def _fused(workflow_id: str, **ranks: int) -> FusedHit:
    return FusedHit(workflow_id=workflow_id, score=0.0, ranks=dict(ranks))


# ── fusion ───────────────────────────────────────────────────────────────
def test_fusion_orders_by_fused_score():
    hits = reciprocal_rank_fusion({"exact": ["a", "b"], "keyword": ["b", "c"]})
    assert hits[0].workflow_id == "b"


def test_agreement_between_channels_beats_a_single_first_place():
    """b is 2nd in both channels; a is 1st in one and absent from the other."""
    hits = reciprocal_rank_fusion({"exact": ["a", "b"], "keyword": ["c", "b"]})
    assert hits[0].workflow_id == "b"
    assert hits[0].ranks == {"exact": 2, "keyword": 2}


def test_fusion_records_every_channel_rank():
    hits = reciprocal_rank_fusion({"exact": ["a"], "keyword": ["a"], "vector": ["a"]})
    assert hits[0].ranks == {"exact": 1, "keyword": 1, "vector": 1}


def test_fusion_ignores_a_duplicate_id_within_one_channel():
    hits = reciprocal_rank_fusion({"exact": ["a", "a", "b"]})
    by_id = {hit.workflow_id: hit for hit in hits}
    assert by_id["a"].score == pytest.approx(1 / 61)
    assert by_id["b"].ranks == {"exact": 2}


def test_fusion_of_nothing_is_empty():
    assert reciprocal_rank_fusion({}) == []
    assert reciprocal_rank_fusion({"exact": []}) == []


def test_fusion_is_deterministic_on_a_tie():
    hits = reciprocal_rank_fusion({"exact": ["b"], "keyword": ["a"]})
    assert [hit.workflow_id for hit in hits] == ["a", "b"]


def test_k_controls_how_much_first_place_matters():
    flat = reciprocal_rank_fusion({"exact": ["a", "b"]}, k=1000)
    steep = reciprocal_rank_fusion({"exact": ["a", "b"]}, k=1)
    assert flat[0].score - flat[1].score < steep[0].score - steep[1].score


# ── the "found" decision ─────────────────────────────────────────────────
def _hit(workflow_id: str, *, trusted: bool, **channels) -> WorkflowHit:
    return WorkflowHit(
        workflow=Workflow(id=workflow_id, app_id="x", name=workflow_id),
        score=0.0,
        channels=dict(channels),
        trusted=trusted,
    )


def test_exact_evidence_is_enough():
    assert decide_found([_hit("a", trusted=True, exact=1)])[0] is True


def test_vector_only_is_not_evidence():
    """The nearest-neighbour query always returns rows; ranking is not proof."""
    found, reason = decide_found([_hit("a", trusted=False, vector=1)])
    assert found is False
    assert "chưa đủ tin cậy" in reason


def test_no_hits_at_all_reports_no_knowledge():
    found, reason = decide_found([])
    assert found is False
    assert "Không tìm thấy workflow" in reason


def test_a_trusted_hit_anywhere_in_the_list_is_enough():
    hits = [_hit("a", trusted=False, vector=1), _hit("b", trusted=True, keyword=1)]
    assert decide_found(hits)[0] is True


def test_split_hits_separates_trusted_from_guesses():
    trusted = WorkflowHit(workflow=Workflow(id="a", app_id="x", name="A"), score=1.0, trusted=True)
    guess = WorkflowHit(workflow=Workflow(id="b", app_id="x", name="B"), score=1.0, trusted=False)
    assert split_hits([trusted, guess]) == ([trusted], [guess])


def test_semantic_only_reflects_the_channels_not_the_decision():
    hit = WorkflowHit(
        workflow=Workflow(id="a", app_id="x", name="A"), score=1.0, channels={"vector": 1}
    )
    assert hit.semantic_only is True
    hit = WorkflowHit(
        workflow=Workflow(id="a", app_id="x", name="A"), score=1.0, channels={"keyword": 3}
    )
    assert hit.semantic_only is False


def test_degraded_result_carries_a_reason():
    result = RetrievalResult(found=False, query="q", app_id="a", degraded=True, reason="down")
    assert result.degraded and result.reason == "down" and result.hits == []


# ── workflow validation ──────────────────────────────────────────────────
def test_workflow_rejects_an_unknown_step_action():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WorkflowStep(action="teleport", target="x")


def test_workflow_step_rejects_an_unknown_key():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WorkflowStep(action="tap", targte="typo")


def test_workflow_rejects_a_malformed_id():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Workflow(id="Auth Logout", app_id="x", name="n")


def test_workflow_dedupes_aliases_case_insensitively():
    workflow = Workflow(id="a", app_id="x", name="n", aliases=["Đăng xuất", " đăng xuất ", "logout"])
    assert workflow.aliases == ["Đăng xuất", "logout"]


def test_embedding_text_carries_aliases_and_step_targets():
    workflow = Workflow(
        id="auth.logout",
        app_id="x",
        name="Đăng xuất tài khoản",
        aliases=["logout"],
        steps=[WorkflowStep(action="tap", target="Tab cuối cùng")],
    )
    text = workflow.embedding_text()
    assert "Đăng xuất tài khoản" in text and "logout" in text and "Tab cuối cùng" in text
