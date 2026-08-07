"""MCPServer entrypoint for youtube-mcp-v2."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server import MCPServer

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env", override=False)

from . import artifacts
from .tools.inspect import inspect_video as _inspect_video
from .tools.transcript import transcript_get as _transcript_get
from .tools.scrape import scrape_search as _scrape_search
from .tools.skeleton_tools import (
    skeleton_build as _skeleton_build,
    skeleton_list as _skeleton_list,
    skeleton_get as _skeleton_get,
    skeleton_expire as _skeleton_expire,
    skeleton_index as _skeleton_index,
)
from .tools.frame import frame_get as _frame_get
from .tools.audio import audio_get as _audio_get
from .tools.api import (
    api_search as _api_search,
    api_channel_stats as _api_channel_stats,
    api_trending as _api_trending,
    api_video_categories as _api_video_categories,
)

log = logging.getLogger("youtube-mcp-v2")
logging.basicConfig(level=logging.INFO)

mcp = MCPServer("YouTube v0.2.1")


# ---------------------------------------------------------------------------
# Tier 1 — pre-flight
# ---------------------------------------------------------------------------


@mcp.tool(name="inspect.video")
def tool_inspect_video(url_or_id: str) -> dict[str, Any]:
    """Pre-flight check on a YouTube video.

    USE WHEN: you have a URL/id and need capabilities before a heavier call.
    OUTPUT SHAPE: envelope wrapping video metadata/caption capabilities.
    """
    return _inspect_video(url_or_id)


# ---------------------------------------------------------------------------
# Tier 1 — transcripts
# ---------------------------------------------------------------------------


@mcp.tool(name="transcript.get")
def tool_transcript_get(
    url_or_id: str,
    mode: Literal["text", "timed", "chunked"] = "text",
    lang: str = "en",
    cursor: str | None = None,
    chunk_tokens: int = 500,
    chunk_overlap: int = 50,
) -> dict[str, Any]:
    """Fetch a validated YouTube transcript in text, timed, or chunked form."""
    return _transcript_get(
        url_or_id,
        mode=mode,
        lang=lang,
        cursor=cursor,
        chunk_tokens=chunk_tokens,
        chunk_overlap=chunk_overlap,
    )


# ---------------------------------------------------------------------------
# Tier 1 — search
# ---------------------------------------------------------------------------


@mcp.tool(name="scrape.search")
def tool_scrape_search(query: str, n: int = 10) -> dict[str, Any]:
    """Search YouTube without an API key through the isolated scrape path."""
    return _scrape_search(query, n)


# ---------------------------------------------------------------------------
# Tier 1 — frozen skeletons
# ---------------------------------------------------------------------------


@mcp.tool(name="skeleton.build")
def tool_skeleton_build(
    target: Literal["channel", "topic"],
    value: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Build a frozen channel/topic research snapshot."""
    return _skeleton_build(target, value, limit)


@mcp.tool(name="skeleton.list")
def tool_skeleton_list(handle: str, enrich: bool = True) -> dict[str, Any]:
    """List videos in a frozen snapshot, optionally joined with current cache metadata."""
    return _skeleton_list(handle, enrich=enrich)


@mcp.tool(name="skeleton.get")
def tool_skeleton_get(handle: str) -> dict[str, Any]:
    """Read one complete frozen skeleton record."""
    return _skeleton_get(handle)


@mcp.tool(name="skeleton.expire")
def tool_skeleton_expire(handle: str) -> dict[str, Any]:
    """Mark a skeleton stale without deleting captured membership."""
    return _skeleton_expire(handle)


@mcp.tool(name="skeleton.index")
def tool_skeleton_index(target: str | None = None) -> dict[str, Any]:
    """Discover frozen skeletons already stored locally."""
    return _skeleton_index(target)


# ---------------------------------------------------------------------------
# Tier 1 — frame/audio extraction
# ---------------------------------------------------------------------------


@mcp.tool(name="frame.get")
def tool_frame_get(
    url_or_id: str,
    mode: Literal["single", "sheet"] = "single",
    timestamp_s: float | None = None,
    n: int = 12,
    layout: str = "4x3",
    size: str = "1280x720",
    fmt: Literal["png", "jpg"] = "png",
) -> dict[str, Any]:
    """Extract a frame/contact sheet and return local plus portable resource refs."""
    return _frame_get(
        url_or_id,
        mode=mode,
        timestamp_s=timestamp_s,
        n=n,
        layout=layout,
        size=size,
        fmt=fmt,
    )


@mcp.tool(name="audio.get")
def tool_audio_get(
    url_or_id: str,
    fmt: Literal["wav", "m4a", "mp3", "flac", "ogg"] = "wav",
    sample_rate: int = 22050,
    start_s: float | None = None,
    end_s: float | None = None,
) -> dict[str, Any]:
    """Extract audio and return a local path plus portable resource ref when bounded."""
    return _audio_get(
        url_or_id,
        fmt=fmt,
        sample_rate=sample_rate,
        start_s=start_s,
        end_s=end_s,
    )


# ---------------------------------------------------------------------------
# Binary MCP resources — remote-safe access to persisted media
# ---------------------------------------------------------------------------
# MCP Python SDK v2 automatically returns bytes as BlobResourceContents. Each
# template has a static MIME type so hosts can consume the artifact directly.


@mcp.resource(
    "youtube-mcp://artifact/frame/png/{video_id}/{name}",
    mime_type="image/png",
)
def resource_frame_png(video_id: str, name: str) -> bytes:
    """Read a persisted PNG frame/contact-sheet artifact."""
    return artifacts.read_resource(
        kind="frame", ext="png", video_id=video_id, name=name
    )


@mcp.resource(
    "youtube-mcp://artifact/frame/jpg/{video_id}/{name}",
    mime_type="image/jpeg",
)
def resource_frame_jpg(video_id: str, name: str) -> bytes:
    """Read a persisted JPEG frame/contact-sheet artifact."""
    return artifacts.read_resource(
        kind="frame", ext="jpg", video_id=video_id, name=name
    )


@mcp.resource(
    "youtube-mcp://artifact/audio/wav/{video_id}/{name}",
    mime_type="audio/wav",
)
def resource_audio_wav(video_id: str, name: str) -> bytes:
    """Read a persisted WAV audio artifact."""
    return artifacts.read_resource(
        kind="audio", ext="wav", video_id=video_id, name=name
    )


@mcp.resource(
    "youtube-mcp://artifact/audio/m4a/{video_id}/{name}",
    mime_type="audio/mp4",
)
def resource_audio_m4a(video_id: str, name: str) -> bytes:
    """Read a persisted M4A audio artifact."""
    return artifacts.read_resource(
        kind="audio", ext="m4a", video_id=video_id, name=name
    )


@mcp.resource(
    "youtube-mcp://artifact/audio/mp3/{video_id}/{name}",
    mime_type="audio/mpeg",
)
def resource_audio_mp3(video_id: str, name: str) -> bytes:
    """Read a persisted MP3 audio artifact."""
    return artifacts.read_resource(
        kind="audio", ext="mp3", video_id=video_id, name=name
    )


@mcp.resource(
    "youtube-mcp://artifact/audio/flac/{video_id}/{name}",
    mime_type="audio/flac",
)
def resource_audio_flac(video_id: str, name: str) -> bytes:
    """Read a persisted FLAC audio artifact."""
    return artifacts.read_resource(
        kind="audio", ext="flac", video_id=video_id, name=name
    )


@mcp.resource(
    "youtube-mcp://artifact/audio/ogg/{video_id}/{name}",
    mime_type="audio/ogg",
)
def resource_audio_ogg(video_id: str, name: str) -> bytes:
    """Read a persisted OGG audio artifact."""
    return artifacts.read_resource(
        kind="audio", ext="ogg", video_id=video_id, name=name
    )


# ---------------------------------------------------------------------------
# Tier 2 — YouTube Data API v3
# ---------------------------------------------------------------------------


@mcp.tool(name="api.search")
def tool_api_search(
    query: str,
    max_results: int = 10,
    order: str = "relevance",
    published_after: str | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """Search YouTube through Data API v3 (1 Search Queries unit/call)."""
    return _api_search(query, max_results, order, published_after, channel_id)


@mcp.tool(name="api.channel_stats")
def tool_api_channel_stats(channel_id_or_handle: str) -> dict[str, Any]:
    """Return canonical channel statistics and metadata."""
    return _api_channel_stats(channel_id_or_handle)


@mcp.tool(name="api.trending")
def tool_api_trending(
    region: str = "US",
    category_id: str | None = None,
    n: int = 20,
) -> dict[str, Any]:
    """Return currently popular videos for a region/category."""
    return _api_trending(region, category_id, n)


@mcp.tool(name="api.video_categories")
def tool_api_video_categories(region: str = "US") -> dict[str, Any]:
    """List YouTube category IDs for a region."""
    return _api_video_categories(region)


log.info(
    "youtube-mcp-v2 ready — tier-1: inspect.video, transcript.get, scrape.search, "
    "skeleton.{build,list,get,expire,index}, frame.get, audio.get | "
    "tier-2: api.{search,channel_stats,trending,video_categories} | "
    "binary media resources registered"
)
