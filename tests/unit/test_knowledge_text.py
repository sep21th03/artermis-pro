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

"""Normalisation and workflow validation for the knowledge base.

The normalisation rules exist because of measured behaviour, not taste: without
diacritic folding a tester typing "dang xuat" finds nothing, and without the
stopword list the keyword channel matches "chụp ảnh màn hình" against the logout
workflow (they share "màn hình").
"""

import pytest
from pydantic import ValidationError

from artemis.knowledge.models import Workflow, WorkflowStep
from artemis.knowledge.text import (
    content_tokens,
    coverage,
    normalize,
    strip_diacritics,
    strip_secrets,
    tokenize,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Đăng xuất", "dang xuat"),
        ("Thoát tài khoản", "thoat tai khoan"),
        ("  Màn   Hình  ", "man hinh"),
        ("", ""),
    ],
)
def test_normalize_folds_case_and_diacritics(raw, expected):
    assert normalize(raw) == expected


def test_strip_diacritics_handles_d_stroke():
    """đ is not a combining mark, so NFD alone leaves it behind."""
    assert strip_diacritics("Đường") == "Duong"


def test_tokenize_keeps_single_character_labels():
    """'đ' and 'x' are real button labels here; dropping them breaks matching."""
    assert tokenize("Nhấn nút X") == ["nhan", "nut", "x"]


def test_content_tokens_drop_stopwords():
    assert content_tokens("Test đăng xuất giúp tôi") == ["dang", "xuat"]


def test_content_tokens_keep_the_query_when_everything_is_a_stopword():
    """A request made only of filler still deserves a lookup."""
    assert content_tokens("kiểm tra giúp tôi") == ["kiem", "tra", "giup", "toi"]


def test_stopwords_are_matched_before_diacritics_are_stripped():
    """Regression: "đăng" and "đang" both fold to "dang".

    Matching stopwords on the folded form dropped "đăng" and reduced
    "đăng xuất" to a single token, which quietly weakened every logout query.
    """
    assert content_tokens("đăng xuất") == ["dang", "xuat"]
    assert content_tokens("đăng nhập đang chờ") == ["dang", "nhap", "cho"]
    assert content_tokens("thoát khỏi tài khoản đang dùng") == ["thoat", "tai", "khoan"]
    # Khi MỌI token đều là stopword thì hàm cố tình trả về token thô (xem docstring).
    assert content_tokens("đang dùng") == ["dang", "dung"]


def test_coverage_matches_whole_words_only():
    assert coverage(["man"], "man hinh ca nhan") == 1.0
    # "man" must not match inside "manh".
    assert coverage(["man"], "manh me") == 0.0


def test_coverage_of_the_screenshot_false_positive_is_below_the_bar():
    """The measured case: 2 of 4 content words shared with the logout workflow."""
    document = "auth.logout dang xuat tai khoan man hinh ca nhan nut dang xuat"
    assert coverage(["chup", "anh", "man", "hinh"], document) == 0.5


def test_coverage_of_a_real_request_is_full():
    document = "auth.logout dang xuat tai khoan bottomtab nut dang xuat"
    assert coverage(["dang", "xuat"], document) == 1.0


@pytest.mark.parametrize(
    "secret",
    [
        "Bearer abcdefghijklmnop",
        "token=supersecretvalue",
        "api_key: sk-abcdefghijklmnop",
        "ghp_abcdefghijklmnopqrst",
    ],
)
def test_strip_secrets_redacts_credentials(secret):
    assert "<redacted>" in strip_secrets(f"ghi chú: {secret}")


def test_strip_secrets_leaves_ordinary_text_alone():
    assert strip_secrets("Nhấn nút Đăng xuất ở cuối màn hình") == (
        "Nhấn nút Đăng xuất ở cuối màn hình"
    )
