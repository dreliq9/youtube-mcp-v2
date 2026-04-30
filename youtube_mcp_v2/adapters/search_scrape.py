"""Page-scrape YouTube search results. No API key needed.

Designed to run in a subprocess (isolation.py): all I/O is scoped to this call,
all return values are plain JSON-serializable dicts.
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


def search_videos(query: str, n: int = 10) -> list[dict[str, Any]]:
    """Return up to n video results for a query. Pure-data return for subprocess transport."""
    n = max(1, min(n, 50))
    with httpx.Client(
        timeout=httpx.Timeout(15.0, connect=10.0),
        follow_redirects=True,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as client:
        resp = client.get(
            "https://www.youtube.com/results",
            params={"search_query": query},
        )
        resp.raise_for_status()
        html = resp.text

    data = _extract_initial_data(html)
    if not data:
        return []

    out: list[dict[str, Any]] = []
    try:
        section_list = (
            data.get("contents", {})
            .get("twoColumnSearchResultsRenderer", {})
            .get("primaryContents", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
        )
        for section in section_list:
            items = section.get("itemSectionRenderer", {}).get("contents", [])
            for item in items:
                renderer = item.get("videoRenderer")
                if not renderer:
                    continue
                vid = renderer.get("videoId")
                if not vid:
                    continue
                title_runs = renderer.get("title", {}).get("runs", [])
                title = "".join(r.get("text", "") for r in title_runs)

                channel_runs = renderer.get("ownerText", {}).get("runs", [])
                channel = "".join(r.get("text", "") for r in channel_runs)
                channel_id = ""
                if channel_runs:
                    nav = channel_runs[0].get("navigationEndpoint", {})
                    channel_id = (
                        nav.get("browseEndpoint", {}).get("browseId", "")
                        or ""
                    )

                duration = renderer.get("lengthText", {}).get("simpleText", "")
                views = renderer.get("viewCountText", {}).get("simpleText", "")
                published = renderer.get("publishedTimeText", {}).get("simpleText", "")

                out.append({
                    "id": vid,
                    "title": title,
                    "channel": channel,
                    "channel_id": channel_id,
                    "duration": duration,
                    "views": views,
                    "published": published,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                })

                if len(out) >= n:
                    return out
    except (AttributeError, TypeError, KeyError):
        pass

    return out
