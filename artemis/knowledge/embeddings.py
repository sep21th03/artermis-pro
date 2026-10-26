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

"""Embedding providers for the knowledge base.

Documents and queries are embedded through the same model.  ``task_type`` is
passed for the models that honour it (``gemini-embedding-001`` and friends) and
is harmless where they do not -- measured against ``gemini-embedding-2``, the
query and document vectors of the same text are identical (cosine 1.0000), so
this model ignores the hint entirely.  The parameter is kept because switching
model is a configuration change and the asymmetry is real for other models.

Failure policy: an embedding failure is never fatal.  It disables the vector
channel for that call and the exact/keyword channels still answer.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """The embedding provider could not be used."""


class Embedder(Protocol):
    """What retrieval needs from an embedding backend."""

    model: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _secret(value: object) -> str | None:
    """Accept either a SecretStr or a plain string, without logging either."""
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    text = getter() if callable(getter) else str(value)
    return text.strip() or None


class GeminiEmbedder:
    """Google Gemini embeddings through the ``google-genai`` SDK."""

    #: Batches are capped because one oversized request fails the whole call,
    #: and a partially embedded catalogue is recoverable while a failed one is not.
    def __init__(
        self,
        model: str = "gemini-embedding-2",
        api_key: str | None = None,
        output_dim: int | None = None,
        batch_size: int = 32,
        client: object | None = None,
    ) -> None:
        self.model = model
        self.batch_size = max(1, batch_size)
        self.output_dim = output_dim
        self._client = client
        self._api_key = api_key
        self._task_type_supported = True

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise EmbeddingError("Thiếu google-genai để tạo embedding") from exc
        key = self._api_key
        if key is None:
            try:
                from artemis.config.settings import settings

                key = _secret(getattr(settings, "GOOGLE_API_KEY", None)) or _secret(
                    getattr(settings, "GEMINI_API_KEY", None)
                )
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingError(f"Không đọc được API key cho embedding: {exc}") from exc
        if not key:
            raise EmbeddingError(
                "Chưa có GOOGLE_API_KEY/GEMINI_API_KEY nên không tạo được embedding. "
                "Đặt key trong .env rồi chạy lại (kênh exact + keyword vẫn hoạt động)."
            )
        self._client = genai.Client(api_key=key)
        return self._client

    def _call(self, texts: list[str], task_type: str) -> list[list[float]]:
        from google.genai import types

        client = self._ensure_client()
        config = None
        if self._task_type_supported:
            try:
                config = types.EmbedContentConfig(
                    task_type=task_type,
                    output_dimensionality=self.output_dim,
                )
            except Exception:  # noqa: BLE001 - older SDK without the field
                config = None
        # A list of plain strings is ONE content with several parts, which the API
        # answers with a single embedding -- measured: 2 texts -> 1 vector.  The
        # batch form needs one Content per text.
        contents = [types.Content(parts=[types.Part(text=text)]) for text in texts]
        try:
            result = client.models.embed_content(model=self.model, contents=contents, config=config)
        except Exception as exc:  # noqa: BLE001
            if config is not None:
                # Some models reject task_type / output_dimensionality.  Losing the
                # hint costs a little recall; losing the whole channel costs more.
                logger.warning(
                    "Embedding model %s từ chối config (%s) — thử lại không config",
                    self.model,
                    type(exc).__name__,
                )
                self._task_type_supported = False
                return self._call(texts, task_type)
            raise EmbeddingError(f"Gọi embedding thất bại: {exc}") from exc

        embeddings = getattr(result, "embeddings", None) or []
        vectors = [list(getattr(item, "values", []) or []) for item in embeddings]
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"Embedding trả về {len(vectors)} vector cho {len(texts)} đoạn text"
            )
        return vectors

    def _batched(self, texts: list[str], task_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            chunk = texts[start : start + self.batch_size]
            out.extend(self._call(chunk, task_type))
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._batched(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        vectors = self._call([text], "RETRIEVAL_QUERY")
        return vectors[0] if vectors else []
