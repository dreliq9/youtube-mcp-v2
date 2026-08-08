"""Replaceable dense embedding backends for corpus retrieval.

The MCP contract never names FastEmbed or a concrete model. This module keeps
that implementation detail behind a tiny protocol so the retrieval evidence
contract can survive future backend/model changes.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Protocol, Sequence

from .paths import MODEL_DIR

DEFAULT_MODEL_ID = "BAAI/bge-small-en-v1.5"
FASTEMBED_CACHE_DIR = MODEL_DIR / "fastembed"
SemanticMode = Literal["off", "auto", "required"]


class EmbeddingError(RuntimeError):
    """Base error for semantic embedding setup/inference."""


class EmbeddingUnavailable(EmbeddingError):
    """Semantic backend/model is not locally available for the requested mode."""


class EmbeddingBackend(Protocol):
    """Backend-neutral dense retrieval contract."""

    model_id: str

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        ...

    def embed_query(self, text: str) -> list[float]:
        ...


def configured_model_id() -> str:
    return os.environ.get("YOUTUBE_MCP_EMBED_MODEL", DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def normalize_vector(vector: Iterable[float]) -> list[float]:
    values = [float(value) for value in vector]
    if not values:
        raise EmbeddingError("embedding backend returned an empty vector")
    if any(not math.isfinite(value) for value in values):
        raise EmbeddingError("embedding backend returned a non-finite vector")
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0.0:
        raise EmbeddingError("embedding backend returned a zero-norm vector")
    return [value / norm for value in values]


def validate_vector_batch(vectors: Sequence[Sequence[float]]) -> list[list[float]]:
    normalized = [normalize_vector(vector) for vector in vectors]
    if not normalized:
        return []
    dim = len(normalized[0])
    if any(len(vector) != dim for vector in normalized):
        raise EmbeddingError("embedding backend returned inconsistent vector dimensions")
    return normalized


@dataclass
class FastEmbedBackend:
    """FastEmbed dense-text adapter loaded lazily.

    `local_files_only=True` is used for auto mode so merely enabling the semantic
    package cannot trigger a surprise model download. Required mode can opt into
    downloading the configured model once.
    """

    model_id: str = DEFAULT_MODEL_ID
    local_files_only: bool = True
    cache_dir: Path = FASTEMBED_CACHE_DIR

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "semantic retrieval requires the optional 'semantic' extra"
            ) from exc

        try:
            self._model = TextEmbedding(
                model_name=self.model_id,
                cache_dir=str(self.cache_dir),
                local_files_only=self.local_files_only,
            )
        except Exception as exc:
            mode = "local cache" if self.local_files_only else "configured model"
            raise EmbeddingUnavailable(
                f"FastEmbed could not initialize {self.model_id!r} from {mode}"
            ) from exc

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            vectors = list(self._model.passage_embed(list(texts)))
        except Exception as exc:
            raise EmbeddingError("FastEmbed passage embedding failed") from exc
        return validate_vector_batch(vectors)

    def embed_query(self, text: str) -> list[float]:
        if not text.strip():
            raise EmbeddingError("semantic query must not be empty")
        try:
            vectors = list(self._model.query_embed(text))
        except Exception as exc:
            raise EmbeddingError("FastEmbed query embedding failed") from exc
        if len(vectors) != 1:
            raise EmbeddingError(
                f"FastEmbed query embedding returned {len(vectors)} vectors, expected 1"
            )
        return normalize_vector(vectors[0])


def resolve_backend(
    mode: SemanticMode,
    *,
    model_id: str | None = None,
) -> EmbeddingBackend | None:
    """Resolve the configured semantic backend without leaking implementation details.

    off      -> lexical only
    auto     -> use FastEmbed only if package + configured model are already local
    required -> require FastEmbed and permit model download unless globally forced local
    """
    if mode not in {"off", "auto", "required"}:
        raise EmbeddingError(f"invalid semantic mode: {mode!r}")
    if mode == "off":
        return None

    resolved_model = (model_id or configured_model_id()).strip()
    if not resolved_model:
        raise EmbeddingError("configured embedding model id must not be empty")

    force_local = _truthy_env("YOUTUBE_MCP_EMBED_LOCAL_ONLY")
    local_only = True if mode == "auto" else force_local
    try:
        return FastEmbedBackend(
            model_id=resolved_model,
            local_files_only=local_only,
            cache_dir=FASTEMBED_CACHE_DIR,
        )
    except EmbeddingUnavailable:
        if mode == "auto":
            return None
        raise
