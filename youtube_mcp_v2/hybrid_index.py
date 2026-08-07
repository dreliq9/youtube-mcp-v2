"""Dense + lexical hybrid retrieval over immutable corpus indexes.

Hybrid indexes are siblings of lexical indexes in the same content-addressed
index namespace. They copy the frozen lexical chunks/postings and add normalized
dense vectors, preserving exactly the same corpus/transcript evidence inputs.

The first implementation intentionally uses an exact vector scan. That keeps the
artifact portable, deterministic, and dependency-free at query-storage time. An
ANN implementation can replace the internal scan later without changing the MCP
search contract or evidence identity.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import corpus_index
from .embeddings import EmbeddingBackend, EmbeddingError, normalize_vector

SEMANTIC_SCHEMA_VERSION = 1
RRF_K = 60.0
DEFAULT_EMBED_BATCH = 64


class HybridIndexError(corpus_index.CorpusIndexError):
    """Hybrid index construction/search failure."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes | None, dim: int | None) -> list[float]:
    if blob is None or dim is None or dim <= 0:
        raise HybridIndexError("hybrid index chunk is missing its dense embedding")
    expected = int(dim) * 4
    if len(blob) != expected:
        raise HybridIndexError(
            f"embedding blob has {len(blob)} bytes, expected {expected} for dim={dim}"
        )
    return list(struct.unpack(f"<{int(dim)}f", blob))


def _publish_immutable(tmp: Path, path: Path) -> None:
    """Publish a completed index without overwriting an existing revision."""
    try:
        os.link(tmp, path)
    except FileExistsError:
        return
    except OSError:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return
        try:
            with os.fdopen(fd, "wb") as out, tmp.open("rb") as src:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    out.write(block)
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise


def _copy_sqlite(source: Path, target: Path) -> None:
    source_conn = sqlite3.connect(source)
    target_conn = sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
        target_conn.commit()
    finally:
        source_conn.close()
        target_conn.close()


def _hybrid_identity(
    lexical: corpus_index.PreparedIndex,
    *,
    model_id: str,
) -> dict[str, Any]:
    meta = corpus_index._read_meta(lexical.path)
    return {
        "semantic_schema_version": SEMANTIC_SCHEMA_VERSION,
        "backend": "hybrid",
        "model_id": model_id,
        "parent_index_revision": lexical.index_revision,
        "parent_identity_sha256": meta.get("identity_sha256"),
        "corpus_revision": lexical.corpus_revision,
        "corpus_sha256": lexical.corpus_sha256,
        "requested_lang": lexical.requested_lang,
        "vector_normalization": "l2-f32le",
    }


def prepare_hybrid_index(
    handle: str,
    *,
    backend: EmbeddingBackend,
    requested_lang: str = "en",
    chunk_tokens: int = corpus_index.DEFAULT_CHUNK_TOKENS,
    chunk_overlap: int = corpus_index.DEFAULT_CHUNK_OVERLAP,
    batch_size: int = DEFAULT_EMBED_BATCH,
) -> corpus_index.PreparedIndex:
    """Build/reuse a dense hybrid index over one immutable lexical evidence set."""
    if batch_size <= 0:
        raise HybridIndexError("embedding batch_size must be > 0")
    model_id = str(getattr(backend, "model_id", "")).strip()
    if not model_id:
        raise HybridIndexError("embedding backend must expose a non-empty model_id")

    lexical = corpus_index.prepare_index(
        handle,
        requested_lang=requested_lang,
        chunk_tokens=chunk_tokens,
        chunk_overlap=chunk_overlap,
    )
    identity = _hybrid_identity(lexical, model_id=model_id)
    index_revision = f"idx1-{corpus_index._sha256_json(identity)[:24]}"
    path = corpus_index.index_path(handle, index_revision)
    if path.exists():
        prepared = corpus_index._prepared_from_path(path, cached=True)
        if prepared.backend != "hybrid" or prepared.model_id != model_id:
            raise HybridIndexError(
                f"index revision collision for {index_revision}: unexpected backend/model"
            )
        return prepared

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if tmp.exists():
        tmp.unlink()
    _copy_sqlite(lexical.path, tmp)

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, text FROM chunks ORDER BY id").fetchall()
        expected_dim: int | None = None
        for offset in range(0, len(rows), batch_size):
            batch = rows[offset : offset + batch_size]
            texts = [str(row["text"]) for row in batch]
            try:
                vectors = backend.embed_passages(texts)
            except EmbeddingError:
                raise
            except Exception as exc:
                raise HybridIndexError("embedding backend failed during passage indexing") from exc
            if len(vectors) != len(batch):
                raise HybridIndexError(
                    f"embedding backend returned {len(vectors)} vectors for "
                    f"{len(batch)} passages"
                )

            for row, vector in zip(batch, vectors):
                normalized = normalize_vector(vector)
                dim = len(normalized)
                if expected_dim is None:
                    expected_dim = dim
                elif dim != expected_dim:
                    raise HybridIndexError(
                        f"embedding dimension changed from {expected_dim} to {dim}"
                    )
                conn.execute(
                    "UPDATE chunks SET embedding = ?, embedding_dim = ? WHERE id = ?",
                    (_pack_vector(normalized), dim, int(row["id"])),
                )

        if rows and expected_dim is None:
            raise HybridIndexError("no embedding dimension resolved for non-empty index")

        updates: dict[str, Any] = {
            "backend": "hybrid",
            "model_id": model_id,
            "index_revision": index_revision,
            "created_at": _now_iso(),
            "identity_sha256": corpus_index._sha256_json(identity),
            "semantic_schema_version": SEMANTIC_SCHEMA_VERSION,
            "parent_index_revision": lexical.index_revision,
            "embedding_dim": expected_dim or 0,
            "vector_normalization": "l2-f32le",
        }
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [(key, str(value)) for key, value in updates.items()],
        )
        conn.commit()
    except Exception:
        conn.close()
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    else:
        conn.close()

    try:
        _publish_immutable(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    prepared = corpus_index._prepared_from_path(path, cached=False)
    if prepared.backend != "hybrid" or prepared.model_id != model_id:
        raise HybridIndexError("published hybrid index metadata is inconsistent")
    return prepared


def latest_hybrid_index(
    handle: str,
    *,
    requested_lang: str = "en",
    model_id: str | None = None,
) -> corpus_index.PreparedIndex | None:
    for prepared in corpus_index.list_indexes(handle, requested_lang=requested_lang):
        if prepared.backend != "hybrid":
            continue
        if model_id is not None and prepared.model_id != model_id:
            continue
        return prepared
    return None


def latest_lexical_index(
    handle: str,
    *,
    requested_lang: str = "en",
) -> corpus_index.PreparedIndex | None:
    for prepared in corpus_index.list_indexes(handle, requested_lang=requested_lang):
        if prepared.backend == "lexical":
            return prepared
    return None


def _deterministic_row_key(row: sqlite3.Row) -> tuple[str, float, int]:
    return (
        str(row["video_id"]),
        float(row["start_s"] or 0.0),
        int(row["chunk_index"]),
    )


def _lexical_scores(
    conn: sqlite3.Connection,
    *,
    query: str,
    rows_by_id: dict[int, sqlite3.Row],
    total_chunks: int,
    avg_doc_len: float,
    validated_only: bool,
) -> dict[int, float]:
    query_terms = corpus_index.tokenize(query)
    if not query_terms:
        return {}

    query_counts = Counter(query_terms)
    scores: defaultdict[int, float] = defaultdict(float)
    k1 = 1.2
    b = 0.75
    for term, qtf in query_counts.items():
        stat = conn.execute(
            "SELECT doc_freq FROM term_stats WHERE term = ?", (term,)
        ).fetchone()
        if stat is None:
            continue
        df = int(stat["doc_freq"])
        idf = math.log(1.0 + (total_chunks - df + 0.5) / (df + 0.5))
        q_weight = 1.0 + math.log(float(qtf)) if qtf > 1 else 1.0
        postings = conn.execute(
            "SELECT p.chunk_id, p.tf, c.length_terms FROM postings p "
            "JOIN chunks c ON c.id = p.chunk_id WHERE p.term = ?",
            (term,),
        ).fetchall()
        for posting in postings:
            chunk_id = int(posting["chunk_id"])
            row = rows_by_id.get(chunk_id)
            if row is None or (validated_only and not bool(row["validated"])):
                continue
            tf = float(posting["tf"])
            dl = float(posting["length_terms"])
            norm = 1.0 - b + b * (dl / avg_doc_len if avg_doc_len else 1.0)
            score = idf * ((tf * (k1 + 1.0)) / (tf + k1 * norm))
            scores[chunk_id] += score * q_weight

    query_lower = query.casefold()
    for chunk_id in list(scores):
        if query_lower in str(rows_by_id[chunk_id]["text"]).casefold():
            scores[chunk_id] += 0.35
    return dict(scores)


def _semantic_scores(
    *,
    query_vector: list[float],
    rows_by_id: dict[int, sqlite3.Row],
    validated_only: bool,
) -> dict[int, float]:
    scores: dict[int, float] = {}
    query_dim = len(query_vector)
    for chunk_id, row in rows_by_id.items():
        if validated_only and not bool(row["validated"]):
            continue
        vector = _unpack_vector(row["embedding"], row["embedding_dim"])
        if len(vector) != query_dim:
            raise HybridIndexError(
                f"query embedding dim {query_dim} does not match stored dim {len(vector)}"
            )
        # Both passage/query vectors are explicitly L2-normalized at our boundary,
        # so dot product is cosine similarity.
        scores[chunk_id] = sum(a * b for a, b in zip(query_vector, vector))
    return scores


def _rank_scores(
    scores: dict[int, float],
    rows_by_id: dict[int, sqlite3.Row],
    *,
    candidate_limit: int,
) -> list[tuple[int, float]]:
    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], *_deterministic_row_key(rows_by_id[item[0]])),
    )
    return ranked[:candidate_limit]


def search_hybrid_index(
    handle: str,
    query: str,
    *,
    backend: EmbeddingBackend,
    top_k: int = 10,
    index_revision: str | None = None,
    requested_lang: str = "en",
    validated_only: bool = False,
) -> dict[str, Any]:
    """Search dense + lexical evidence and fuse independent rankings with RRF."""
    query = query.strip()
    if not query:
        raise HybridIndexError("query must not be empty")
    if top_k < 1 or top_k > corpus_index.MAX_TOP_K:
        raise HybridIndexError(
            f"top_k must be between 1 and {corpus_index.MAX_TOP_K}"
        )

    model_id = str(getattr(backend, "model_id", "")).strip()
    if not model_id:
        raise HybridIndexError("embedding backend must expose a non-empty model_id")

    if index_revision is not None:
        path = corpus_index.index_path(handle, index_revision)
        prepared = corpus_index._prepared_from_path(path, cached=True)
    else:
        prepared = latest_hybrid_index(
            handle, requested_lang=requested_lang, model_id=model_id
        )
        if prepared is None:
            raise corpus_index.CorpusIndexNotFound(
                f"no hybrid index for {handle!r} language {requested_lang!r} "
                f"and model {model_id!r}"
            )
        path = prepared.path

    if prepared.backend != "hybrid":
        raise HybridIndexError(
            f"index {prepared.index_revision} is {prepared.backend!r}, not hybrid"
        )
    if prepared.requested_lang != requested_lang:
        raise HybridIndexError(
            f"index {prepared.index_revision} was built for "
            f"{prepared.requested_lang!r}, not {requested_lang!r}"
        )
    if prepared.model_id != model_id:
        raise HybridIndexError(
            f"index model {prepared.model_id!r} does not match active model {model_id!r}"
        )

    try:
        query_vector = normalize_vector(backend.embed_query(query))
    except EmbeddingError:
        raise
    except Exception as exc:
        raise HybridIndexError("embedding backend failed during query embedding") from exc

    meta = corpus_index._read_meta(path)
    try:
        total_chunks = int(meta.get("chunk_count", "0"))
        avg_doc_len = float(meta.get("avg_doc_len", "0"))
        stored_dim = int(meta.get("embedding_dim", "0"))
    except ValueError as exc:
        raise HybridIndexError("hybrid index metadata contains invalid statistics") from exc
    if stored_dim and len(query_vector) != stored_dim:
        raise HybridIndexError(
            f"query embedding dim {len(query_vector)} does not match index dim {stored_dim}"
        )

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM chunks").fetchall()
        rows_by_id = {int(row["id"]): row for row in rows}
        if total_chunks != len(rows_by_id):
            raise HybridIndexError(
                f"hybrid index metadata says {total_chunks} chunks but stores {len(rows_by_id)}"
            )

        lexical_scores = _lexical_scores(
            conn,
            query=query,
            rows_by_id=rows_by_id,
            total_chunks=total_chunks,
            avg_doc_len=avg_doc_len,
            validated_only=validated_only,
        )
        semantic_scores = _semantic_scores(
            query_vector=query_vector,
            rows_by_id=rows_by_id,
            validated_only=validated_only,
        )

        candidate_limit = min(total_chunks, max(100, top_k * 20))
        lexical_ranked = _rank_scores(
            lexical_scores, rows_by_id, candidate_limit=candidate_limit
        )
        semantic_ranked = _rank_scores(
            semantic_scores, rows_by_id, candidate_limit=candidate_limit
        )

        lexical_rank = {
            chunk_id: rank
            for rank, (chunk_id, _score) in enumerate(lexical_ranked, start=1)
        }
        semantic_rank = {
            chunk_id: rank
            for rank, (chunk_id, _score) in enumerate(semantic_ranked, start=1)
        }
        candidate_ids = set(lexical_rank) | set(semantic_rank)

        fused: list[tuple[float, int]] = []
        for chunk_id in candidate_ids:
            score = 0.0
            if chunk_id in lexical_rank:
                score += 1.0 / (RRF_K + lexical_rank[chunk_id])
            if chunk_id in semantic_rank:
                score += 1.0 / (RRF_K + semantic_rank[chunk_id])
            fused.append((score, chunk_id))
        fused.sort(
            key=lambda item: (-item[0], *_deterministic_row_key(rows_by_id[item[1]]))
        )
        fused = fused[:top_k]

        max_fused = fused[0][0] if fused else 1.0
        max_lex = max(lexical_scores.values(), default=0.0)
        hits: list[dict[str, Any]] = []
        for rank, (hybrid_raw, chunk_id) in enumerate(fused, start=1):
            row = rows_by_id[chunk_id]
            lexical_raw = lexical_scores.get(chunk_id, 0.0)
            lexical_norm = lexical_raw / max_lex if max_lex > 0.0 else 0.0
            hit = corpus_index._row_to_hit(
                row,
                lexical_raw=lexical_raw,
                lexical_norm=lexical_norm,
            )
            hit["rank"] = rank
            hit["scores"] = {
                "lexical": lexical_norm,
                "lexical_raw": lexical_raw,
                "semantic": semantic_scores.get(chunk_id),
                "hybrid": hybrid_raw / max_fused if max_fused else 0.0,
                "hybrid_raw": hybrid_raw,
                "lexical_rank": lexical_rank.get(chunk_id),
                "semantic_rank": semantic_rank.get(chunk_id),
            }
            hits.append(hit)
    finally:
        conn.close()

    return {
        "corpus_revision": handle,
        "index_revision": prepared.index_revision,
        "backend": "hybrid",
        "model_id": prepared.model_id,
        "query": query,
        "coverage": prepared.as_dict(),
        "fusion": {"method": "reciprocal_rank_fusion", "k": RRF_K},
        "hits": hits,
    }
