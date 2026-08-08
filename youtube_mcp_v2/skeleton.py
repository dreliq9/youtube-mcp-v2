"""Skeleton handles — frozen reference objects for multi-step research.

A skeleton is a read-only snapshot of a research set at a point in time. It may
come from one channel, one topic search, or an explicitly composed heterogeneous
collection. Re-building or re-composing creates a new handle; old handles remain
queryable forever (revision discipline).
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
    r"^(chan|topic|set)-[A-Za-z0-9_.@-]+-\d{8}-\d{6}(?:-\d{6})?$"
)


class SkeletonDiffError(ValueError):
    """Raised when two frozen snapshots cannot be compared safely."""


class SkeletonScopeMismatch(SkeletonDiffError):
    """Raised when a caller tries to diff unrelated channel/topic scopes."""


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


def make_handle(
    target: Literal["channel", "topic", "collection"], value: str
) -> str:
    if target == "channel":
        prefix = "chan"
        body = value.strip()
    elif target == "topic":
        prefix = "topic"
        body = _slug(value.strip())
    elif target == "collection":
        prefix = "set"
        body = _slug(value.strip())
    else:  # defensive guard for non-typed callers
        raise ValueError(f"unsupported skeleton target: {target!r}")
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
