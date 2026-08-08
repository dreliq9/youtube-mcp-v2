"""skeleton.* — frozen reference objects for multi-step research.

Once built, a skeleton is read-only. Re-running build creates a new handle. Old
handles remain queryable forever (revision discipline). Future corpus/ML tools
attach enrichment alongside existing videoIds without modifying the snapshot.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from .. import envelope, isolation, skeleton as _skeleton


def _build_channel(value: str, limit: int) -> tuple[dict[str, Any], str]:
    """Channel skeleton builder. Returns (raw_data, source).

    Auto-upgrades to tier-2 Data API v3 (full enumeration) when YOUTUBE_API_KEY
    is set; otherwise scrapes the /videos tab. The on-disk skeleton's `source`
    field reflects which path ran so future comparisons preserve provenance.
    """
    if os.environ.get("YOUTUBE_API_KEY"):
        from ..adapters import data_api
        try:
            raw = data_api.channel_uploads(value, limit=limit)
            return raw, "api"
        except (data_api.ApiAuthError, data_api.ApiQuotaError, data_api.ApiCallError):
            # Auto-upgrade is best-effort; fall back to the no-key path.
            pass

    raw = isolation.run_isolated(
        "youtube_mcp_v2.adapters.channel_scrape.fetch_channel_uploads",
        value,
        limit,
        timeout_s=25,
    )
    return raw, raw.get("source", "scrape")


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
    if target not in ("channel", "topic"):
        return envelope.fail(
            "bad_target",
            f"target must be 'channel' or 'topic', got {target!r}",
            recoverable=False,
        )
    if not value or not value.strip():
        return envelope.fail("bad_value", "value is empty", recoverable=False)

    limit = max(1, min(int(limit), 200))

    try:
        if target == "channel":
            raw, source = _build_channel(value.strip(), limit)
        else:
            raw = _build_topic(value.strip(), limit)
            source = "scrape"
    except TimeoutError as e:
        return envelope.fail("skeleton_build_timeout", str(e))
    except RuntimeError as e:
        return envelope.fail("skeleton_build_failed", str(e))

    handle = _skeleton.make_handle(target, value)
    payload = {
        "handle": handle,
        "target": target,
        "value": value,
        "built_at": _skeleton._now_iso(),
        "source": source,
        "expired_at": None,
        "channel": raw.get("channel"),
        "videos": raw.get("videos", []),
    }
    try:
        _skeleton.save_skeleton(payload)
    except FileExistsError:
        # This should be extraordinarily rare with microsecond handles, but the
        # storage layer intentionally refuses to violate frozen-snapshot history.
        return envelope.fail(
            "skeleton_handle_collision",
            f"refusing to overwrite existing skeleton handle {handle}",
            recoverable=True,
        )

    return envelope.ok(
        {
            "handle": handle,
            "target": target,
            "value": value,
            "source": source,
            "count": len(payload["videos"]),
        },
        source=source,
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
    return envelope.ok(data, source="cache")


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
