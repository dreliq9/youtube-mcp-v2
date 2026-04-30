"""Page scrape — fetch and parse YouTube watch pages without an API key.

httpx with explicit timeouts is bounded and safe in-process. We don't need
subprocess isolation for these calls; the JSON parsing is pure Python.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from bs4 import BeautifulSoup

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


_client: httpx.Client | None = None


def _client_get() -> httpx.Client:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.Client(
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
    return _client


def _extract_player_response(html: str) -> dict[str, Any] | None:
    for pat in (
        r"var\s+ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;",
        r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;",
    ):
        m = re.search(pat, html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


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


@dataclass
class WatchPage:
    video_id: str
    title: str = ""
    channel: str = ""
    channel_id: str = ""
    description: str = ""
    duration_s: int | None = None
    view_count: int | None = None
    publish_date: str = ""
    thumbnail_url: str = ""
    keywords: list[str] = field(default_factory=list)
    available_caption_langs: list[str] = field(default_factory=list)
    has_transcript: bool = False
    age_gated: bool = False
    livestream: bool = False
    embed_allowed: bool = True


def fetch_watch_page(video_id: str) -> WatchPage:
    """Fetch and parse the watch page for a single video. No key needed.

    Returns a WatchPage with whatever could be parsed; missing fields stay default.
    """
    client = _client_get()
    resp = client.get(f"https://www.youtube.com/watch?v={video_id}")
    resp.raise_for_status()
    html = resp.text

    page = WatchPage(video_id=video_id)
    page.thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    player = _extract_player_response(html) or {}
    vd = player.get("videoDetails", {})
    page.title = vd.get("title", "")
    page.channel = vd.get("author", "")
    page.channel_id = vd.get("channelId", "")
    page.description = vd.get("shortDescription", "")
    page.keywords = vd.get("keywords", []) or []
    page.livestream = bool(vd.get("isLiveContent", False))

    if (length := vd.get("lengthSeconds")):
        try:
            page.duration_s = int(length)
        except (ValueError, TypeError):
            pass

    if (vc := vd.get("viewCount")):
        try:
            page.view_count = int(vc)
        except (ValueError, TypeError):
            pass

    thumbs = vd.get("thumbnail", {}).get("thumbnails", [])
    if thumbs:
        page.thumbnail_url = thumbs[-1].get("url", page.thumbnail_url)

    # Caption tracks — tells us whether transcripts exist and in which langs.
    captions = (
        player.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks", [])
    )
    if captions:
        page.has_transcript = True
        for track in captions:
            code = track.get("languageCode")
            if code and code not in page.available_caption_langs:
                page.available_caption_langs.append(code)

    # Embed/age-gate flags
    playability = player.get("playabilityStatus", {}) or {}
    if playability.get("status") == "LOGIN_REQUIRED":
        page.age_gated = True
    if playability.get("playableInEmbed") is False:
        page.embed_allowed = False

    # Publish date from initialData
    initial = _extract_initial_data(html)
    if initial:
        try:
            results = (
                initial.get("contents", {})
                .get("twoColumnWatchNextResults", {})
                .get("results", {})
                .get("results", {})
                .get("contents", [])
            )
            for block in results:
                primary = block.get("videoPrimaryInfoRenderer", {})
                date_text = primary.get("dateText", {}).get("simpleText", "")
                if date_text:
                    page.publish_date = date_text
                    break
        except (AttributeError, TypeError):
            pass

    if not page.title:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("title")
        if tag:
            page.title = tag.get_text().replace(" - YouTube", "").strip()

    return page
