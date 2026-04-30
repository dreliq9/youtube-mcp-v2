"""Page-scrape a YouTube channel's uploads list. No API key needed.

Tier-1 path for skeleton.build(target='channel'). Returns up to ~30 most recent
uploads from the channel's /videos tab — YouTube's first-page render. For
exhaustive enumeration, the tier-2 Data API is required (auto-upgrade triggered
by YOUTUBE_API_KEY).

Designed to run in a subprocess (isolation.py).
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]+$")


def _resolve_url(value: str) -> str:
    """Build the channel /videos URL from a channel id, @handle, or full URL."""
    v = value.strip()
    if _CHANNEL_ID_RE.match(v):
        return f"https://www.youtube.com/channel/{v}/videos"
    if _HANDLE_RE.match(v):
        return f"https://www.youtube.com/{v}/videos"
    if v.startswith("http"):
        # If they passed a channel URL, append /videos.
        if "/videos" in v:
            return v
        return v.rstrip("/") + "/videos"
    # Fallback: treat as a handle without @ prefix.
    return f"https://www.youtube.com/@{v}/videos"


def _extract_initial_data(html: str) -> dict[str, Any] | None:
    for pat in (
        r"var\s+ytInitialData\s*=\s*(\{.+?\})\s*;",
        r"ytInitialData\s*=\s*(\{.+?\})\s*;",
    ):
        m = re.search(pat, html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


def _parse_duration_text(s: str) -> int | None:
    """Convert "12:34" or "1:02:03" or "0:30" → seconds. Returns None if unparseable."""
    if not s or ":" not in s:
        return None
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        m, s_ = nums
        return m * 60 + s_
    if len(nums) == 3:
        h, m, s_ = nums
        return h * 3600 + m * 60 + s_
    return None


def _walk_video_renderers(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the channel-tab response and yield videoRenderer / gridVideoRenderer dicts.

    Channel layouts vary (richGridRenderer in newer pages, gridRenderer in older).
    Walk depth-first looking for the relevant keys.
    """
    found: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("videoRenderer", "gridVideoRenderer"):
                if key in node and isinstance(node[key], dict):
                    found.append(node[key])
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(data)
    return found


def fetch_channel_uploads(value: str, limit: int = 30) -> dict[str, Any]:
    """Fetch a channel's /videos page. Returns channel meta + list of recent uploads.

    Pure-data return so subprocess transport is JSON-clean.
    """
    url = _resolve_url(value)
    with httpx.Client(
        timeout=httpx.Timeout(15.0, connect=10.0),
        follow_redirects=True,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as client:
        resp = client.get(url)
        resp.raise_for_status()
        html = resp.text

    data = _extract_initial_data(html) or {}

    # Channel meta — try multiple header shapes (YouTube has migrated this).
    channel_id = ""
    channel_handle = ""
    channel_title = ""
    metadata = data.get("metadata", {}).get("channelMetadataRenderer", {})
    if metadata:
        channel_id = metadata.get("externalId", "") or ""
        channel_title = metadata.get("title", "") or ""
        channel_url = metadata.get("vanityChannelUrl", "") or ""
        if "/@" in channel_url:
            channel_handle = "@" + channel_url.rsplit("/@", 1)[-1]

    # Walk all video renderers in the page and collect recent uploads.
    renderers = _walk_video_renderers(data)
    videos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in renderers:
        vid = r.get("videoId", "")
        if not vid or vid in seen:
            continue
        seen.add(vid)

        title_runs = r.get("title", {}).get("runs", [])
        title = "".join(t.get("text", "") for t in title_runs)
        if not title:
            title = r.get("title", {}).get("simpleText", "")

        duration_text = r.get("lengthText", {}).get("simpleText", "")
        duration_s = _parse_duration_text(duration_text)

        published = r.get("publishedTimeText", {}).get("simpleText", "")
        view_count = r.get("viewCountText", {}).get("simpleText", "")

        thumbs = r.get("thumbnail", {}).get("thumbnails", [])
        thumbnail_url = thumbs[-1].get("url", "") if thumbs else (
            f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        )

        videos.append({
            "id": vid,
            "title": title,
            "duration_s": duration_s,
            "duration_text": duration_text,
            "published": published,
            "view_count": view_count,
            "thumbnail_url": thumbnail_url,
            # Reserved for later inspect.video / ml.transcribe enrichment.
            # Schema constraint per SPEC §12 — never drop these fields.
            "has_transcript": None,
            "caption_track_id": None,
            "lang": None,
        })

        if len(videos) >= limit:
            break

    return {
        "channel": {
            "id": channel_id,
            "handle": channel_handle,
            "title": channel_title,
            "url": url,
        },
        "videos": videos,
    }
