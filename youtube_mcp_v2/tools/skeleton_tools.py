"""skeleton.* — legacy compatibility wrappers over corpus revisions.

Once built, a skeleton is read-only. Re-running build creates a new handle. Old
handles remain queryable forever (revision discipline).

Since v0.3 the canonical tools are `corpus.build/get/list/index/diff`
(`tools/corpus_revisions.py`). `skeleton.build` now writes the same immutable,
manifest-bearing corpus revision and keeps its v0.2 response shape; the
`skeleton.*` MCP tools are only registered when
YOUTUBE_MCP_LEGACY_SKELETON_TOOLS=1.
"""

from __future__ import annotations

from typing import Any, Literal

from .. import envelope, isolation, skeleton as _skeleton


def _build_channel(value: str, limit: int) -> tuple[dict[str, Any], str]:
    """Channel skeleton builder. Returns (raw_data, source).

    Auto-upgrades to tier-2 Data API v3 (full enumeration) when YOUTUBE_API_KEY
    is set; otherwise scrapes the /videos tab (falling back to yt-dlp). The
    revision's `source` and manifest attempts reflect which path ran.
    """
    from .corpus_revisions import acquire_channel

    raw, source, _attempts = acquire_channel(value, limit)
    return raw, source


def _build_topic(value: str, limit: int) -> dict[str, Any]:
    """Tier-1: scrape search results, reshape into skeleton-video schema."""
    results = isolation.run_isolated(
        "youtube_mcp_v2.adapters.search_scrape.search_videos",
        value,
        limit,
        timeout_s=20,
    )
    videos = []
    for r in results:
        videos.append({
            "id": r["id"],
            "title": r["title"],
            "channel": r.get("channel", ""),
            "channel_id": r.get("channel_id", ""),
            "duration_s": None,
            "duration_text": r.get("duration", ""),
            "published": r.get("published", ""),
            "view_count": r.get("views", ""),
            "thumbnail_url": f"https://i.ytimg.com/vi/{r['id']}/hqdefault.jpg",
            "has_transcript": None,
            "caption_track_id": None,
            "lang": None,
        })
    return {
        "channel": None,
        "videos": videos,
    }


def skeleton_build(
    target: Literal["channel", "topic"],
    value: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Build a frozen reference of videos for a channel or topic."""
    from .corpus_revisions import build_revision

    if target not in ("channel", "topic"):
        return envelope.fail(
            "bad_target",
            f"target must be 'channel' or 'topic', got {target!r}",
            recoverable=False,
        )
    if not value or not value.strip():
        return envelope.fail("bad_value", "value is empty", recoverable=False)

    payload, warnings, failure = build_revision(target, value, limit=limit)
    if failure is not None:
        return failure
    return envelope.ok(
        {
            "handle": payload["handle"],
            "target": target,
            "value": payload["value"],
            "source": payload["source"],
            "count": len(payload["videos"]),
        },
        source=payload["source"],
        warnings=warnings,
    )


def skeleton_list(handle: str, enrich: bool = True) -> dict[str, Any]:
    """List the videos in a skeleton."""
    try:
        data = _skeleton.load_skeleton(handle)
    except FileNotFoundError as e:
        return envelope.fail("skeleton_not_found", str(e), recoverable=False)
    except ValueError as e:
        return envelope.fail("bad_handle", str(e), recoverable=False)

    videos = data.get("videos", [])
    if enrich:
        videos = _skeleton.enrich_videos_with_meta(videos)
    return envelope.ok(videos, source="cache")


def skeleton_get(handle: str) -> dict[str, Any]:
    """Load the full frozen skeleton snapshot."""
    try:
        data = _skeleton.load_skeleton(handle)
    except FileNotFoundError as e:
        return envelope.fail("skeleton_not_found", str(e), recoverable=False)
    except ValueError as e:
        return envelope.fail("bad_handle", str(e), recoverable=False)
    return envelope.ok({**data, "expired_at": _skeleton.expired_at(handle, data)}, source="cache")


def skeleton_diff(base_handle: str, head_handle: str) -> dict[str, Any]:
    """Diff two frozen revisions of the same channel/topic scope.

    USE WHEN: you built the same channel/topic at two points in time and need to
              know what was added, removed, or changed without consulting live
              YouTube state.
    DO NOT USE WHEN: comparing unrelated channels/topics; build comparable
                     revisions first.
    OUTPUT SHAPE: envelope wrapping handles/scope, counts, added[], removed[],
                  changed[] and source-change provenance.
    """
    try:
        result = _skeleton.diff_skeletons(base_handle, head_handle)
    except FileNotFoundError as e:
        return envelope.fail("skeleton_not_found", str(e), recoverable=False)
    except _skeleton.SkeletonScopeMismatch as e:
        return envelope.fail("scope_mismatch", str(e), recoverable=False)
    except _skeleton.SkeletonDiffError as e:
        return envelope.fail("skeleton_diff_invalid", str(e), recoverable=False)
    except ValueError as e:
        return envelope.fail("bad_handle", str(e), recoverable=False)
    return envelope.ok(result, source="cache")


def skeleton_expire(handle: str) -> dict[str, Any]:
    """Mark a skeleton stale. Does NOT delete or alter captured membership."""
    try:
        data = _skeleton.expire_skeleton(handle)
    except FileNotFoundError as e:
        return envelope.fail("skeleton_not_found", str(e), recoverable=False)
    except ValueError as e:
        return envelope.fail("bad_handle", str(e), recoverable=False)
    return envelope.ok(
        {"handle": data["handle"], "expired_at": data["expired_at"]},
        source="cache",
    )


def skeleton_index(target: str | None = None) -> dict[str, Any]:
    """List all skeletons on disk (summary view)."""
    summaries = _skeleton.list_skeletons(target=target)
    return envelope.ok(summaries, source="cache")
