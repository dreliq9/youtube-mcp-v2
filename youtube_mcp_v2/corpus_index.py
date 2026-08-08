"""Immutable transcript index for frozen YouTube research corpora.

The current public frozen-corpus primitive is a skeleton handle. This module
accepts those legacy handles directly so semantic/evidence search can evolve
without orphaning existing snapshots.

Each index revision freezes the exact transcript cache row IDs and content hashes
used to build it. Index files are content-addressed and never rewritten. This
means a later transcript refresh cannot silently change what an old research run
searched.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import cache, skeleton
from .paths import VECTOR_DIR as _DEFAULT_VECTOR_DIR

VECTOR_DIR = _DEFAULT_VECTOR_DIR
INDEX_ROOT = VECTOR_DIR / "corpus"
INDEX_SCHEMA_VERSION = 1
DEFAULT_CHUNK_TOKENS = 500
DEFAULT_CHUNK_OVERLAP = 50
MAX_TOP_K = 50

_RAW_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[._:/+\-][A-Za-z0-9]+)*")
_SPLIT_TECH_RE = re.compile(r"[._:/+\-]+")


class CorpusIndexError(RuntimeError):
    """Base error for deterministic corpus indexing/search failures."""


class CorpusIndexNotFound(CorpusIndexError):
    pass


@dataclass(frozen=True)
class PreparedIndex:
    corpus_revision: str
    index_revision: str
    path: Path
    corpus_sha256: str
    requested_lang: str
    total_videos: int
    indexed_videos: int
    missing_videos: list[str]
    chunk_count: int
    cached: bool
    backend: str = "lexical"
    model_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        coverage = (
            self.indexed_videos / self.total_videos if self.total_videos else 0.0
        )
        return {
            "corpus_revision": self.corpus_revision,
            "index_revision": self.index_revision,
            "backend": self.backend,
            "model_id": self.model_id,
            "requested_lang": self.requested_lang,
            "total_videos": self.total_videos,
            "indexed_videos": self.indexed_videos,
            "missing_videos": self.missing_videos,
            "coverage": coverage,
            "chunk_count": self.chunk_count,
            "cached": self.cached,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def tokenize(text: str) -> list[str]:
    """Tokenize prose while retaining technical identifiers.

    `TPS62132-Q1` yields both `tps62132-q1` and component tokens. Exact part
    numbers therefore survive lexical retrieval while ordinary words still
    behave like a conventional BM25 corpus.
    """
    out: list[str] = []
    for match in _RAW_TOKEN_RE.finditer(text):
        raw = match.group(0).lower()
        out.append(raw)
        if _SPLIT_TECH_RE.search(raw):
            for part in _SPLIT_TECH_RE.split(raw):
                if len(part) >= 2 and part != raw:
                    out.append(part)
    return out


def _validate_chunk_budget(chunk_tokens: int, chunk_overlap: int) -> None:
    if chunk_tokens <= 0:
        raise CorpusIndexError("chunk_tokens must be > 0")
    if chunk_overlap < 0 or chunk_overlap >= chunk_tokens:
        raise CorpusIndexError(
            "chunk_overlap must be >= 0 and smaller than chunk_tokens"
        )


def _segment_end(segment: dict[str, Any]) -> float | None:
    try:
        start = float(segment.get("start_s", 0.0))
    except (TypeError, ValueError):
        return None
    if segment.get("end_s") is not None:
        try:
            return float(segment["end_s"])
        except (TypeError, ValueError):
            pass
    try:
        return start + float(segment.get("duration_s", 0.0))
    except (TypeError, ValueError):
        return start


def chunk_segments(
    segments: list[dict[str, Any]],
    *,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[dict[str, Any]]:
    """Pack timed segments into reproducible timestamp-preserving chunks."""
    _validate_chunk_budget(chunk_tokens, chunk_overlap)
    if not segments:
        return []

    chunks: list[dict[str, Any]] = []
    i = 0
    while i < len(segments):
        buf: list[dict[str, Any]] = []
        used = 0
        j = i
        while j < len(segments):
            text = str(segments[j].get("text") or "").strip()
            seg_tokens = _estimate_tokens(text)
            if buf and used + seg_tokens > chunk_tokens:
                break
            buf.append(segments[j])
            used += seg_tokens
            j += 1
            if used >= chunk_tokens:
                break

        if not buf:
            break

        text = " ".join(str(seg.get("text") or "").strip() for seg in buf).strip()
        start_s: float | None
        try:
            start_s = float(buf[0].get("start_s", 0.0))
        except (TypeError, ValueError):
            start_s = None
        end_s = _segment_end(buf[-1])
        chunks.append(
            {
                "chunk_index": len(chunks),
                "segment_start": i,
                "segment_end": j,
                "start_s": start_s,
                "end_s": end_s,
                "text": text,
                "token_estimate": _estimate_tokens(text),
            }
        )

        if j >= len(segments):
            break

        overlap = 0
        rewind = j
        while rewind > i and overlap < chunk_overlap:
            rewind -= 1
            overlap += _estimate_tokens(str(segments[rewind].get("text") or ""))
        i = max(rewind, i + 1)

    return chunks


def _latest_transcript_revision(video_id: str, lang: str) -> dict[str, Any] | None:
    """Return the newest append-only transcript revision with deterministic ties."""
    with cache.connect() as conn:
        row = conn.execute(
            "SELECT * FROM transcripts WHERE video_id = ? AND lang = ? "
            "ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (video_id, lang),
        ).fetchone()
    return dict(row) if row is not None else None


def _transcript_hash(row: dict[str, Any]) -> str:
    return _sha256_json(
        {
            "id": row.get("id"),
            "video_id": row.get("video_id"),
            "requested_lang": row.get("lang"),
            "actual_lang": row.get("actual_lang"),
            "is_generated": row.get("is_generated"),
            "text": row.get("text"),
            "segments_json": row.get("segments_json"),
            "validated": row.get("validated"),
            "warnings_json": row.get("warnings_json"),
            "fetched_at": row.get("fetched_at"),
        }
    )


def _corpus_hash(payload: dict[str, Any]) -> str:
    # `expired_at` is deliberately excluded. Expiration is mutable lifecycle
    # metadata; it must not alter the identity of the captured membership.
    return _sha256_json(
        {
            "handle": payload.get("handle"),
            "target": payload.get("target"),
            "value": payload.get("value"),
            "built_at": payload.get("built_at"),
            "source": payload.get("source"),
            "videos": payload.get("videos", []),
        }
    )


def _index_dir(handle: str) -> Path:
    if not skeleton.is_valid_handle(handle):
        raise CorpusIndexError(f"invalid corpus revision handle: {handle!r}")
    return INDEX_ROOT / handle


def index_path(handle: str, index_revision: str) -> Path:
    if not re.fullmatch(r"idx1-[0-9a-f]{24}", index_revision):
        raise CorpusIndexError(f"invalid index revision: {index_revision!r}")
    return _index_dir(handle) / f"{index_revision}.sqlite"


def _read_meta(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise CorpusIndexNotFound(f"index does not exist: {path}")
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.Error as exc:
        raise CorpusIndexError(f"invalid corpus index at {path}: {exc}") from exc
    finally:
        conn.close()
    return {str(key): str(value) for key, value in rows}


def _prepared_from_path(path: Path, *, cached: bool) -> PreparedIndex:
    meta = _read_meta(path)
    try:
        missing = json.loads(meta.get("missing_videos", "[]"))
        return PreparedIndex(
            corpus_revision=meta["corpus_revision"],
            index_revision=meta["index_revision"],
            path=path,
            corpus_sha256=meta["corpus_sha256"],
            requested_lang=meta["requested_lang"],
            total_videos=int(meta["total_videos"]),
            indexed_videos=int(meta["indexed_videos"]),
            missing_videos=list(missing),
            chunk_count=int(meta["chunk_count"]),
            cached=cached,
            backend=meta.get("backend", "lexical"),
            model_id=meta.get("model_id") or None,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CorpusIndexError(f"incomplete corpus index metadata: {path}") from exc


def list_indexes(handle: str, *, requested_lang: str | None = None) -> list[PreparedIndex]:
    directory = _index_dir(handle)
    if not directory.exists():
        return []
    found: list[tuple[str, PreparedIndex]] = []
    for path in directory.glob("idx1-*.sqlite"):
        try:
            prepared = _prepared_from_path(path, cached=True)
            meta = _read_meta(path)
        except CorpusIndexError:
            continue
        if requested_lang and prepared.requested_lang != requested_lang:
            continue
        found.append((meta.get("created_at", ""), prepared))
    found.sort(key=lambda pair: (pair[0], pair[1].index_revision), reverse=True)
    return [prepared for _created, prepared in found]


def latest_index(handle: str, *, requested_lang: str = "en") -> PreparedIndex | None:
    indexes = list_indexes(handle, requested_lang=requested_lang)
    return indexes[0] if indexes else None


def _build_source_chunks(
    corpus: dict[str, Any],
    *,
    requested_lang: str,
    chunk_tokens: int,
    chunk_overlap: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    videos = corpus.get("videos")
    if not isinstance(videos, list):
        raise CorpusIndexError("corpus revision has no valid videos list")

    sources: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[str] = set()

    for video in videos:
        if not isinstance(video, dict):
            raise CorpusIndexError("corpus video entries must be objects")
        video_id = str(video.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise CorpusIndexError(f"invalid video id in corpus: {video_id!r}")
        if video_id in seen:
            raise CorpusIndexError(f"duplicate video id in corpus: {video_id}")
        seen.add(video_id)

        row = _latest_transcript_revision(video_id, requested_lang)
        if row is None:
            missing.append(video_id)
            continue

        transcript_sha = _transcript_hash(row)
        try:
            segments = json.loads(row.get("segments_json") or "[]")
        except json.JSONDecodeError as exc:
            raise CorpusIndexError(
                f"transcript revision {row.get('id')} has invalid segments JSON"
            ) from exc
        if not isinstance(segments, list):
            segments = []

        if segments:
            video_chunks = chunk_segments(
                segments,
                chunk_tokens=chunk_tokens,
                chunk_overlap=chunk_overlap,
            )
        else:
            text = str(row.get("text") or "").strip()
            video_chunks = (
                [
                    {
                        "chunk_index": 0,
                        "segment_start": None,
                        "segment_end": None,
                        "start_s": None,
                        "end_s": None,
                        "text": text,
                        "token_estimate": _estimate_tokens(text),
                    }
                ]
                if text
                else []
            )

        if not video_chunks:
            missing.append(video_id)
            continue

        source = {
            "video_id": video_id,
            "title": video.get("title"),
            "channel": video.get("channel"),
            "transcript_revision_id": int(row["id"]),
            "transcript_sha256": transcript_sha,
            "requested_lang": row.get("lang") or requested_lang,
            "actual_lang": row.get("actual_lang") or row.get("lang") or requested_lang,
            "is_generated": (
                None if row.get("is_generated") is None else bool(row.get("is_generated"))
            ),
            "validated": bool(row.get("validated")),
            "transcript_fetched_at": row.get("fetched_at"),
            "warnings": json.loads(row.get("warnings_json") or "[]"),
        }
        sources.append(source)

        for chunk in video_chunks:
            chunk_payload = {
                **chunk,
                **source,
            }
            chunk_payload["chunk_sha256"] = _sha256_json(
                {
                    "video_id": video_id,
                    "transcript_revision_id": source["transcript_revision_id"],
                    "transcript_sha256": transcript_sha,
                    "chunk_index": chunk["chunk_index"],
                    "start_s": chunk["start_s"],
                    "end_s": chunk["end_s"],
                    "text": chunk["text"],
                }
            )
            chunks.append(chunk_payload)

    return sources, chunks, missing


_INDEX_SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL,
    title TEXT,
    channel TEXT,
    chunk_index INTEGER NOT NULL,
    start_s REAL,
    end_s REAL,
    text TEXT NOT NULL,
    length_terms INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL,
    chunk_sha256 TEXT NOT NULL,
    transcript_revision_id INTEGER NOT NULL,
    transcript_sha256 TEXT NOT NULL,
    requested_lang TEXT NOT NULL,
    actual_lang TEXT NOT NULL,
    is_generated INTEGER,
    validated INTEGER NOT NULL,
    transcript_fetched_at TEXT,
    warnings_json TEXT NOT NULL,
    embedding BLOB,
    embedding_dim INTEGER
);
CREATE INDEX idx_chunks_video ON chunks(video_id, chunk_index);
CREATE TABLE postings (
    term TEXT NOT NULL,
    chunk_id INTEGER NOT NULL,
    tf INTEGER NOT NULL,
    PRIMARY KEY(term, chunk_id)
);
CREATE INDEX idx_postings_term ON postings(term);
CREATE TABLE term_stats (
    term TEXT PRIMARY KEY,
    doc_freq INTEGER NOT NULL
);
"""


def prepare_index(
    handle: str,
    *,
    requested_lang: str = "en",
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> PreparedIndex:
    """Build or reuse an immutable lexical index for one frozen corpus revision."""
    _validate_chunk_budget(chunk_tokens, chunk_overlap)
    requested_lang = requested_lang.strip()
    if not requested_lang:
        raise CorpusIndexError("requested_lang must not be empty")

    corpus = skeleton.load_skeleton(handle)
    corpus_sha = _corpus_hash(corpus)
    sources, chunks, missing = _build_source_chunks(
        corpus,
        requested_lang=requested_lang,
        chunk_tokens=chunk_tokens,
        chunk_overlap=chunk_overlap,
    )

    identity = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "backend": "lexical",
        "model_id": None,
        "corpus_revision": handle,
        "corpus_sha256": corpus_sha,
        "requested_lang": requested_lang,
        "chunk_tokens": chunk_tokens,
        "chunk_overlap": chunk_overlap,
        "sources": sorted(
            [
                {
                    "video_id": source["video_id"],
                    "transcript_revision_id": source["transcript_revision_id"],
                    "transcript_sha256": source["transcript_sha256"],
                }
                for source in sources
            ],
            key=lambda item: item["video_id"],
        ),
    }
    index_revision = f"idx1-{_sha256_json(identity)[:24]}"
    path = index_path(handle, index_revision)
    if path.exists():
        return _prepared_from_path(path, cached=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if tmp.exists():
        tmp.unlink()

    term_df: Counter[str] = Counter()
    total_terms = 0
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(_INDEX_SCHEMA)
        for chunk_id, chunk in enumerate(chunks, start=1):
            terms = tokenize(chunk["text"])
            counts = Counter(terms)
            length_terms = max(1, len(terms))
            total_terms += length_terms
            for term in counts:
                term_df[term] += 1

            conn.execute(
                "INSERT INTO chunks ("
                "id, video_id, title, channel, chunk_index, start_s, end_s, text, "
                "length_terms, token_estimate, chunk_sha256, transcript_revision_id, "
                "transcript_sha256, requested_lang, actual_lang, is_generated, "
                "validated, transcript_fetched_at, warnings_json, embedding, embedding_dim"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    chunk_id,
                    chunk["video_id"],
                    chunk.get("title"),
                    chunk.get("channel"),
                    int(chunk["chunk_index"]),
                    chunk.get("start_s"),
                    chunk.get("end_s"),
                    chunk["text"],
                    length_terms,
                    int(chunk["token_estimate"]),
                    chunk["chunk_sha256"],
                    int(chunk["transcript_revision_id"]),
                    chunk["transcript_sha256"],
                    str(chunk["requested_lang"]),
                    str(chunk["actual_lang"]),
                    (
                        None
                        if chunk.get("is_generated") is None
                        else (1 if chunk.get("is_generated") else 0)
                    ),
                    1 if chunk.get("validated") else 0,
                    chunk.get("transcript_fetched_at"),
                    json.dumps(chunk.get("warnings") or []),
                ),
            )
            conn.executemany(
                "INSERT INTO postings (term, chunk_id, tf) VALUES (?, ?, ?)",
                [(term, chunk_id, int(tf)) for term, tf in counts.items()],
            )

        conn.executemany(
            "INSERT INTO term_stats (term, doc_freq) VALUES (?, ?)",
            [(term, int(df)) for term, df in term_df.items()],
        )

        total_videos = len(corpus.get("videos") or [])
        avg_doc_len = total_terms / len(chunks) if chunks else 0.0
        meta: dict[str, Any] = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "index_revision": index_revision,
            "corpus_revision": handle,
            "corpus_sha256": corpus_sha,
            "created_at": _now_iso(),
            "backend": "lexical",
            "model_id": "",
            "requested_lang": requested_lang,
            "chunk_tokens": chunk_tokens,
            "chunk_overlap": chunk_overlap,
            "total_videos": total_videos,
            "indexed_videos": len(sources),
            "missing_videos": missing,
            "chunk_count": len(chunks),
            "avg_doc_len": avg_doc_len,
            "identity_sha256": _sha256_json(identity),
            "source_revisions": identity["sources"],
        }
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                (
                    key,
                    _canonical_json(value)
                    if isinstance(value, (list, dict))
                    else str(value),
                )
                for key, value in meta.items()
            ],
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
        # os.link gives us an atomic no-overwrite publication step on the same
        # filesystem. Another process that built the same content-addressed index
        # first wins; both produced equivalent bytes by contract.
        os.link(tmp, path)
    except FileExistsError:
        pass
    except OSError:
        # Some filesystems do not permit hard links. O_EXCL preserves the
        # no-overwrite invariant; copy bytes only after claiming the path.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as out, tmp.open("rb") as src:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    out.write(block)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    return _prepared_from_path(path, cached=False)


def _row_to_hit(row: sqlite3.Row, *, lexical_raw: float, lexical_norm: float) -> dict[str, Any]:
    start_s = row["start_s"]
    if start_s is None:
        url = f"https://www.youtube.com/watch?v={row['video_id']}"
    else:
        url = (
            f"https://www.youtube.com/watch?v={row['video_id']}"
            f"&t={max(0, int(float(start_s)))}s"
        )
    raw_generated = row["is_generated"]
    return {
        "video_id": row["video_id"],
        "title": row["title"],
        "channel": row["channel"],
        "url": url,
        "start_s": start_s,
        "end_s": row["end_s"],
        "excerpt": row["text"],
        "scores": {
            "lexical": lexical_norm,
            "lexical_raw": lexical_raw,
            "semantic": None,
            "hybrid": lexical_norm,
        },
        "chunk": {
            "index": row["chunk_index"],
            "sha256": row["chunk_sha256"],
        },
        "transcript": {
            "revision_id": row["transcript_revision_id"],
            "sha256": row["transcript_sha256"],
            "requested_lang": row["requested_lang"],
            "actual_lang": row["actual_lang"],
            "is_generated": (
                None if raw_generated is None else bool(raw_generated)
            ),
            "validated": bool(row["validated"]),
            "fetched_at": row["transcript_fetched_at"],
            "warnings": json.loads(row["warnings_json"] or "[]"),
        },
    }


def search_index(
    handle: str,
    query: str,
    *,
    top_k: int = 10,
    index_revision: str | None = None,
    requested_lang: str = "en",
    validated_only: bool = False,
) -> dict[str, Any]:
    """Search one immutable index revision using a BM25-style lexical scorer."""
    query = query.strip()
    if not query:
        raise CorpusIndexError("query must not be empty")
    if top_k < 1 or top_k > MAX_TOP_K:
        raise CorpusIndexError(f"top_k must be between 1 and {MAX_TOP_K}")

    prepared: PreparedIndex | None
    if index_revision is not None:
        path = index_path(handle, index_revision)
        prepared = _prepared_from_path(path, cached=True)
        if prepared.requested_lang != requested_lang:
            raise CorpusIndexError(
                f"index {index_revision} was built for {prepared.requested_lang!r}, "
                f"not {requested_lang!r}"
            )
    else:
        prepared = latest_index(handle, requested_lang=requested_lang)
        if prepared is None:
            raise CorpusIndexNotFound(
                f"no prepared index for {handle!r} language {requested_lang!r}"
            )
        path = prepared.path

    query_terms = tokenize(query)
    if not query_terms:
        raise CorpusIndexError("query contains no searchable terms")
    query_counts = Counter(query_terms)

    meta = _read_meta(path)
    try:
        total_chunks = int(meta.get("chunk_count", "0"))
        avg_doc_len = float(meta.get("avg_doc_len", "0"))
    except ValueError as exc:
        raise CorpusIndexError("index metadata contains invalid corpus statistics") from exc

    if total_chunks <= 0:
        return {
            "corpus_revision": handle,
            "index_revision": prepared.index_revision,
            "backend": prepared.backend,
            "model_id": prepared.model_id,
            "query": query,
            "coverage": prepared.as_dict(),
            "hits": [],
        }

    scores: defaultdict[int, float] = defaultdict(float)
    k1 = 1.2
    b = 0.75
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        for term, qtf in query_counts.items():
            stat = conn.execute(
                "SELECT doc_freq FROM term_stats WHERE term = ?", (term,)
            ).fetchone()
            if stat is None:
                continue
            df = int(stat["doc_freq"])
            idf = math.log(1.0 + (total_chunks - df + 0.5) / (df + 0.5))
            q_weight = 1.0 + math.log(float(qtf)) if qtf > 1 else 1.0
            rows = conn.execute(
                "SELECT p.chunk_id, p.tf, c.length_terms FROM postings p "
                "JOIN chunks c ON c.id = p.chunk_id WHERE p.term = ?",
                (term,),
            ).fetchall()
            for row in rows:
                tf = float(row["tf"])
                dl = float(row["length_terms"])
                norm = 1.0 - b + b * (dl / avg_doc_len if avg_doc_len else 1.0)
                contribution = idf * ((tf * (k1 + 1.0)) / (tf + k1 * norm))
                scores[int(row["chunk_id"])] += contribution * q_weight

        if not scores:
            return {
                "corpus_revision": handle,
                "index_revision": prepared.index_revision,
                "backend": prepared.backend,
                "model_id": prepared.model_id,
                "query": query,
                "coverage": prepared.as_dict(),
                "hits": [],
            }

        candidate_ids = list(scores.keys())
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = conn.execute(
            f"SELECT * FROM chunks WHERE id IN ({placeholders})",
            candidate_ids,
        ).fetchall()
        by_id = {int(row["id"]): row for row in rows}

        query_lower = query.casefold()
        ranked: list[tuple[float, int]] = []
        for chunk_id, score in scores.items():
            row = by_id.get(chunk_id)
            if row is None:
                continue
            if validated_only and not bool(row["validated"]):
                continue
            # A small exact-phrase boost improves technical/name queries without
            # allowing phrase matching to replace the token-based candidate gate.
            if query_lower in str(row["text"]).casefold():
                score += 0.35
            ranked.append((score, chunk_id))

        ranked.sort(
            key=lambda item: (
                -item[0],
                str(by_id[item[1]]["video_id"]),
                float(by_id[item[1]]["start_s"] or 0.0),
                int(by_id[item[1]]["chunk_index"]),
            )
        )
        ranked = ranked[:top_k]
        max_score = ranked[0][0] if ranked else 1.0
        hits = [
            {
                "rank": rank,
                **_row_to_hit(
                    by_id[chunk_id],
                    lexical_raw=score,
                    lexical_norm=(score / max_score if max_score else 0.0),
                ),
            }
            for rank, (score, chunk_id) in enumerate(ranked, start=1)
        ]
    finally:
        conn.close()

    return {
        "corpus_revision": handle,
        "index_revision": prepared.index_revision,
        "backend": prepared.backend,
        "model_id": prepared.model_id,
        "query": query,
        "coverage": prepared.as_dict(),
        "hits": hits,
    }
