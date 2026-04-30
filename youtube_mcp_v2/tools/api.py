"""api.* — tier-2 YouTube Data API v3 tools (BYO key via YOUTUBE_API_KEY).

All tools share the same auth/error path: missing key → recoverable
auth_required envelope; quota exhausted → recoverable quota_exceeded; any
other API error → recoverable api_call_failed. Server never crashes.
"""

from __future__ import annotations

from typing import Any

from .. import envelope
from ..adapters import data_api
from ..adapters.data_api import (
    ApiAuthError,
    ApiCallError,
    ApiQuotaError,
)


def _wrap_api_call(fn, *args, **kwargs) -> dict[str, Any]:
    """Run an adapter call and translate exceptions to envelope errors."""
    try:
        result = fn(*args, **kwargs)
    except ApiAuthError as e:
        return envelope.fail("auth_required", str(e), recoverable=True, source="api")
    except ApiQuotaError as e:
        return envelope.fail("quota_exceeded", str(e), recoverable=True, source="api")
    except ApiCallError as e:
        return envelope.fail("api_call_failed", str(e), recoverable=True, source="api")
    except Exception as e:  # adapter-side bug or transport blip — still don't crash
        return envelope.fail("api_call_failed", repr(e), recoverable=True, source="api")
    return envelope.ok(result, source="api")


def api_search(
    query: str,
    max_results: int = 10,
    order: str = "relevance",
    published_after: str | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """Search YouTube via the Data API v3 (richer, no rate-limit quirks).

    USE WHEN: YOUTUBE_API_KEY is set and you want clean structured results
              (channel ids, ISO publish dates, no scrape fragility).
    DO NOT USE WHEN: no API key — call scrape.search instead.
    OUTPUT SHAPE: envelope wrapping list of {id, title, channel, channel_id,
                  published_at, description_excerpt, url}.
    QUOTA: 100 units per call. ~100 searches/day on the free tier.
    """
    if not query or not query.strip():
        return envelope.fail("bad_query", "query is empty", recoverable=False, source="api")
    return _wrap_api_call(
        data_api.search,
        query, max_results, order, published_after, channel_id,
    )


def api_channel_stats(channel_id_or_handle: str) -> dict[str, Any]:
    """Channel statistics — subscriber count, total views, video count, etc.

    USE WHEN: you need creator-size signals (sub count, view total) or canonical
              channel metadata (custom URL, country, created_at).
    DO NOT USE WHEN: you only need a list of recent uploads — call
                     skeleton.build(target='channel') instead.
    OUTPUT SHAPE: envelope wrapping {id, title, description_excerpt,
                  subscriber_count, view_count, video_count, created_at,
                  country, custom_url}.
    QUOTA: 1 unit per call.
    """
    if not channel_id_or_handle or not channel_id_or_handle.strip():
        return envelope.fail("bad_channel", "channel id/handle is empty",
                             recoverable=False, source="api")
    env = _wrap_api_call(data_api.channel_stats, channel_id_or_handle.strip())
    if env["error"] is None and env["data"] is None:
        return envelope.fail("channel_not_found",
                             f"no channel for {channel_id_or_handle!r}",
                             recoverable=False, source="api")
    return env


def api_trending(
    region: str = "US",
    category_id: str | None = None,
    n: int = 20,
) -> dict[str, Any]:
    """Trending videos for a region (chart=mostPopular).

    USE WHEN: you want what's currently popular — for content research,
              recommendation systems, or trend monitoring.
    DO NOT USE WHEN: you have a specific topic — use api.search or
                     scrape.search.
    OUTPUT SHAPE: envelope wrapping list of {id, title, channel, channel_id,
                  published_at, duration_s, view_count, like_count,
                  comment_count, category_id}.
    QUOTA: 1 unit per call.
    """
    return _wrap_api_call(data_api.trending, region, category_id, n)


def api_video_categories(region: str = "US") -> dict[str, Any]:
    """List YouTube category ids for a region (used to filter api.trending).

    USE WHEN: you need the numeric category id for filtering trending or
              search results.
    DO NOT USE WHEN: you don't care about category filters.
    OUTPUT SHAPE: envelope wrapping list of {id, title}.
    QUOTA: 1 unit per call.
    """
    return _wrap_api_call(data_api.video_categories, region)
