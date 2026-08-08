"""URL parsing — extract and validate YouTube video IDs from common URL forms."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTU_BE_HOSTS = {"youtu.be", "www.youtu.be"}
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_PATH_ID_PREFIXES = {"embed", "v", "shorts", "live"}


def _validated_id(candidate: str | None) -> str | None:
    if not candidate:
        return None
    candidate = candidate.strip()
    return candidate if _VIDEO_ID_RE.fullmatch(candidate) else None


def parse_video_id(url: str) -> str:
    """Extract an 11-character video ID from a supported YouTube URL or bare ID.

    Raises ValueError when the host is not a recognized YouTube host, the URL form
    does not identify a video, or the extracted candidate is not a valid
    11-character YouTube video ID.
    """
    url = url.strip()

    bare = _validated_id(url)
    if bare is not None:
        return bare

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host in _YOUTU_BE_HOSTS:
        candidate = parsed.path.lstrip("/").split("/")[0]
        video_id = _validated_id(candidate)
        if video_id is not None:
            return video_id

    if host in _YOUTUBE_HOSTS:
        qs = parse_qs(parsed.query)
        if "v" in qs:
            video_id = _validated_id(qs["v"][0])
            if video_id is not None:
                return video_id

        segments = parsed.path.strip("/").split("/")
        if len(segments) >= 2 and segments[0] in _PATH_ID_PREFIXES:
            video_id = _validated_id(segments[1])
            if video_id is not None:
                return video_id

    raise ValueError(
        f"Could not extract a valid YouTube video ID from: {url!r}\n"
        "Supported: youtube.com/watch?v=<11-char-id>, youtu.be/<11-char-id>, "
        "youtube.com/shorts/<id>, youtube.com/live/<id>, "
        "youtube.com/embed/<id>, youtube-nocookie.com/embed/<id>, "
        "or a bare 11-character ID"
    )
