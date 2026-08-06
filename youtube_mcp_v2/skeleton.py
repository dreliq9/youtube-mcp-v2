"""Skeleton handles — frozen reference objects for multi-step research.

A skeleton is a snapshot of the videos for a channel or topic at a point in time.
Once built, it is read-only. Re-building creates a new handle. Old handles remain
queryable forever (CAiD revision discipline).

Storage layout (XDG-canonical):
    ~/.cache/youtube-mcp/
    ├── v2.sqlite                     # transcripts, video_meta, search cache
    ├── skeletons/
    │   └── <handle>.json             # this file
    ├── vectors/                      # RESERVED for v0.3 ML vector indices
    └── models/                       # RESERVED for v0.3 ML model weights

Per-video schema includes nullable `has_transcript`, `caption_track_id`, `lang`
fields that v0.3 `ml.embed_transcripts` will populate alongside vectors. Do not
drop these fields.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

CACHE_DIR = Path.home() / ".cache" / "youtube-mcp"
SKELETON_DIR = CACHE_DIR / "skeletons"
VECTOR_DIR = CACHE_DIR / "vectors"      # reserved
MODEL_DIR = CACHE_DIR / "models"        # reserved


_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Accept both legacy second-resolution handles and v0.2.1 microsecond handles.
_HANDLE_RE = re.compile(
    r"^(chan|topic)-[A-Za-z0-9_.@-]+-\d{8}-\d{6}(?:-\d{6})?$"
)


class SkeletonDiffError(ValueError):
    """Raised when two frozen snapshots cannot be compared safely."""


class SkeletonScopeMismatch(SkeletonDiffError):
    """Raised when a caller tries to diff unrelated channel/topic scopes."""


def _ensure_dirs() -> None:
    for d in (SKELETON_DIR, VECTOR_DIR, MODEL_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_handle_stamp() -> str:
    # Seconds were not sufficient: two builds of the same target inside one
    # second could collide and write the same path. Preserve the readable time
    # component but add microseconds. Legacy handles remain accepted by regex.
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _slug(value: str, max_len: int = 40) -> str:
    s = _SLUG_RE.sub("-", value.lower()).strip("-")
    return s[:max_len] or "unknown"


def make_handle(target: Literal["channel", "topic"], value: str) -> str:
    prefix = "chan" if target == "channel" else "topic"
    base = value.strip()
    # For channel ids/handles, keep them mostly as-is for greppability;
    # for topic queries, slugify.
    body = base if target == "channel" else _slug(base)
    return f"{prefix}-{body}-{_now_handle_stamp()}"


def is_valid_handle(handle: str) -> bool:
    return bool(_HANDLE_RE.match(handle))


def skeleton_path(handle: str) -> Path:
    if not is_valid_handle(handle):
        # Safety: don't write to arbitrary paths from a malformed handle.
        raise ValueError(f"invalid skeleton handle: {handle!r}")
    return SKELETON_DIR / f"{handle}.json"


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def save_skeleton(payload: dict[str, Any]) -> Path:
    """Create a new frozen skeleton file and refuse to overwrite any existing one."""
    _ensure_dirs()
    handle = payload["handle"]
    path = skeleton_path(handle)
    encoded = json.dumps(payload, indent=2)
    # Exclusive creation is the final guardrail even if handle generation ever
    # regresses or two processes somehow generate an identical handle.
    with path.open("x", encoding="utf-8") as f:
        f.write(encoded)
    return path


def load_skeleton(handle: str) -> dict[str, Any]:
    path = skeleton_path(handle)
    if not path.exists():
        raise FileNotFoundError(f"no skeleton at {path}")
    return json.loads(path.read_text())


def list_skeletons(target: str | None = None) -> list[dict[str, Any]]:
    _ensure_dirs()
    summaries: list[dict[str, Any]] = []
    for path in sorted(SKELETON_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if target and data.get("target") != target:
            continue
        summaries.append({
            "handle": data.get("handle"),
            "target": data.get("target"),
            "value": data.get("value"),
            "built_at": data.get("built_at"),
            "expired_at": data.get("expired_at"),
            "video_count": len(data.get("videos", [])),
            "source": data.get("source"),
        })
    return summaries


def expire_skeleton(handle: str) -> dict[str, Any]:
    """Mark a skeleton stale. This is the one intentional in-place metadata mutation."""
    data = load_skeleton(handle)
    if data.get("expired_at"):
        return data
    data["expired_at"] = _now_iso()
    # `save_skeleton` is intentionally exclusive. Expiration is the documented
    # exception: it mutates only the stale marker while preserving the captured
    # membership. Write directly to the already-resolved skeleton path.
    skeleton_path(handle).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


# ---------------------------------------------------------------------------
# Revision diff
# ---------------------------------------------------------------------------


def _index_videos(videos: list[dict[str, Any]], *, label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for i, video in enumerate(videos):
        video_id = video.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise SkeletonDiffError(f"{label} video at index {i} has no usable id")
        if video_id in indexed:
            raise SkeletonDiffError(f"{label} contains duplicate video id {video_id}")
        indexed[video_id] = video
    return indexed


def diff_skeletons(base_handle: str, head_handle: str) -> dict[str, Any]:
    """Compare two immutable snapshots of the same channel/topic scope.

    The comparison operates only on the captured skeleton payloads — never on
    current cache enrichment — so a diff remains reproducible later.
    """
    base = load_skeleton(base_handle)
    head = load_skeleton(head_handle)

    base_scope = (base.get("target"), base.get("value"))
    head_scope = (head.get("target"), head.get("value"))
    if base_scope != head_scope:
        raise SkeletonScopeMismatch(
            "skeleton scopes differ: "
            f"base={base_scope!r}, head={head_scope!r}"
        )

    base_by_id = _index_videos(base.get("videos", []), label="base")
    head_by_id = _index_videos(head.get("videos", []), label="head")

    base_ids = set(base_by_id)
    head_ids = set(head_by_id)
    added_ids = sorted(head_ids - base_ids)
    removed_ids = sorted(base_ids - head_ids)
    shared_ids = sorted(base_ids & head_ids)

    changed: list[dict[str, Any]] = []
    unchanged = 0
    for video_id in shared_ids:
        before = base_by_id[video_id]
        after = head_by_id[video_id]
        changed_fields = sorted(
            field
            for field in (set(before) | set(after))
            if before.get(field) != after.get(field)
        )
        if changed_fields:
            changed.append({
                "id": video_id,
                "changed_fields": changed_fields,
                "before": before,
                "after": after,
            })
        else:
            unchanged += 1

    return {
        "base_handle": base_handle,
        "head_handle": head_handle,
        "target": base.get("target"),
        "value": base.get("value"),
        "base_built_at": base.get("built_at"),
        "head_built_at": head.get("built_at"),
        "base_source": base.get("source"),
        "head_source": head.get("source"),
        "source_changed": base.get("source") != head.get("source"),
        "counts": {
            "base": len(base_by_id),
            "head": len(head_by_id),
            "added": len(added_ids),
            "removed": len(removed_ids),
            "changed": len(changed),
            "unchanged": unchanged,
        },
        "added": [head_by_id[video_id] for video_id in added_ids],
        "removed": [base_by_id[video_id] for video_id in removed_ids],
        "changed": changed,
    }


# ---------------------------------------------------------------------------
# Enrichment join — pulls cached metadata from the SQLite cache so skeleton.list
# can return a "current view" without modifying the frozen skeleton itself.
# This is how the v0.3 vector index will plug in: a separate table joined at
# read time, never mutating the skeleton.
# ---------------------------------------------------------------------------


def enrich_videos_with_meta(
    videos: list[dict[str, Any]],
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """For each video, look up the most recent video_meta cache row and merge
    selected enrichment fields. Never modifies the source list.
    """
    from . import cache as _cache  # local to avoid circular import at module load

    db = db_path or _cache.CACHE_PATH
    if not db.exists():
        return list(videos)

    out: list[dict[str, Any]] = []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        for v in videos:
            vid = v.get("id")
            if not vid:
                out.append(dict(v))
                continue
            row = conn.execute(
                "SELECT payload_json FROM video_meta "
                "WHERE video_id = ? ORDER BY fetched_at DESC LIMIT 1",
                (vid,),
            ).fetchone()
            if not row:
                out.append(dict(v))
                continue
            meta = json.loads(row["payload_json"])
            merged = dict(v)
            # Only fill nulls in the skeleton — never overwrite skeleton-time data.
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
