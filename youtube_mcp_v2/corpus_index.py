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

import dataclasses
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
from typing import Any, Iterable, Mapping

from . import cache, skeleton
from .paths import VECTOR_DIR as _DEFAULT_VECTOR_DIR

VECTOR_DIR = _DEFAULT_VECTOR_DIR
INDEX_ROOT = VECTOR_DIR / "corpus"
# Schema 2 adds per-source acquisition provenance/validation, source segment
# ranges, timestamp/sentence-boundary chunking, and indexed-duration stats.
# Schema-1 indexes remain readable and searchable when pinned.
INDEX_SCHEMA_VERSION = 2
DEFAULT_CHUNK_TOKENS = 500
DEFAULT_CHUNK_OVERLAP = 50
MAX_TOP_K = 50
# Chunk boundaries prefer sentence ends and speech pauses once a chunk is at
# least CHUNK_MIN_FILL full, so evidence excerpts do not start mid-thought.
CHUNKER_ID = "timestamp-boundary-v1"
LEGACY_CHUNKER_ID = "token-window-v1"
CHUNK_MIN_FILL = 0.6
PAUSE_BOUNDARY_S = 1.5

_RAW_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[._:/+\-][A-Za-z0-9]+)*")
_SPLIT_TECH_RE = re.compile(r"[._:/+\-]+")
_SENTENCE_END_RE = re.compile(r"[.!?\u2026][\"'\u201d\u2019)\]]*$")
_QUOTED_RE = re.compile(r'"([^"]{2,})"|\u201c([^\u201d]{2,})\u201d')
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


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
    schema_version: int = 1
    chunker: str = LEGACY_CHUNKER_ID
    indexed_duration_s: float | None = None
    corpus_manifest_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        coverage = (
            self.indexed_videos / self.total_videos if self.total_videos else 0.0
        )
        return {
            "corpus_revision": self.corpus_revision,
            "corpus_manifest_sha256": self.corpus_manifest_sha256,
            "index_revision": self.index_revision,
            "index_schema_version": self.schema_version,
            "backend": self.backend,
            "model_id": self.model_id,
            "chunker": self.chunker,
            "requested_lang": self.requested_lang,
            "total_videos": self.total_videos,
            "indexed_videos": self.indexed_videos,
            "missing_videos": self.missing_videos,
            "coverage": coverage,
            "chunk_count": self.chunk_count,
            "indexed_duration_s": self.indexed_duration_s,
            "indexed_hours": (
                round(self.indexed_duration_s / 3600.0, 2)
                if self.indexed_duration_s is not None
                else None
            ),
            "cached": self.cached,
        }


FILTER_KEYS = (
    "video_ids",
    "exclude_video_ids",
    "channels",
    "time_range_s",
    "validated_only",
    "acquisition_methods",
    "is_generated",
)
ACQUISITION_METHODS = ("captions", "local_stt")


def _string_set(raw: Any, key: str) -> frozenset[str]:
    if not isinstance(raw, (list, tuple)) or not all(isinstance(v, str) for v in raw):
        raise CorpusIndexError(f"filters.{key} must be a list of strings")
    values = frozenset(v.strip() for v in raw if v.strip())
    if not values:
        raise CorpusIndexError(f"filters.{key} must not be empty")
    return values


@dataclass(frozen=True)
class SearchFilters:
    """Evidence filters applied inside one immutable index (never re-ranking stats).

    BM25 statistics and dense similarities are always computed over the whole
    frozen index; filters only decide which chunks may be returned, so the same
    filter on the same index revision is reproducible.
    """

    video_ids: frozenset[str] | None = None
    exclude_video_ids: frozenset[str] = frozenset()
    channels: frozenset[str] | None = None
    time_range_s: tuple[float, float] | None = None
    validated_only: bool = False
    acquisition_methods: frozenset[str] | None = None
    is_generated: bool | None = None

    @classmethod
    def parse(
        cls, raw: Mapping[str, Any] | None, *, validated_only: bool = False
    ) -> "SearchFilters":
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise CorpusIndexError("filters must be an object")
        unknown = sorted(set(raw) - set(FILTER_KEYS))
        if unknown:
            raise CorpusIndexError(
                f"unknown filter key(s) {unknown}; allowed: {list(FILTER_KEYS)}"
            )
        kwargs: dict[str, Any] = {}
        for key in ("video_ids", "exclude_video_ids"):
            if raw.get(key) is not None:
                ids = _string_set(raw[key], key)
                bad = sorted(v for v in ids if not _VIDEO_ID_RE.fullmatch(v))
                if bad:
                    raise CorpusIndexError(f"filters.{key} has invalid video id(s): {bad}")
                kwargs[key] = ids
        if raw.get("channels") is not None:
            kwargs["channels"] = _string_set(raw["channels"], "channels")
        if raw.get("time_range_s") is not None:
            window = raw["time_range_s"]
            if not isinstance(window, (list, tuple)):
                raise CorpusIndexError("filters.time_range_s must be [start_s, end_s]")
            try:
                start, end = (float(window[0]), float(window[1]))
                if len(window) != 2:
                    raise ValueError
            except (TypeError, ValueError, IndexError) as exc:
                raise CorpusIndexError(
                    "filters.time_range_s must be [start_s, end_s]"
                ) from exc
            if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end < start:
                raise CorpusIndexError("filters.time_range_s must satisfy 0 <= start_s <= end_s")
            kwargs["time_range_s"] = (start, end)
        if raw.get("acquisition_methods") is not None:
            methods = _string_set(raw["acquisition_methods"], "acquisition_methods")
            bad = sorted(methods - set(ACQUISITION_METHODS))
            if bad:
                raise CorpusIndexError(
                    f"filters.acquisition_methods has unknown value(s) {bad}; "
                    f"allowed: {list(ACQUISITION_METHODS)}"
                )
            kwargs["acquisition_methods"] = methods
        for key in ("validated_only", "is_generated"):
            if raw.get(key) is not None and not isinstance(raw[key], bool):
                raise CorpusIndexError(f"filters.{key} must be a boolean")
        if raw.get("is_generated") is not None:
            kwargs["is_generated"] = raw["is_generated"]
        kwargs["validated_only"] = bool(validated_only or raw.get("validated_only"))
        return cls(**kwargs)

    @property
    def needs_acquisition(self) -> bool:
        return self.acquisition_methods is not None

    def as_dict(self) -> dict[str, Any]:
        """Canonical applied-filter record, echoed in search provenance."""
        out: dict[str, Any] = {}
        if self.video_ids is not None:
            out["video_ids"] = sorted(self.video_ids)
        if self.exclude_video_ids:
            out["exclude_video_ids"] = sorted(self.exclude_video_ids)
        if self.channels is not None:
            out["channels"] = sorted(self.channels)
        if self.time_range_s is not None:
            out["time_range_s"] = list(self.time_range_s)
        if self.validated_only:
            out["validated_only"] = True
        if self.acquisition_methods is not None:
            out["acquisition_methods"] = sorted(self.acquisition_methods)
        if self.is_generated is not None:
            out["is_generated"] = self.is_generated
        return out

    def matches(self, row: Mapping[str, Any], method: str | None = None) -> bool:
        video_id = row["video_id"]
        if self.video_ids is not None and video_id not in self.video_ids:
            return False
        if video_id in self.exclude_video_ids:
            return False
        if self.channels is not None and row["channel"] not in self.channels:
            return False
        if self.validated_only and not bool(row["validated"]):
            return False
        if self.is_generated is not None:
            raw = row["is_generated"]
            if raw is None or bool(raw) != self.is_generated:
                return False
        if self.time_range_s is not None:
            start, end = self.time_range_s
            chunk_start = row["start_s"]
            chunk_end = row["end_s"] if row["end_s"] is not None else chunk_start
            if chunk_start is None or float(chunk_end) < start or float(chunk_start) > end:
                return False
        if self.acquisition_methods is not None and method not in self.acquisition_methods:
            return False
        return True


def validation_status(validated: bool, warnings: Iterable[str]) -> str:
    """Map the v0.2 boolean gate onto EVIDENCE_MODEL statuses.

    `pass` only when every gate ran and passed; `partial` when the only problem
    is a gate that could not run (duration unknown); otherwise `fail`.
    """
    if validated:
        return "pass"
    informational = ("lang fallback", "transcript fallback used")
    blocking = [
        w for w in warnings if not str(w).startswith(informational)
    ]
    if blocking and all(str(w).startswith("duration unavailable") for w in blocking):
        return "partial"
    return "fail"


def _provider_class(method: str | None) -> str | None:
    if method == "local_stt":
        return "local"
    if method == "captions":
        return "scrape"
    return None


def acquisition_record(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Frozen copy of a transcript revision's acquisition provenance, if recorded."""
    provider = row.get("provider")
    method = row.get("method")
    if provider is None and method is None:
        return None  # historical v0.2 row: never fabricate provenance
    try:
        attempts = json.loads(row.get("attempts_json") or "[]")
    except json.JSONDecodeError:
        attempts = []
    try:
        details = json.loads(row.get("details_json") or "null")
    except json.JSONDecodeError:
        details = None
    record: dict[str, Any] = {
        "provider": provider,
        "method": method,
        "provider_class": _provider_class(method),
        "attempts": attempts if isinstance(attempts, list) else [],
    }
    if isinstance(details, dict):
        record["details"] = details
    return record


def exact_terms(query: str) -> tuple[list[str], list[str]]:
    """Identifier-like tokens and quoted phrases that must match verbatim.

    Identifiers are tokens with a digit (length >= 2) or a technical separator:
    part numbers, model names, dates, quantities, citations, codes. Quoted
    phrases are matched case-insensitively against the excerpt text.
    """
    phrases: list[str] = []
    for match in _QUOTED_RE.finditer(query):
        phrase = (match.group(1) or match.group(2) or "").strip().casefold()
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    identifiers: list[str] = []
    for match in _RAW_TOKEN_RE.finditer(query):
        raw = match.group(0).lower()
        has_digit = any(ch.isdigit() for ch in raw)
        if (has_digit and len(raw) >= 2) or _SPLIT_TECH_RE.search(raw):
            if raw not in identifiers:
                identifiers.append(raw)
    return identifiers, phrases


def exact_match_ids(
    conn: sqlite3.Connection,
    candidate_texts: Mapping[int, str],
    identifiers: list[str],
    phrases: list[str],
) -> set[int]:
    """Chunk ids (within candidates) containing every identifier and phrase."""
    if not identifiers and not phrases:
        return set()
    matched = set(candidate_texts)
    for term in identifiers:
        rows = conn.execute("SELECT chunk_id FROM postings WHERE term = ?", (term,)).fetchall()
        matched &= {int(row[0]) for row in rows}
        if not matched:
            return set()
    if phrases:
        matched = {
            chunk_id
            for chunk_id in matched
            if all(phrase in candidate_texts[chunk_id].casefold() for phrase in phrases)
        }
    return matched


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


def _segment_start(segment: dict[str, Any]) -> float | None:
    try:
        return float(segment.get("start_s", 0.0))
    except (TypeError, ValueError):
        return None


def _is_boundary(previous: dict[str, Any], following: dict[str, Any]) -> bool:
    """A natural chunk boundary: sentence end, or a speech pause between segments."""
    if _SENTENCE_END_RE.search(str(previous.get("text") or "").strip()):
        return True
    previous_end = _segment_end(previous)
    next_start = _segment_start(following)
    return (
        previous_end is not None
        and next_start is not None
        and next_start - previous_end >= PAUSE_BOUNDARY_S
    )


def chunk_segments(
    segments: list[dict[str, Any]],
    *,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    boundary_aware: bool = True,
) -> list[dict[str, Any]]:
    """Pack timed segments into reproducible timestamp-preserving chunks.

    Chunks never split a segment, so `start_s`/`end_s` are real caption/STT
    timings and `segment_start:segment_end` points back into the exact source
    transcript revision. With `boundary_aware` (the `timestamp-boundary-v1`
    chunker) a chunk that is at least CHUNK_MIN_FILL of its budget closes at the
    next sentence end or speech pause instead of running to the token limit.
    """
    _validate_chunk_budget(chunk_tokens, chunk_overlap)
    min_fill = max(1, int(chunk_tokens * CHUNK_MIN_FILL))
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
            if (
                boundary_aware
                and used >= min_fill
                and j < len(segments)
                and _is_boundary(segments[j - 1], segments[j])
            ):
                break

        if not buf:
            break

        parts = [str(seg.get("text") or "").strip() for seg in buf]
        text = " ".join(parts).strip()
        # Character offset of each source segment inside `text`, with its real
        # timing, so a hit can cite the exact matching segment (its anchor).
        segment_times: list[list[Any]] = []
        offset = 0
        for seg, part in zip(buf, parts):
            segment_times.append([offset, _segment_start(seg), _segment_end(seg)])
            offset += len(part) + 1
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
                "segment_times": segment_times,
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
            schema_version=int(meta.get("schema_version", "1")),
            chunker=meta.get("chunker") or LEGACY_CHUNKER_ID,
            indexed_duration_s=(
                float(meta["indexed_duration_s"]) if meta.get("indexed_duration_s") else None
            ),
            corpus_manifest_sha256=meta.get("corpus_manifest_sha256") or None,
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
    boundary_aware: bool = True,
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
                boundary_aware=boundary_aware,
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

        warnings = json.loads(row.get("warnings_json") or "[]")
        ends = [end for end in (_segment_end(seg) for seg in segments if isinstance(seg, dict)) if end is not None]
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
            "validation_status": validation_status(bool(row.get("validated")), warnings),
            "transcript_fetched_at": row.get("fetched_at"),
            "warnings": warnings,
            "acquisition": acquisition_record(row),
            "duration_s": max(ends) if ends else None,
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
    embedding_dim INTEGER,
    segment_start INTEGER,
    segment_end INTEGER,
    segment_times_json TEXT
);
CREATE INDEX idx_chunks_video ON chunks(video_id, chunk_index);
CREATE TABLE sources (
    transcript_revision_id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL,
    transcript_sha256 TEXT NOT NULL,
    duration_s REAL,
    validation_status TEXT NOT NULL,
    acquisition_json TEXT
);
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
    # Seam for immutable corpus revisions (#3): a revision file that carries a
    # manifest digest has it echoed into index metadata and search results.
    manifest_sha = corpus.get("manifest_sha256")
    manifest_sha = manifest_sha if isinstance(manifest_sha, str) else None
    sources, chunks, missing = _build_source_chunks(
        corpus,
        requested_lang=requested_lang,
        chunk_tokens=chunk_tokens,
        chunk_overlap=chunk_overlap,
    )

    identity = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "chunker": CHUNKER_ID,
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
                "validated, transcript_fetched_at, warnings_json, embedding, embedding_dim, "
                "segment_start, segment_end, segment_times_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
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
                    chunk.get("segment_start"),
                    chunk.get("segment_end"),
                    (
                        json.dumps(chunk["segment_times"], separators=(",", ":"))
                        if chunk.get("segment_times")
                        else None
                    ),
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
        conn.executemany(
            "INSERT INTO sources (transcript_revision_id, video_id, transcript_sha256, "
            "duration_s, validation_status, acquisition_json) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    int(source["transcript_revision_id"]),
                    source["video_id"],
                    source["transcript_sha256"],
                    source["duration_s"],
                    source["validation_status"],
                    _canonical_json(source["acquisition"])
                    if source["acquisition"] is not None
                    else None,
                )
                for source in sources
            ],
        )
        durations = [s["duration_s"] for s in sources if s["duration_s"] is not None]

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
            "chunker": CHUNKER_ID,
            "corpus_manifest_sha256": manifest_sha or "",
            "indexed_duration_s": round(sum(durations), 3),
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


def load_sources(conn: sqlite3.Connection) -> dict[int, dict[str, Any]] | None:
    """Per-transcript-revision provenance frozen into a schema-2 index.

    Returns `None` for schema-1 indexes, which predate the sources table.
    """
    try:
        rows = conn.execute(
            "SELECT transcript_revision_id, duration_s, validation_status, acquisition_json "
            "FROM sources"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    out: dict[int, dict[str, Any]] = {}
    for revision_id, duration_s, status, acquisition_json in rows:
        try:
            acquisition = json.loads(acquisition_json) if acquisition_json else None
        except json.JSONDecodeError:
            acquisition = None
        out[int(revision_id)] = {
            "duration_s": duration_s,
            "validation_status": status,
            "acquisition": acquisition,
        }
    return out


def source_method(sources: Mapping[int, Mapping[str, Any]] | None, revision_id: int) -> str | None:
    if sources is None:
        return None
    acquisition = (sources.get(int(revision_id)) or {}).get("acquisition") or {}
    return acquisition.get("method")


def require_filter_support(
    filters: SearchFilters, sources: Mapping[int, Any] | None, index_revision: str
) -> None:
    if filters.needs_acquisition and sources is None:
        raise CorpusIndexError(
            f"index {index_revision} predates acquisition provenance (schema 1); "
            "filters.acquisition_methods needs an index prepared by this version"
        )


def fetch_rows(
    conn: sqlite3.Connection, ids: Iterable[int], columns: str = "*"
) -> dict[int, sqlite3.Row]:
    """Fetch chunk rows by id in bounded batches (never the whole corpus)."""
    wanted = sorted({int(i) for i in ids})
    out: dict[int, sqlite3.Row] = {}
    for offset in range(0, len(wanted), 500):
        batch = wanted[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT {columns} FROM chunks WHERE id IN ({placeholders})", batch
        ).fetchall():
            out[int(row["id"])] = row
    return out


# Columns needed to filter and deterministically order chunks; excludes text and
# embeddings so a 100+ hour corpus can be scanned without loading transcripts.
LIGHT_COLUMNS = (
    "id, video_id, channel, chunk_index, start_s, end_s, validated, "
    "is_generated, transcript_revision_id"
)
_HIT_COLUMNS = (
    "id, video_id, title, channel, chunk_index, start_s, end_s, text, chunk_sha256, "
    "transcript_revision_id, transcript_sha256, requested_lang, actual_lang, "
    "is_generated, validated, transcript_fetched_at, warnings_json"
)


def hit_columns(conn: sqlite3.Connection) -> str:
    names = {row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    columns = _HIT_COLUMNS
    if "segment_start" in names:
        columns += ", segment_start, segment_end"
    if "segment_times_json" in names:
        columns += ", segment_times_json"
    return columns


def evidence_anchor(row: sqlite3.Row, query: str) -> dict[str, Any] | None:
    """The source segment inside a chunk that best matches the query.

    Scores each segment by exact identifiers/phrases (weighted) and distinct
    query terms; ties go to the earliest segment. Falls back to the chunk start
    (`basis: "chunk"`) for semantic-only matches. Returns None for indexes that
    predate per-segment timings.
    """
    if "segment_times_json" not in set(row.keys()) or not row["segment_times_json"]:
        return None
    try:
        times = json.loads(row["segment_times_json"])
    except (TypeError, ValueError):
        return None
    if not times:
        return None
    text = str(row["text"])
    identifiers, phrases = exact_terms(query)
    query_terms = {t for t in tokenize(query) if len(t) >= 2}
    best: tuple[float, int] = (0.0, 0)
    basis = "chunk"
    for index, item in enumerate(times):
        start = int(item[0])
        stop = int(times[index + 1][0]) if index + 1 < len(times) else len(text)
        piece = text[start:stop]
        piece_terms = set(tokenize(piece))
        exact = sum(1 for term in identifiers if term in piece_terms)
        folded = piece.casefold()
        exact += sum(1 for phrase in phrases if phrase in folded)
        score = 10.0 * exact + len(query_terms & piece_terms)
        if score > best[0]:
            best = (score, index)
            basis = "exact" if exact else "lexical"
    _offset, start_s, end_s = times[best[1]]
    return {
        "start_s": start_s,
        "end_s": end_s,
        "url": canonical_url(row["video_id"], start_s),
        "segment": (row["segment_start"] + best[1]) if row["segment_start"] is not None else None,
        "basis": basis,
    }


def canonical_url(video_id: str, start_s: Any) -> str:
    if start_s is None:
        return f"https://www.youtube.com/watch?v={video_id}"
    return f"https://www.youtube.com/watch?v={video_id}&t={max(0, int(float(start_s)))}s"


def _row_to_hit(
    row: sqlite3.Row,
    *,
    lexical_raw: float,
    lexical_norm: float,
    prepared: PreparedIndex | None = None,
    sources: Mapping[int, Mapping[str, Any]] | None = None,
    exact_match: bool | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    start_s = row["start_s"]
    raw_generated = row["is_generated"]
    keys = set(row.keys())
    warnings = json.loads(row["warnings_json"] or "[]")
    source = (sources or {}).get(int(row["transcript_revision_id"])) if sources is not None else None
    chunk: dict[str, Any] = {
        "index": row["chunk_index"],
        "sha256": row["chunk_sha256"],
    }
    if "segment_start" in keys and row["segment_start"] is not None:
        chunk["segment_range"] = [row["segment_start"], row["segment_end"]]
    hit: dict[str, Any] = {
        "evidence_id": f"ev_{row['chunk_sha256'][:20]}",
        "video_id": row["video_id"],
        "title": row["title"],
        "channel": row["channel"],
        "url": canonical_url(row["video_id"], start_s),
        "start_s": start_s,
        "end_s": row["end_s"],
        "excerpt": row["text"],
        "scores": {
            "lexical": lexical_norm,
            "lexical_raw": lexical_raw,
            "semantic": None,
            "hybrid": lexical_norm,
            "exact_match": exact_match,
        },
        "chunk": chunk,
        "transcript": {
            "revision_id": row["transcript_revision_id"],
            "sha256": row["transcript_sha256"],
            "requested_lang": row["requested_lang"],
            "actual_lang": row["actual_lang"],
            "is_generated": (
                None if raw_generated is None else bool(raw_generated)
            ),
            "validated": bool(row["validated"]),
            "validation_status": (
                source["validation_status"]
                if source is not None
                else validation_status(bool(row["validated"]), warnings)
            ),
            "fetched_at": row["transcript_fetched_at"],
            "warnings": warnings,
            # None when the index predates frozen provenance (schema 1) or the
            # transcript revision itself predates acquisition recording.
            "acquisition": source["acquisition"] if source is not None else None,
        },
    }
    if query is not None:
        anchor = evidence_anchor(row, query)
        if anchor is not None:
            hit["anchor"] = anchor
    if prepared is not None:
        hit["corpus_revision"] = prepared.corpus_revision
        hit["index_revision"] = prepared.index_revision
    return hit


def retrieval_record(
    prepared: PreparedIndex,
    *,
    top_k: int,
    filters: SearchFilters,
    meta: Mapping[str, str],
) -> dict[str, Any]:
    """Search provenance (EVIDENCE_MODEL): enough to reproduce the ranking."""
    record = {
        "class": prepared.backend,
        "index_schema_version": prepared.schema_version,
        "chunker": prepared.chunker,
        "lexical_scorer": "bm25(k1=1.2,b=0.75)+phrase",
        "model_id": prepared.model_id,
        "top_k": top_k,
        "filters": filters.as_dict(),
    }
    if prepared.backend == "hybrid":
        record["fusion"] = meta.get("fusion") or "rrf-v1"
        record["vector_search"] = meta.get("vector_search") or "exact-scan-v1"
    return record


def _empty_result(
    handle: str, prepared: PreparedIndex, query: str, record: dict[str, Any]
) -> dict[str, Any]:
    return {
        "corpus_revision": handle,
        "corpus_manifest_sha256": prepared.corpus_manifest_sha256,
        "index_revision": prepared.index_revision,
        "backend": prepared.backend,
        "model_id": prepared.model_id,
        "query": query,
        "retrieval": record,
        "coverage": prepared.as_dict(),
        "hits": [],
    }


def search_index(
    handle: str,
    query: str,
    *,
    top_k: int = 10,
    index_revision: str | None = None,
    requested_lang: str = "en",
    validated_only: bool = False,
    filters: SearchFilters | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Search one immutable index revision using a BM25-style lexical scorer."""
    query = query.strip()
    if not query:
        raise CorpusIndexError("query must not be empty")
    if top_k < 1 or top_k > MAX_TOP_K:
        raise CorpusIndexError(f"top_k must be between 1 and {MAX_TOP_K}")
    if not isinstance(filters, SearchFilters):
        filters = SearchFilters.parse(filters, validated_only=validated_only)
    elif validated_only and not filters.validated_only:
        filters = dataclasses.replace(filters, validated_only=True)

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
    record = retrieval_record(prepared, top_k=top_k, filters=filters, meta=meta)
    try:
        total_chunks = int(meta.get("chunk_count", "0"))
        avg_doc_len = float(meta.get("avg_doc_len", "0"))
    except ValueError as exc:
        raise CorpusIndexError("index metadata contains invalid corpus statistics") from exc

    if total_chunks <= 0:
        return _empty_result(handle, prepared, query, record)

    scores: defaultdict[int, float] = defaultdict(float)
    k1 = 1.2
    b = 0.75
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        sources = load_sources(conn)
        require_filter_support(filters, sources, prepared.index_revision)
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
            return _empty_result(handle, prepared, query, record)

        by_id = fetch_rows(conn, scores.keys(), hit_columns(conn))

        query_lower = query.casefold()
        ranked: list[tuple[float, int]] = []
        for chunk_id, score in scores.items():
            row = by_id.get(chunk_id)
            if row is None:
                continue
            if not filters.matches(
                row, source_method(sources, row["transcript_revision_id"])
            ):
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
        identifiers, phrases = exact_terms(query)
        exact_ids = exact_match_ids(
            conn,
            {chunk_id: str(by_id[chunk_id]["text"]) for _score, chunk_id in ranked},
            identifiers,
            phrases,
        )
        has_exact = bool(identifiers or phrases)
        max_score = ranked[0][0] if ranked else 1.0
        hits = [
            {
                "rank": rank,
                **_row_to_hit(
                    by_id[chunk_id],
                    lexical_raw=score,
                    lexical_norm=(score / max_score if max_score else 0.0),
                    prepared=prepared,
                    sources=sources,
                    exact_match=(chunk_id in exact_ids) if has_exact else None,
                    query=query,
                ),
            }
            for rank, (score, chunk_id) in enumerate(ranked, start=1)
        ]
    finally:
        conn.close()

    return {
        "corpus_revision": handle,
        "corpus_manifest_sha256": prepared.corpus_manifest_sha256,
        "index_revision": prepared.index_revision,
        "backend": prepared.backend,
        "model_id": prepared.model_id,
        "query": query,
        "retrieval": record,
        "coverage": prepared.as_dict(),
        "hits": hits,
    }
