"""Versioned SQLite cache. Append-only — new fetches insert new rows, never overwrite.

Cache lives at ~/.cache/youtube-mcp/v2.sqlite (XDG-canonical).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

CACHE_DIR = Path.home() / ".cache" / "youtube-mcp"
CACHE_PATH = CACHE_DIR / "v2.sqlite"

TTL_TRANSCRIPTS = timedelta(days=30)
TTL_VIDEO_META = timedelta(days=7)
TTL_SEARCH = timedelta(hours=1)


SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    lang TEXT NOT NULL,
    text TEXT NOT NULL,
    segments_json TEXT,
    word_count INTEGER,
    fetched_at TEXT NOT NULL,
    validated INTEGER NOT NULL,
    warnings_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_transcripts_lookup
    ON transcripts(video_id, lang, fetched_at DESC);

CREATE TABLE IF NOT EXISTS video_meta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_meta_lookup
    ON video_meta(video_id, fetched_at DESC);

CREATE TABLE IF NOT EXISTS search_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_lookup
    ON search_results(query, fetched_at DESC);
"""


def _ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    _ensure_cache_dir()
    conn = sqlite3.connect(CACHE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _is_fresh(fetched_at: str, ttl: timedelta) -> tuple[bool, int]:
    """Return (is_fresh, age_in_seconds)."""
    age = datetime.now(timezone.utc) - _parse_iso(fetched_at)
    return age < ttl, int(age.total_seconds())


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


def put_transcript(
    video_id: str,
    lang: str,
    text: str,
    segments: list[dict[str, Any]] | None,
    word_count: int,
    validated: bool,
    warnings: list[str],
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO transcripts "
            "(video_id, lang, text, segments_json, word_count, fetched_at, validated, warnings_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                video_id,
                lang,
                text,
                json.dumps(segments) if segments is not None else None,
                word_count,
                _now_iso(),
                1 if validated else 0,
                json.dumps(warnings),
            ),
        )


def get_transcript(
    video_id: str,
    lang: str,
    *,
    fresh_only: bool = True,
) -> tuple[dict[str, Any], int] | None:
    """Return (row_dict, age_seconds) for the most recent cached transcript, or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM transcripts WHERE video_id = ? AND lang = ? "
            "ORDER BY fetched_at DESC LIMIT 1",
            (video_id, lang),
        ).fetchone()
    if row is None:
        return None
    fresh, age = _is_fresh(row["fetched_at"], TTL_TRANSCRIPTS)
    if fresh_only and not fresh:
        return None
    return dict(row), age


# ---------------------------------------------------------------------------
# Video meta
# ---------------------------------------------------------------------------


def put_video_meta(video_id: str, payload: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO video_meta (video_id, payload_json, fetched_at) VALUES (?, ?, ?)",
            (video_id, json.dumps(payload), _now_iso()),
        )


def get_video_meta(
    video_id: str, *, fresh_only: bool = True
) -> tuple[dict[str, Any], int] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT payload_json, fetched_at FROM video_meta WHERE video_id = ? "
            "ORDER BY fetched_at DESC LIMIT 1",
            (video_id,),
        ).fetchone()
    if row is None:
        return None
    fresh, age = _is_fresh(row["fetched_at"], TTL_VIDEO_META)
    if fresh_only and not fresh:
        return None
    return json.loads(row["payload_json"]), age
