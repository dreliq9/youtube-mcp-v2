"""Skeleton handles — frozen reference objects for multi-step research.

A skeleton is a snapshot of the videos for a channel or topic at a point in time.
Once built, it is read-only. Re-building creates a new handle. Old handles remain
queryable forever (CAiD revision discipline).
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .paths import CACHE_DIR as _DEFAULT_CACHE_DIR
from .paths import MODEL_DIR as _DEFAULT_MODEL_DIR
from .paths import SKELETON_DIR as _DEFAULT_SKELETON_DIR
from .paths import VECTOR_DIR as _DEFAULT_VECTOR_DIR

# Retain module aliases for existing tests/monkeypatch consumers.
CACHE_DIR = _DEFAULT_CACHE_DIR
SKELETON_DIR = _DEFAULT_SKELETON_DIR
VECTOR_DIR = _DEFAULT_VECTOR_DIR
MODEL_DIR = _DEFAULT_MODEL_DIR


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_HANDLE_RE = re.compile(
    r"^(chan|topic)-[A-Za-z0-9_.@-]+-\d{8}-\d{6}(?:-\d{6})?$"
)


def _ensure_dirs() -> None:
    for directory in (SKELETON_DIR, VECTOR_DIR, MODEL_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_handle_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _slug(value: str, max_len: int = 40) -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    return slug[:max_len] or "unknown"


def make_handle(target: Literal["channel", "topic"], value: str) -> str:
    prefix = "chan" if target == "channel" else "topic"
    base = value.strip()
    body = base if target == "channel" else _slug(base)
    return f"{prefix}-{body}-{_now_handle_stamp()}"


def is_valid_handle(handle: str) -> bool:
    return bool(_HANDLE_RE.match(handle))


def skeleton_path(handle: str) -> Path:
    if not is_valid_handle(handle):
        raise ValueError(f"invalid skeleton handle: {handle!r}")
    return SKELETON_DIR / f"{handle}.json"


def save_skeleton(payload: dict[str, Any]) -> Path:
    """Create a new frozen skeleton file and refuse to overwrite an existing one."""
    _ensure_dirs()
    handle = payload["handle"]
    path = skeleton_path(handle)
    encoded = json.dumps(payload, indent=2)
    with path.open("x", encoding="utf-8") as file:
        file.write(encoded)
    return path


def load_skeleton(handle: str) -> dict[str, Any]:
    path = skeleton_path(handle)
    if not path.exists():
        raise FileNotFoundError(f"no skeleton at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_skeletons(target: str | None = None) -> list[dict[str, Any]]:
    _ensure_dirs()
    summaries: list[dict[str, Any]] = []
    for path in sorted(SKELETON_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if target and data.get("target") != target:
            continue
        summaries.append(
            {
                "handle": data.get("handle"),
                "target": data.get("target"),
                "value": data.get("value"),
                "built_at": data.get("built_at"),
                "expired_at": data.get("expired_at"),
                "video_count": len(data.get("videos", [])),
                "source": data.get("source"),
            }
        )
    return summaries


def expire_skeleton(handle: str) -> dict[str, Any]:
    """Mark a skeleton stale; membership remains frozen."""
    data = load_skeleton(handle)
    if data.get("expired_at"):
        return data
    data["expired_at"] = _now_iso()
    skeleton_path(handle).write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    return data


def enrich_videos_with_meta(
    videos: list[dict[str, Any]],
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Merge current cached metadata at read time without mutating the snapshot."""
    from . import cache as _cache

    db = db_path or _cache.CACHE_PATH
    if not db.exists():
        return list(videos)

    out: list[dict[str, Any]] = []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        for video in videos:
            video_id = video.get("id")
            if not video_id:
                out.append(dict(video))
                continue
            row = conn.execute(
                "SELECT payload_json FROM video_meta "
                "WHERE video_id = ? ORDER BY fetched_at DESC LIMIT 1",
                (video_id,),
            ).fetchone()
            if not row:
                out.append(dict(video))
                continue
            meta = json.loads(row["payload_json"])
            merged = dict(video)
            for field in (
                "has_transcript",
                "lang_default",
                "available_caption_langs",
                "duration_s",
                "view_count",
            ):
                if merged.get(field) in (None, [], "") and field in meta:
                    merged[field] = meta[field]
            out.append(merged)
    finally:
        conn.close()
    return out
