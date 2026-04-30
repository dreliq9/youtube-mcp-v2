"""URL parsing — extract YouTube video IDs from any common URL format."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_BARE_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


def parse_video_id(url: str) -> str:
    """Extract video ID from any common YouTube URL format.

    Raises ValueError if no id can be parsed.
    """
    url = url.strip()

    if _BARE_ID_RE.fullmatch(url):
        return url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host in ("youtu.be", "www.youtu.be"):
        vid = parsed.path.lstrip("/").split("/")[0]
        if vid:
            return vid

    if "youtube" in host:
        qs = parse_qs(parsed.query)
        if "v" in qs:
            return qs["v"][0]
        segments = parsed.path.strip("/").split("/")
        if len(segments) >= 2 and segments[0] in ("embed", "v", "shorts", "live"):
            return segments[1]

    raise ValueError(
        f"Could not extract video ID from: {url!r}\n"
        "Supported: youtube.com/watch?v=..., youtu.be/..., "
        "youtube.com/shorts/..., youtube.com/embed/..., or bare 11-char ID"
    )
