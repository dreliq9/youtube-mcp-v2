"""youtube-transcript-api adapter with explicit, secret-safe proxy configuration.

Returns timestamped segments plus resolved language/generated-caption metadata.
Proxy settings are opt-in and namespaced to youtube-mcp-v2 so the server does not
silently inherit unrelated process-wide proxy policy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (  # noqa: F401  (re-exported)
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)
from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig


@dataclass
class Segment:
    start_s: float
    duration_s: float
    text: str

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


@dataclass
class TranscriptFetch:
    segments: list[Segment]
    lang: str
    is_generated: bool


@dataclass(frozen=True)
class ProxySettings:
    mode: str  # direct | generic | webshare
    config: Any | None
    summary: dict[str, Any]


_ENV_PROXY = "YOUTUBE_TRANSCRIPT_PROXY"
_ENV_HTTP_PROXY = "YOUTUBE_TRANSCRIPT_HTTP_PROXY"
_ENV_HTTPS_PROXY = "YOUTUBE_TRANSCRIPT_HTTPS_PROXY"
_ENV_WEBSHARE_USER = "YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME"
_ENV_WEBSHARE_PASSWORD = "YOUTUBE_TRANSCRIPT_WEBSHARE_PASSWORD"
_ENV_WEBSHARE_LOCATIONS = "YOUTUBE_TRANSCRIPT_WEBSHARE_LOCATIONS"


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def proxy_settings_from_env() -> ProxySettings:
    """Build transcript proxy settings without exposing credential-bearing URLs.

    Supported generic settings:
      YOUTUBE_TRANSCRIPT_PROXY=<url>              # use for HTTP + HTTPS
      YOUTUBE_TRANSCRIPT_HTTP_PROXY=<url>
      YOUTUBE_TRANSCRIPT_HTTPS_PROXY=<url>

    Supported Webshare settings:
      YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME=<user>
      YOUTUBE_TRANSCRIPT_WEBSHARE_PASSWORD=<password>
      YOUTUBE_TRANSCRIPT_WEBSHARE_LOCATIONS=us,de   # optional

    Generic and Webshare modes are mutually exclusive so routing is never
    ambiguous. The returned `summary` contains booleans/location codes only —
    never proxy URLs, usernames, or passwords.
    """
    shared = _env(_ENV_PROXY)
    http_proxy = _env(_ENV_HTTP_PROXY) or shared
    https_proxy = _env(_ENV_HTTPS_PROXY) or shared

    webshare_user = _env(_ENV_WEBSHARE_USER)
    webshare_password = _env(_ENV_WEBSHARE_PASSWORD)
    locations_raw = _env(_ENV_WEBSHARE_LOCATIONS)
    locations = (
        [part.strip().lower() for part in locations_raw.split(",") if part.strip()]
        if locations_raw
        else []
    )

    generic_configured = bool(http_proxy or https_proxy)
    webshare_configured = bool(webshare_user or webshare_password or locations)

    if generic_configured and webshare_configured:
        raise ValueError(
            "configure either generic transcript proxy settings or Webshare, not both"
        )

    if webshare_configured:
        if not webshare_user or not webshare_password:
            raise ValueError(
                "Webshare transcript proxy requires both username and password"
            )
        kwargs: dict[str, Any] = {
            "proxy_username": webshare_user,
            "proxy_password": webshare_password,
        }
        if locations:
            kwargs["filter_ip_locations"] = locations
        config = WebshareProxyConfig(**kwargs)
        return ProxySettings(
            mode="webshare",
            config=config,
            summary={"mode": "webshare", "locations": locations},
        )

    if generic_configured:
        # If the caller provided only one generic URL, use it for both schemes.
        # This is convenient for SOCKS/rotating endpoints that handle both.
        http_url = http_proxy or https_proxy
        https_url = https_proxy or http_proxy
        config = GenericProxyConfig(http_url=http_url, https_url=https_url)
        return ProxySettings(
            mode="generic",
            config=config,
            summary={
                "mode": "generic",
                "http_configured": http_url is not None,
                "https_configured": https_url is not None,
            },
        )

    return ProxySettings(mode="direct", config=None, summary={"mode": "direct"})


def _build_client() -> YouTubeTranscriptApi:
    settings = proxy_settings_from_env()
    if settings.config is None:
        return YouTubeTranscriptApi()
    return YouTubeTranscriptApi(proxy_config=settings.config)


def fetch_transcript(video_id: str, lang: str = "en") -> TranscriptFetch:
    """Fetch a transcript. Tries `lang`, then English, then any available track.

    Raises NoTranscriptFound if absolutely nothing is available, or the upstream
    error if the video is private/disabled/etc. Proxy configuration errors are
    raised without including credential values.
    """
    ytt = _build_client()

    # First attempt: requested language with English fallback.
    try:
        result = ytt.fetch(video_id, languages=[lang, "en"])
        return _to_fetch(result, fallback_lang=lang)
    except NoTranscriptFound:
        pass

    # Last-resort: any track at all.
    transcript_list = ytt.list(video_id)
    available = list(transcript_list)
    if not available:
        raise NoTranscriptFound(video_id, [lang], None)
    track = available[0]
    fetched = track.fetch()
    return _to_fetch(
        fetched,
        fallback_lang=track.language_code,
        is_generated=track.is_generated,
    )


def _to_fetch(
    result,
    *,
    fallback_lang: str,
    is_generated: bool | None = None,
) -> TranscriptFetch:
    segs = [
        Segment(start_s=float(s.start), duration_s=float(s.duration), text=s.text)
        for s in result.snippets
    ]
    lang = getattr(result, "language_code", None) or fallback_lang
    gen = (
        is_generated
        if is_generated is not None
        else bool(getattr(result, "is_generated", False))
    )
    return TranscriptFetch(segments=segs, lang=lang, is_generated=gen)
