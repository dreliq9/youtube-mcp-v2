"""Replaceable text↔image embedding backend for visual evidence retrieval.

The public MCP surface exposes only `auto|required` policy. FastEmbed's paired
CLIP text/vision encoders are an implementation detail and can later be replaced
without changing visual evidence identity or search result shape.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from .embeddings import EmbeddingError, EmbeddingUnavailable, normalize_vector
from .paths import MODEL_DIR

DEFAULT_TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"
DEFAULT_IMAGE_MODEL = "Qdrant/clip-ViT-B-32-vision"
FASTEMBED_VISUAL_CACHE = MODEL_DIR / "fastembed"


class VisualEmbeddingBackend(Protocol):
    text_model_id: str
    image_model_id: str

    def embed_images(self, paths: Sequence[Path]) -> list[list[float]]:
        ...

    def embed_query(self, text: str) -> list[float]:
        ...


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def configured_models() -> tuple[str, str]:
    text = os.environ.get("YOUTUBE_MCP_VISUAL_TEXT_MODEL", DEFAULT_TEXT_MODEL).strip()
    image = os.environ.get("YOUTUBE_MCP_VISUAL_IMAGE_MODEL", DEFAULT_IMAGE_MODEL).strip()
    if not text or not image:
        raise EmbeddingError("visual text/image model IDs must not be empty")
    return text, image


@dataclass
class FastEmbedClipBackend:
    text_model_id: str = DEFAULT_TEXT_MODEL
    image_model_id: str = DEFAULT_IMAGE_MODEL
    local_files_only: bool = True
    cache_dir: Path = FASTEMBED_VISUAL_CACHE

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            from fastembed import ImageEmbedding, TextEmbedding
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "visual retrieval requires the optional 'semantic' extra"
            ) from exc

        try:
            self._text = TextEmbedding(
                model_name=self.text_model_id,
                cache_dir=str(self.cache_dir),
                local_files_only=self.local_files_only,
            )
            self._image = ImageEmbedding(
                model_name=self.image_model_id,
                cache_dir=str(self.cache_dir),
                local_files_only=self.local_files_only,
            )
        except Exception as exc:
            mode = "local cache" if self.local_files_only else "configured models"
            raise EmbeddingUnavailable(
                f"FastEmbed could not initialize paired visual models from {mode}"
            ) from exc

    def embed_images(self, paths: Sequence[Path]) -> list[list[float]]:
        if not paths:
            return []
        try:
            vectors = list(self._image.embed([str(path) for path in paths]))
        except Exception as exc:
            raise EmbeddingError("visual image embedding failed") from exc
        normalized = [normalize_vector(vector) for vector in vectors]
        if len(normalized) != len(paths):
            raise EmbeddingError(
                f"visual backend returned {len(normalized)} vectors for {len(paths)} images"
            )
        if normalized:
            dim = len(normalized[0])
            if any(len(vector) != dim for vector in normalized):
                raise EmbeddingError("visual backend returned inconsistent image dimensions")
        return normalized

    def embed_query(self, text: str) -> list[float]:
        if not text.strip():
            raise EmbeddingError("visual query must not be empty")
        try:
            vectors = list(self._text.embed([text]))
        except Exception as exc:
            raise EmbeddingError("visual text embedding failed") from exc
        if len(vectors) != 1:
            raise EmbeddingError(
                f"visual text backend returned {len(vectors)} vectors, expected 1"
            )
        return normalize_vector(vectors[0])


def resolve_visual_backend(
    mode: str,
    *,
    text_model_id: str | None = None,
    image_model_id: str | None = None,
) -> VisualEmbeddingBackend | None:
    """Resolve a paired local cross-modal backend.

    auto     -> never downloads; returns None when package/models are not local
    required -> may initialize/download unless YOUTUBE_MCP_EMBED_LOCAL_ONLY=1
    """
    if mode not in {"auto", "required"}:
        raise EmbeddingError("visual mode must be 'auto' or 'required'")
    configured_text, configured_image = configured_models()
    text = (text_model_id or configured_text).strip()
    image = (image_model_id or configured_image).strip()
    if not text or not image:
        raise EmbeddingError("visual text/image model IDs must not be empty")

    local_only = True if mode == "auto" else _truthy_env("YOUTUBE_MCP_EMBED_LOCAL_ONLY")
    try:
        return FastEmbedClipBackend(
            text_model_id=text,
            image_model_id=image,
            local_files_only=local_only,
            cache_dir=FASTEMBED_VISUAL_CACHE,
        )
    except EmbeddingUnavailable:
        if mode == "auto":
            return None
        raise
