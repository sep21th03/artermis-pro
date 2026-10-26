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

"""Text normalisation shared by the knowledge stores and retrieval.

Vietnamese is the reason this exists.  PostgreSQL ships no Vietnamese stemmer,
so ``to_tsvector('simple', …)`` on raw text treats "đăng" and "dang" as unrelated
tokens -- and a tester types either one.  Both the indexed column and the query
therefore pass through :func:`normalize`, which lower-cases, strips diacritics
and collapses whitespace, so the two spellings meet.
"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s.]", re.UNICODE)


def nfc(text: str) -> str:
    """Unicode NFC — the normal form every piece of system-generated text uses.

    Vietnamese renders the same word in two byte-different ways (composed vs
    combining marks).  Mixing them inside one prompt makes every exact comparison
    unreliable, so all generated text goes through here.
    """
    return unicodedata.normalize("NFC", text or "")


def strip_diacritics(text: str) -> str:
    """Drop combining marks, and the stroke on đ/Đ which is not a combining mark."""
    decomposed = unicodedata.normalize("NFD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return without_marks.replace("đ", "d").replace("Đ", "D")


def normalize(text: str) -> str:
    """Normalise for matching: casefold, strip diacritics, collapse whitespace."""
    if not text:
        return ""
    folded = strip_diacritics(str(text).casefold())
    return _WHITESPACE.sub(" ", folded).strip()


def tokenize(text: str) -> list[str]:
    """Normalised word tokens, punctuation dropped, order preserved.

    Single characters are kept: "đ" and "x" are real button labels in this domain
    and dropping them would silently break a label match.
    """
    cleaned = _PUNCT.sub(" ", normalize(text))
    return [token for token in cleaned.split() if token]


def strip_secrets(text: str) -> str:
    """Redact credential-shaped substrings before anything is logged.

    The knowledge base is written by testers and read by an LLM; a bearer token
    pasted into a workflow note must not end up in ``kb_retrieval_log``.
    """
    redacted = re.sub(r"\b(Bearer)\s+[A-Za-z0-9._\-]{8,}", r"\1 <redacted>", text)
    redacted = re.sub(r"\b(sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_\-]{8,}", "<redacted>", redacted)
    redacted = re.sub(r"\b[A-Za-z0-9_\-]{32,}\b", "<redacted>", redacted)
    redacted = re.sub(
        r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        redacted,
    )
    return redacted


#: Words that carry no retrieval signal in this domain.  Without this list the
#: keyword channel over-matches: "chụp ảnh màn hình" shares the tokens "man" and
#: "hinh" with the logout workflow (whose step says "Màn hình cá nhân"), and an
#: OR query would hand the agent the logout procedure for a screenshot request.
#:
#: **These are matched against the raw, still-accented word.**  Stripping
#: diacritics first would create homographs and drop real signal: "đăng" (as in
#: đăng xuất) and "đang" (the progressive marker) both fold to "dang", so a
#: folded stopword list would remove "đăng" and reduce "đăng xuất" to a single
#: token.  Words that are naturally unaccented live in :data:`STOPWORDS_ASCII`.
STOPWORDS = frozenset(
    {
        # Hư từ tiếng Việt
        "đang",
        "dùng",
        "khỏi",
        "của",
        "và",
        "với",
        "cho",
        "tôi",
        "mình",
        "giúp",
        "hãy",
        "kiểm",
        "tra",
        "chức",
        "năng",
        "một",
        "mới",
        "các",
        "những",
        "khi",
        "sau",
        "trước",
        "trong",
        "ngoài",
        "lên",
        "xuống",
        "vào",
        "ra",
        "đi",
        "đến",
        "bằng",
        "như",
        "rồi",
        "được",
        "này",
        "đó",
        "thì",
        "là",
        "có",
        "cái",
        "con",
        "xem",
        "thử",
        "giùm",
        "nhé",
        "ạ",
    }
)

#: Unaccented filler, safe to match on the folded form.
STOPWORDS_ASCII = frozenset(
    {
        "test",
        "testing",
        "check",
        "verify",
        "please",
        "then",
        "flow",
        "feature",
        "the",
        "a",
        "an",
        "to",
        "of",
        "for",
        "and",
        "or",
    }
)


def content_tokens(text: str) -> list[str]:
    """Query tokens with domain stopwords removed, in normalised form.

    Falls back to the raw token list when every token is a stopword: a query made
    only of filler still deserves a lookup rather than a guaranteed miss.
    """
    cleaned = _PUNCT.sub(" ", str(text or ""))
    kept: list[str] = []
    for word in cleaned.split():
        folded = word.casefold()
        if folded in STOPWORDS or folded in STOPWORDS_ASCII:
            continue
        normalised = normalize(word)
        if normalised:
            kept.append(normalised)
    return kept or tokenize(text)


def coverage(query_tokens: list[str], document: str) -> float:
    """Share of ``query_tokens`` present as whole words in ``document``.

    This is what turns the keyword channel from "any token matched" into a
    defensible signal.  ``document`` must already be normalised.
    """
    if not query_tokens:
        return 0.0
    found = 0
    for token in query_tokens:
        if re.search(rf"(?<![0-9a-z]){re.escape(token)}(?![0-9a-z])", document):
            found += 1
    return found / len(query_tokens)


#: Định danh của tester phải sống sót nguyên vẹn qua mọi bước sinh văn bản: số điện
#: thoại, email, mã thiết bị, URL.  Một LLM "chuẩn hoá" chúng là hành vi phá hoại
#: im lặng — test sẽ chạy trên một tài khoản khác.
_IDENTITY_PATTERNS = (
    re.compile(r"(?<!\w)(?:\+?\d[\d\s.\-]{7,}\d)(?!\w)"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    re.compile(r"\b[A-Z0-9]{2,}(?:[-_][A-Z0-9]+)+\b"),
    re.compile(r"https?://\S+"),
)


def identity_tokens(text: str) -> list[str]:
    """Định danh trong ``text`` — thứ không được phép đổi ở bất kỳ bước nào."""
    out: list[str] = []
    for pattern in _IDENTITY_PATTERNS:
        for match in pattern.findall(text or ""):
            token = match.strip()
            if token and token not in out:
                out.append(token)
    return out
