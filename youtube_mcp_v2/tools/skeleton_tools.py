"""skeleton.* — frozen reference objects for multi-step research.

Once built, a skeleton is read-only. Re-running build creates a new handle. Old
handles remain queryable forever (revision discipline). v0.3 ML tools will
attach vectors to existing skeleton videoIds without modifying the skeleton.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from .. import envelope, isolation, skeleton as _skeleton


def _build_channel(value: str, limit: int) -> tuple[dict[str, Any], str]:
    """Channel skeleton builder. Returns (raw_data, source).

    Auto-upgrades to tier-2 Data API v3 (full enumeration) when YOUTUBE_API_KEY
    is set; otherwise scrapes the /videos tab (~30 most recent uploads). The
    on-disk skeleton's `source` field reflects which path ran so the LLM and
    future inspections can reason about provenance.
    """
    if os.environ.get("YOUTUBE_API_KEY"):
        # Tier-2 path. Imported lazily to keep the optional [api] dep optional.
        from ..adapters import data_api
        try:
            raw = data_api.channel_uploads(value, limit=limit)
            return raw, "api"
        except (data_api.ApiAuthError, data_api.ApiQuotaError, data_api.ApiCallError):
            # Fall through to scrape — auto-upgrade is best-effort, not a hard dep.
            pass

    raw = isolation.run_isolated(
        "youtube_mcp_v2.adapters.channel_scrape.fetch_channel_uploads",
        value, limit,
        timeout_s=25,
    )
    return raw, "scrape"


def _build_topic(value: str, limit: int) -> dict[str, Any]:
    """Tier-1: scrape search results, reshape into skeleton-video schema."""
    results = isolation.run_isolated(
        "youtube_mcp_v2.adapters.search_scrape.search_videos",
        value, limit,
        timeout_s=20,
    )
    videos = []
    for r in results:
        videos.append({
            "id": r["id"],
            "title": r["title"],
            "channel": r.get("channel", ""),
            "channel_id": r.get("channel_id", ""),
            "duration_s": None,            # search results don't expose seconds
            "duration_text": r.get("duration", ""),
            "published": r.get("published", ""),
            "view_count": r.get("views", ""),
            "thumbnail_url": f"https://i.ytimg.com/vi/{r['id']}/hqdefault.jpg",
            # Reserved nullable fields — populated later by inspect.video / ml.embed.
            "has_transcript": None,
            "caption_track_id": None,
            "lang": None,
        })
    return {
        "channel": None,           # topic skeletons aren't tied to one channel
        "videos": videos,
    }


def skeleton_build(
    target: Literal["channel", "topic"],
    value: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Build a frozen reference of videos for a channel or topic.

    USE WHEN target='channel': you want a stable list of a creator's recent
                               uploads to fan downstream calls (transcripts,
                               frames) against — without re-fetching.
    USE WHEN target='topic':   you want a frozen snapshot of search results for
                               a query, e.g. for comparison or later semantic
                               search (v0.3 ml.search_skeleton).
    DO NOT USE WHEN: you only need a one-shot search — call scrape.search.
    OUTPUT SHAPE: envelope wrapping {handle, target, value, source, count}.
                  The full skeleton is on disk; use skeleton.get to load it.
    """
    if target not in ("channel", "topic"):
        return envelope.fail(
            "bad_target", f"target must be 'channel' or 'topic', got {target!r}",
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
    _skeleton.save_skeleton(payload)

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
    """List the videos in a skeleton.

    USE WHEN: iterating videos for downstream batch ops (e.g. inspect each, then
              fetch transcripts for the ones with captions).
    DO NOT USE WHEN: you need the channel meta or build provenance — use skeleton.get.
    OUTPUT SHAPE: envelope wrapping list of video entries. With enrich=True
                  (default), nullable fields (has_transcript, lang_default,
                  duration_s, etc.) are filled from the cache where available;
                  the on-disk skeleton itself is never modified.
    """
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
    """Load the full skeleton snapshot.

    USE WHEN: you need the build provenance (built_at, source, channel meta) or
              the raw frozen video list as captured at build time.
    DO NOT USE WHEN: you only need the videos themselves — use skeleton.list.
    OUTPUT SHAPE: envelope wrapping full skeleton dict.
    """
    try:
        data = _skeleton.load_skeleton(handle)
    except FileNotFoundError as e:
        return envelope.fail("skeleton_not_found", str(e), recoverable=False)
    except ValueError as e:
        return envelope.fail("bad_handle", str(e), recoverable=False)
    return envelope.ok(data, source="cache")


def skeleton_expire(handle: str) -> dict[str, Any]:
    """Mark a skeleton stale. Does NOT delete (revision discipline).

    USE WHEN: a skeleton's underlying channel/topic has changed enough that
              downstream consumers should rebuild — but you want the old data
              preserved for diff/audit.
    DO NOT USE WHEN: you want to discard the data — that's not supported on
                     purpose. Build a new handle and ignore the old one instead.
    OUTPUT SHAPE: envelope wrapping {handle, expired_at}.
    """
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
    """List all skeletons on disk (summary view).

    USE WHEN: discovering what skeletons already exist before building a new one.
    DO NOT USE WHEN: you already know the handle.
    OUTPUT SHAPE: envelope wrapping list of {handle, target, value, built_at,
                  expired_at, video_count, source}.
    """
    summaries = _skeleton.list_skeletons(target=target)
    return envelope.ok(summaries, source="cache")
