"""MCPServer entrypoint. Registers v0.2.1 tools with namespaced names.

Hard-cut migration: this server replaces the v0.1 `youtube` MCP entry. Tool names
are intentionally NOT backwards-compatible — the LLM should learn the new namespace.

Loads YOUTUBE_API_KEY from a project-local .env if present (BYO key — file is
gitignored). The client config env block also works and takes precedence.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server import MCPServer

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env", override=False)

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
from .tools.corpus import (
    corpus_hydrate as _corpus_hydrate,
    corpus_prepare as _corpus_prepare,
    corpus_search as _corpus_search,
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


@mcp.tool(name="inspect.video")
def tool_inspect_video(url_or_id: str) -> dict[str, Any]:
    """Pre-flight check on a YouTube video.

    USE WHEN: you have a URL/id and need capabilities before deciding which heavy
    tool to call next.
    """
    return _inspect_video(url_or_id)


@mcp.tool(name="transcript.get")
def tool_transcript_get(
    url_or_id: str,
    mode: Literal["text", "timed", "chunked"] = "text",
    lang: str = "en",
    cursor: str | None = None,
    chunk_tokens: int = 500,
    chunk_overlap: int = 50,
) -> dict[str, Any]:
    """Fetch a YouTube transcript as flat text, timed segments, or chunks."""
    return _transcript_get(
        url_or_id,
        mode=mode,
        lang=lang,
        cursor=cursor,
        chunk_tokens=chunk_tokens,
        chunk_overlap=chunk_overlap,
    )


@mcp.tool(name="scrape.search")
def tool_scrape_search(query: str, n: int = 10) -> dict[str, Any]:
    """Search YouTube via page scraping when no Data API key is available."""
    return _scrape_search(query, n)


@mcp.tool(name="skeleton.build")
def tool_skeleton_build(
    target: Literal["channel", "topic"],
    value: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Build a frozen reference of videos for a channel or topic."""
    return _skeleton_build(target, value, limit)


@mcp.tool(name="skeleton.list")
def tool_skeleton_list(handle: str, enrich: bool = True) -> dict[str, Any]:
    """List videos in a frozen skeleton/corpus revision."""
    return _skeleton_list(handle, enrich=enrich)


@mcp.tool(name="skeleton.get")
def tool_skeleton_get(handle: str) -> dict[str, Any]:
    """Load the full frozen skeleton snapshot and provenance."""
    return _skeleton_get(handle)


@mcp.tool(name="skeleton.expire")
def tool_skeleton_expire(handle: str) -> dict[str, Any]:
    """Mark a skeleton stale without deleting its frozen membership."""
    return _skeleton_expire(handle)


@mcp.tool(name="skeleton.index")
def tool_skeleton_index(target: str | None = None) -> dict[str, Any]:
    """Discover existing frozen skeletons on disk."""
    return _skeleton_index(target)


# ---------------------------------------------------------------------------
# Tier 1 — frozen-corpus acquisition and evidence retrieval
# ---------------------------------------------------------------------------


@mcp.tool(name="corpus.hydrate")
def tool_corpus_hydrate(
    handle: str,
    lang: str = "en",
    cursor: str | None = None,
    batch_size: int = 8,
    max_workers: int = 3,
    policy: Literal["missing", "fresh"] = "missing",
) -> dict[str, Any]:
    """Populate cached transcripts for one bounded/resumable corpus batch.

    USE WHEN: a frozen corpus contains many videos whose transcripts have not yet
              been cached. This replaces one transcript.get call per video with a
              bounded batch operation.
    POLICY: 'missing' preserves any historical cached transcript revision; 'fresh'
            refetches members whose newest requested-language transcript is stale.
    RESUME: pass next_cursor into the next call. Failed acquisitions still advance
            the cursor; restart from cursor='0' later to retry only unresolved
            members because prior successes are cache skips.
    OUTPUT: compact per-attempt status/provenance only — transcript bodies are
            deliberately not returned into model context.
    """
    return _corpus_hydrate(
        handle,
        lang=lang,
        cursor=cursor,
        batch_size=batch_size,
        max_workers=max_workers,
        policy=policy,
    )


@mcp.tool(name="corpus.prepare")
def tool_corpus_prepare(
    handle: str,
    lang: str = "en",
    chunk_tokens: int = 500,
    chunk_overlap: int = 50,
) -> dict[str, Any]:
    """Prepare an immutable local evidence index from cached corpus transcripts."""
    return _corpus_prepare(
        handle,
        lang=lang,
        chunk_tokens=chunk_tokens,
        chunk_overlap=chunk_overlap,
    )


@mcp.tool(name="corpus.search")
def tool_corpus_search(
    handle: str,
    query: str,
    top_k: int = 10,
    lang: str = "en",
    index_revision: str | None = None,
    validated_only: bool = False,
    auto_prepare: bool = True,
) -> dict[str, Any]:
    """Retrieve a bounded set of timestamped evidence across a frozen corpus."""
    return _corpus_search(
        handle,
        query,
        top_k=top_k,
        lang=lang,
        index_revision=index_revision,
        validated_only=validated_only,
        auto_prepare=auto_prepare,
    )


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
    """Extract one frame or a contact sheet from a YouTube video."""
    return _frame_get(
        url_or_id, mode=mode, timestamp_s=timestamp_s,
        n=n, layout=layout, size=size, fmt=fmt,
    )


@mcp.tool(name="audio.get")
def tool_audio_get(
    url_or_id: str,
    fmt: Literal["wav", "m4a", "mp3", "flac", "ogg"] = "wav",
    sample_rate: int = 22050,
    start_s: float | None = None,
    end_s: float | None = None,
) -> dict[str, Any]:
    """Extract cached mono audio for downstream speech/audio analysis."""
    return _audio_get(
        url_or_id,
        fmt=fmt,
        sample_rate=sample_rate,
        start_s=start_s,
        end_s=end_s,
    )


@mcp.tool(name="api.search")
def tool_api_search(
    query: str,
    max_results: int = 10,
    order: str = "relevance",
    published_after: str | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """Search YouTube via Data API v3."""
    return _api_search(query, max_results, order, published_after, channel_id)


@mcp.tool(name="api.channel_stats")
def tool_api_channel_stats(channel_id_or_handle: str) -> dict[str, Any]:
    """Return channel statistics and canonical metadata."""
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
    "skeleton.{build,list,get,expire,index}, corpus.{hydrate,prepare,search}, "
    "frame.get, audio.get | tier-2: "
    "api.{search,channel_stats,trending,video_categories}"
)
