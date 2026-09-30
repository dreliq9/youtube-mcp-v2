"""Replaceable dense-vector search behind hybrid corpus indexes.

The MCP contract exposes neither the vector store nor the embedding model; a
hybrid index records which implementation built/searches it (`vector_search`
in index metadata), so a future ANN backend can be added as a new named
implementation while existing index revisions keep their exact-scan results.
"""

from __future__ import annotations

import sqlite3
import struct
from typing import Callable, Protocol

DEFAULT_VECTOR_SEARCH = "exact-scan-v1"


class VectorSearchError(RuntimeError):
    pass


class VectorSearch(Protocol):
    name: str

    def similarities(
        self,
        conn: sqlite3.Connection,
        query_vector: list[float],
        *,
        allowed: Callable[[int], bool],
    ) -> dict[int, float]:
        """Cosine similarity for every allowed chunk id."""
        ...


def unpack_vector(blob: bytes | None, dim: int | None) -> list[float]:
    if blob is None or dim is None or dim <= 0:
        raise VectorSearchError("hybrid index chunk is missing its dense embedding")
    expected = int(dim) * 4
    if len(blob) != expected:
        raise VectorSearchError(
            f"embedding blob has {len(blob)} bytes, expected {expected} for dim={dim}"
        )
    return list(struct.unpack(f"<{int(dim)}f", blob))


class ExactScan:
    """Exact dot-product scan over L2-normalized float32 vectors in SQLite.

    Streams one row at a time, so memory stays bounded by the score map rather
    than by the number of stored vectors or transcript text.
    """

    name = DEFAULT_VECTOR_SEARCH

    def similarities(
        self,
        conn: sqlite3.Connection,
        query_vector: list[float],
        *,
        allowed: Callable[[int], bool],
    ) -> dict[int, float]:
        scores: dict[int, float] = {}
        query_dim = len(query_vector)
        cursor = conn.execute("SELECT id, embedding, embedding_dim FROM chunks ORDER BY id")
        for chunk_id, blob, dim in cursor:
            chunk_id = int(chunk_id)
            if not allowed(chunk_id):
                continue
            vector = unpack_vector(blob, dim)
            if len(vector) != query_dim:
                raise VectorSearchError(
                    f"query embedding dim {query_dim} does not match stored dim {len(vector)}"
                )
            scores[chunk_id] = sum(a * b for a, b in zip(query_vector, vector))
        return scores


_REGISTRY: dict[str, VectorSearch] = {ExactScan.name: ExactScan()}


def get(name: str | None) -> VectorSearch:
    resolved = name or DEFAULT_VECTOR_SEARCH
    try:
        return _REGISTRY[resolved]
    except KeyError:
        raise VectorSearchError(
            f"index requires vector search implementation {resolved!r}, which this "
            "server version does not provide"
        ) from None


def register(implementation: VectorSearch) -> None:
    """Register an additional implementation (e.g. an ANN backend)."""
    _REGISTRY[implementation.name] = implementation
