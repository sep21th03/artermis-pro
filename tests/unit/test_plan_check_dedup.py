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

"""PHASE C (dedup dòng verify/assert) + PHASE D (phạm vi số đo, retry telemetry).

PHASE C — đo trên máy thật 30/10/2026 (session ``4806881d``): plan có **hai** dòng
``- verify@end: Current logged-in account is 0972842548 and current house is Dled`` giống hệt nhau ⇒
cùng một tiêu chí bị phán hai lần.

PHASE D — đo trên cùng phiên: step-metrics đếm **148** LLM response / 3,21 M token, còn perf có
**103** span ``llm_call`` / 1,93 M token. Hai kênh đếm **phạm vi khác nhau** (không phải double-count
trong cùng một kênh): step-metrics đếm mọi response mà **lượt Operator** tiêu thụ; perf đếm mỗi lượt
gọi provider **đã hoàn tất của mọi vai trò**, và các lần thử lại là event ``llm_retry`` riêng. Phạm
vi nay được ghi thẳng trong payload, và ``retry_count``/``retry_wait_ms`` được đếm tại **đúng lớp
provider retry** thay vì suy từ wall time.
"""

from __future__ import annotations

from artemis.utils.notes import collapse_duplicate_plan_blocks
from artemis.utils.plan_grammar import collapse_duplicate_check_lines, parse_plan
from artemis.utils.step_metrics import (
    LLM_COUNT_SCOPE,
    TOKEN_COUNT_SCOPE,
    StepMetrics,
)

DUPLICATED = (
    "- [x] Verify current logged-in account on the Menu screen\n"
    "- [/] Switch current house to `Dled`\n"
    "  - [x] Tap the Dled card\n"
    "- verify@end: Current account is 0972842548 and current house is Dled\n"
    "- verify@end: Current account is 0972842548 and current house is Dled\n"
)


def test_identical_check_lines_are_collapsed_keeping_the_first():
    text, removed = collapse_duplicate_check_lines(DUPLICATED)

    assert removed == 1
    assert text.count("verify@end: Current account is 0972842548") == 1
    # Thứ tự giữ nguyên: dòng còn lại nằm đúng vị trí xuất hiện đầu tiên.
    assert text.index("verify@end") > text.index("Switch current house")


def test_different_criteria_and_scopes_are_kept():
    plan = (
        "- [x] Milestone A\n"
        "  - verify: Trang chủ hiển thị Dled\n"
        "  - verify: Trang chủ hiển thị Dled\n"  # trùng trong CÙNG milestone ⇒ bỏ
        "- [x] Milestone B\n"
        "  - verify: Trang chủ hiển thị Dled\n"  # cùng văn bản nhưng KHÁC milestone ⇒ giữ
        "  - assert: không còn thanh điều hướng\n"
        "  - assert@end: không còn thanh điều hướng\n"  # khác mốc phán ⇒ giữ
    )

    text, removed = collapse_duplicate_check_lines(plan)

    assert removed == 1
    assert text.count("verify: Trang chủ hiển thị Dled") == 2
    assert text.count("assert") == 2


def test_other_lines_are_untouched():
    text, removed = collapse_duplicate_check_lines(DUPLICATED)

    assert removed == 1
    for line in DUPLICATED.splitlines():
        if line.startswith("- verify@end:"):
            continue
        assert line in text


def test_a_plan_without_check_lines_is_unchanged():
    plan = "- [x] A\n  - [x] B\n"

    assert collapse_duplicate_check_lines(plan) == (plan, 0)
    assert collapse_duplicate_check_lines("") == ("", 0)


def test_check_items_after_dedup_are_unique():
    text, _ = collapse_duplicate_check_lines(DUPLICATED)

    snapshot = parse_plan(text)
    keys = [(ci.kind, ci.when, ci.parent_key, ci.text) for ci in snapshot.all_check_items]

    assert len(keys) == len(set(keys)) == 1


def test_milestone_dedup_still_works_alongside():
    text, removed = collapse_duplicate_plan_blocks(DUPLICATED)
    text, removed_checks = collapse_duplicate_check_lines(text)

    assert removed == 0  # không có milestone trùng trong ví dụ này
    assert removed_checks == 1
    assert text.count("verify@end") == 1


# ── PHASE D: phạm vi số đo + retry telemetry ─────────────────────────────


def test_metrics_payload_states_the_scope_of_llm_numbers():
    payload = StepMetrics(llm_calls=3, input_tokens=100).as_detail()

    assert payload["llm_calls_scope"] == LLM_COUNT_SCOPE == "operator_turn_responses"
    assert payload["tokens_scope"] == TOKEN_COUNT_SCOPE


def test_retry_telemetry_counts_attempts_and_backoff_at_the_provider_layer(monkeypatch):
    """``retry_count``/``retry_wait_ms`` phải được **đếm** ở lớp provider retry, không suy từ wall time."""
    import logging

    from artemis.services import llm as llm_module

    captured: list[dict] = []

    def fake_record_retry(error, delay, **kwargs):
        captured.append({"error": error, "delay": delay, **kwargs})

    monkeypatch.setattr(llm_module, "_record_llm_retry", fake_record_retry)
    handler = llm_module._ProviderRetryTelemetryHandler()
    request = {"provider": llm_module.ModelProvider.GOOGLE.value, "request_id": "r1"}
    token = llm_module._ACTIVE_LLM_REQUEST.set(request)
    try:
        record = logging.LogRecord(
            name="google_genai._api_client",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=(
                "Retrying request to /v1beta/models/gemini: 429 Too Many Requests."
                " Retrying in 1.5 seconds as it raised ClientError"
            ),
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        handler.emit(record)
    finally:
        llm_module._ACTIVE_LLM_REQUEST.reset(token)

    assert len(captured) == 2
    assert captured[0]["attempt"] == 1 and captured[1]["attempt"] == 2
    assert captured[0]["delay"] == 1.5
    assert request["retry_count"] == 2  # đếm trên ngữ cảnh lượt gọi, không suy đoán
