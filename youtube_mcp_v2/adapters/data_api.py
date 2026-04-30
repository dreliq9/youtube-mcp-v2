"""YouTube Data API v3 adapter — tier-2.

BYO key via YOUTUBE_API_KEY env var. Returns lean payloads (kirbah pattern):
strip eTags, redundant thumbnail dicts, localization fields, etc. The LLM
gets exactly what it needs to reason, nothing else.

Quota cost guide (per call):
- videos.list (any number of ids): 1
- channels.list:                   1
- playlistItems.list (per page):   1
- search.list:                     **100** — use sparingly
- videoCategories.list:            1
"""

from __future__ import annotations

import os
import re
from typing import Any

# Imported lazily so the import doesn't fail if google-api-python-client
# isn't installed and only tier-1 tools are being used.


_DUR_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


class ApiAuthError(RuntimeError):
    """Raised when YOUTUBE_API_KEY is missing or rejected."""


class ApiCallError(RuntimeError):
    """Raised on non-recoverable API failures (bad request, not found, etc.)."""


class ApiQuotaError(RuntimeError):
    """Raised when daily quota is exhausted."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_iso_duration(iso: str | None) -> int | None:
    """ISO 8601 duration → seconds. PT4M13S → 253. None on bad input."""
    if not iso:
        return None
    m = _DUR_RE.fullmatch(iso)
    if not m:
        return None
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + s


_client = None


def _get_client():
    """Lazy-init the API client. Raises ApiAuthError if no key."""
    global _client
    if _client is not None:
        return _client
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise ApiAuthError(
            "YOUTUBE_API_KEY not set. See SPEC §2 (BYO key) or the "
            "Google Cloud Console docs to obtain one."
        )
    from googleapiclient.discovery import build  # local import — optional dep
    _client = build("youtube", "v3", developerKey=key, cache_discovery=False)
    return _client


def _execute(request) -> dict[str, Any]:
    """Run a Google API client request. Translate errors to typed exceptions."""
    from googleapiclient.errors import HttpError

    try:
        return request.execute()
    except HttpError as e:
        status = getattr(e.resp, "status", None) or 0
        body = e.content.decode("utf-8", errors="replace") if e.content else ""
        if status == 403 and "quotaExceeded" in body:
            raise ApiQuotaError(
                "YouTube Data API daily quota exhausted (resets 00:00 PT)"
            ) from e
        if status in (401, 403):
            raise ApiAuthError(
                f"API rejected request (status {status}): {body[:200]}"
            ) from e
        raise ApiCallError(f"API call failed (status {status}): {body[:200]}") from e


# ---------------------------------------------------------------------------
# Lean shaping
# ---------------------------------------------------------------------------


def _shape_search_item(item: dict[str, Any]) -> dict[str, Any]:
    snip = item.get("snippet", {}) or {}
    vid_id = (item.get("id") or {}).get("videoId", "")
    desc = snip.get("description", "") or ""
    return {
        "id": vid_id,
        "title": snip.get("title", ""),
        "channel": snip.get("channelTitle", ""),
        "channel_id": snip.get("channelId", ""),
        "published_at": snip.get("publishedAt", ""),
        "description_excerpt": (desc[:200] + "…") if len(desc) > 200 else desc,
        "url": f"https://www.youtube.com/watch?v={vid_id}" if vid_id else "",
    }


def _shape_channel_item(item: dict[str, Any]) -> dict[str, Any]:
    snip = item.get("snippet", {}) or {}
    stats = item.get("statistics", {}) or {}
    branding = item.get("brandingSettings", {}) or {}
    desc = snip.get("description", "") or ""
    return {
        "id": item.get("id", ""),
        "title": snip.get("title", ""),
        "description_excerpt": (desc[:300] + "…") if len(desc) > 300 else desc,
        "subscriber_count": int(stats["subscriberCount"]) if stats.get("subscriberCount") else None,
        "view_count": int(stats["viewCount"]) if stats.get("viewCount") else None,
        "video_count": int(stats["videoCount"]) if stats.get("videoCount") else None,
        "created_at": snip.get("publishedAt", ""),
        "country": snip.get("country", ""),
        "custom_url": snip.get("customUrl", ""),
    }


def _shape_video_full(item: dict[str, Any]) -> dict[str, Any]:
    snip = item.get("snippet", {}) or {}
    cd = item.get("contentDetails", {}) or {}
    stats = item.get("statistics", {}) or {}
    return {
        "id": item.get("id", ""),
        "title": snip.get("title", ""),
        "channel": snip.get("channelTitle", ""),
        "channel_id": snip.get("channelId", ""),
        "published_at": snip.get("publishedAt", ""),
        "duration_s": parse_iso_duration(cd.get("duration")),
        "view_count": int(stats["viewCount"]) if stats.get("viewCount") else None,
        "like_count": int(stats["likeCount"]) if stats.get("likeCount") else None,
        "comment_count": int(stats["commentCount"]) if stats.get("commentCount") else None,
        "category_id": snip.get("categoryId", ""),
    }


# ---------------------------------------------------------------------------
# Public API — used by tools.api and skeleton_tools (channel auto-upgrade)
# ---------------------------------------------------------------------------


def search(
    query: str,
    max_results: int = 10,
    order: str = "relevance",
    published_after: str | None = None,
    channel_id: str | None = None,
) -> list[dict[str, Any]]:
    """search.list (100 quota units). Returns lean video items."""
    yt = _get_client()
    params: dict[str, Any] = {
        "part": "snippet",
        "type": "video",
        "q": query,
        "maxResults": max(1, min(int(max_results), 50)),
        "order": order,
    }
    if published_after:
        params["publishedAfter"] = published_after
    if channel_id:
        params["channelId"] = channel_id
    resp = _execute(yt.search().list(**params))
    return [_shape_search_item(it) for it in resp.get("items", [])]


def channel_stats(channel_id_or_handle: str) -> dict[str, Any] | None:
    """channels.list (1 quota unit). Returns one lean channel record or None."""
    yt = _get_client()
    if channel_id_or_handle.startswith("@"):
        params = {"part": "snippet,statistics", "forHandle": channel_id_or_handle}
    else:
        params = {"part": "snippet,statistics", "id": channel_id_or_handle}
    resp = _execute(yt.channels().list(**params))
    items = resp.get("items", [])
    if not items:
        return None
    return _shape_channel_item(items[0])


def trending(
    region: str = "US",
    category_id: str | None = None,
    n: int = 20,
) -> list[dict[str, Any]]:
    """videos.list (1 quota unit) for chart=mostPopular. Lean video records."""
    yt = _get_client()
    params: dict[str, Any] = {
        "part": "snippet,contentDetails,statistics",
        "chart": "mostPopular",
        "regionCode": region,
        "maxResults": max(1, min(int(n), 50)),
    }
    if category_id:
        params["videoCategoryId"] = category_id
    resp = _execute(yt.videos().list(**params))
    return [_shape_video_full(it) for it in resp.get("items", [])]


def video_categories(region: str = "US") -> list[dict[str, Any]]:
    """videoCategories.list (1 quota unit). Returns [{id, title}]."""
    yt = _get_client()
    resp = _execute(
        yt.videoCategories().list(part="snippet", regionCode=region)
    )
    out = []
    for it in resp.get("items", []):
        out.append({
            "id": it.get("id", ""),
            "title": (it.get("snippet", {}) or {}).get("title", ""),
        })
    return out


def channel_uploads(
    channel_id_or_handle: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Skeleton.build(target='channel') auto-upgrade path: full uploads enumeration.

    Three API calls (~3 quota units total):
      1. channels.list → get uploads playlist id and channel meta
      2. playlistItems.list → page videos out of the uploads playlist
      3. videos.list (batched) → enrich with contentDetails + statistics
    """
    yt = _get_client()

    # 1. Resolve channel + uploads playlist
    if channel_id_or_handle.startswith("@"):
        ch_params = {"part": "snippet,contentDetails", "forHandle": channel_id_or_handle}
    elif channel_id_or_handle.startswith("UC"):
        ch_params = {"part": "snippet,contentDetails", "id": channel_id_or_handle}
    else:
        # Bare handle without @ — try forHandle anyway
        ch_params = {"part": "snippet,contentDetails", "forHandle": "@" + channel_id_or_handle}
    ch_resp = _execute(yt.channels().list(**ch_params))
    items = ch_resp.get("items", [])
    if not items:
        return {"channel": None, "videos": []}
    channel = items[0]
    snip = channel.get("snippet", {}) or {}
    uploads_id = (
        channel.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads", "")
    )
    channel_meta = {
        "id": channel.get("id", ""),
        "handle": snip.get("customUrl", ""),
        "title": snip.get("title", ""),
        "url": (
            f"https://www.youtube.com/channel/{channel.get('id','')}"
            if channel.get("id") else ""
        ),
    }
    if not uploads_id:
        return {"channel": channel_meta, "videos": []}

    # 2. Page through the uploads playlist
    video_ids: list[str] = []
    page_token: str | None = None
    while len(video_ids) < limit:
        page_params: dict[str, Any] = {
            "part": "contentDetails",
            "playlistId": uploads_id,
            "maxResults": min(50, limit - len(video_ids)),
        }
        if page_token:
            page_params["pageToken"] = page_token
        pi_resp = _execute(yt.playlistItems().list(**page_params))
        for it in pi_resp.get("items", []):
            vid = (it.get("contentDetails", {}) or {}).get("videoId")
            if vid:
                video_ids.append(vid)
        page_token = pi_resp.get("nextPageToken")
        if not page_token:
            break

    if not video_ids:
        return {"channel": channel_meta, "videos": []}

    # 3. Batch-enrich (videos.list accepts up to 50 ids per call, 1 unit total)
    videos: list[dict[str, Any]] = []
    for chunk_start in range(0, len(video_ids), 50):
        chunk = video_ids[chunk_start:chunk_start + 50]
        v_resp = _execute(
            yt.videos().list(
                part="snippet,contentDetails,statistics",
                id=",".join(chunk),
                maxResults=50,
            )
        )
        for it in v_resp.get("items", []):
            shaped = _shape_video_full(it)
            videos.append({
                "id": shaped["id"],
                "title": shaped["title"],
                "duration_s": shaped["duration_s"],
                "duration_text": "",
                "published": shaped["published_at"],
                "view_count": shaped["view_count"],
                "thumbnail_url": f"https://i.ytimg.com/vi/{shaped['id']}/hqdefault.jpg",
                # Reserved nullable slots — same schema as scrape path so v0.3
                # ml.embed_transcripts can populate them uniformly.
                "has_transcript": None,
                "caption_track_id": None,
                "lang": None,
            })

    return {"channel": channel_meta, "videos": videos}
