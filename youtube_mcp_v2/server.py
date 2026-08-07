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

from . import visual_resources
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
from .tools.corpus_compose import corpus_compose as _corpus_compose
from .tools.corpus import (
    corpus_prepare as _corpus_prepare,
    corpus_search as _corpus_search,
)
from .tools.visual import corpus_visual_search as _corpus_visual_search
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

    USE WHEN: you have a URL/id and need capabilities (transcript? language?
    duration? age-gated?) before deciding which heavy tool to call next.
    DO NOT USE WHEN: you've already inspected this id this session.
    OUTPUT SHAPE: envelope wrapping { id, title, channel, duration_s, view_count,
                  publish_date, lang_default, available_caption_langs[],
                  has_transcript, age_gated, embed_allowed, livestream }.
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
    """Fetch a YouTube transcript in one of three shapes.

    USE WHEN mode='text': consumer just needs the words, no timing.
    USE WHEN mode='timed': consumer needs timestamps (jump to a moment, cut clips).
    USE WHEN mode='chunked': long transcript that won't fit a single LLM call.
    DO NOT USE: when you don't yet have a video id — call inspect.video first.
    OUTPUT SHAPE: depends on mode. text → {text, word_count}; timed → {segments,
                  next_cursor}; chunked → {chunks: [{i, n, start_s, end_s, text,
                  token_estimate}]}. All modes include requested_lang, actual lang,
                  and whether the caption track was generated when known.
    """
    return _transcript_get(
        url_or_id,
        mode=mode,
        lang=lang,
        cursor=cursor,
        chunk_tokens=chunk_tokens,
        chunk_overlap=chunk_overlap,
    )


# ---------------------------------------------------------------------------
# Tier 1 — search (no key)
# ---------------------------------------------------------------------------


@mcp.tool(name="scrape.search")
def tool_scrape_search(query: str, n: int = 10) -> dict[str, Any]:
    """Search YouTube via page scraping. No API key needed.

    USE WHEN: discovering videos by topic without a YOUTUBE_API_KEY.
    DO NOT USE WHEN: YOUTUBE_API_KEY is set — call api.search instead for richer
                     fields (channel ids, dates, no rate-limit quirks).
    OUTPUT SHAPE: envelope wrapping list of {id, title, channel, channel_id,
                  duration, views, published, url}.
    """
    return _scrape_search(query, n)


# ---------------------------------------------------------------------------
# Tier 1 — skeletons (frozen reference objects)
# ---------------------------------------------------------------------------


@mcp.tool(name="skeleton.build")
def tool_skeleton_build(
    target: Literal["channel", "topic"],
    value: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Build a frozen reference of videos for a channel or topic.

    USE WHEN target='channel': stable list of a creator's recent uploads to fan
                               downstream calls against.
    USE WHEN target='topic': frozen snapshot of search results for comparison or
                             later evidence retrieval.
    DO NOT USE WHEN: you only need a one-shot search — call scrape.search.
    For a deliberately mixed research set, use corpus.compose.
    """
    return _skeleton_build(target, value, limit)


@mcp.tool(name="skeleton.list")
def tool_skeleton_list(handle: str, enrich: bool = True) -> dict[str, Any]:
    """List the videos in a frozen skeleton/corpus revision."""
    return _skeleton_list(handle, enrich=enrich)


@mcp.tool(name="skeleton.get")
def tool_skeleton_get(handle: str) -> dict[str, Any]:
    """Load the full frozen snapshot and its capture/composition provenance."""
    return _skeleton_get(handle)


@mcp.tool(name="skeleton.expire")
def tool_skeleton_expire(handle: str) -> dict[str, Any]:
    """Mark a frozen snapshot stale without deleting its membership."""
    return _skeleton_expire(handle)


@mcp.tool(name="skeleton.index")
def tool_skeleton_index(target: str | None = None) -> dict[str, Any]:
    """Discover frozen channel/topic/collection snapshots on disk."""
    return _skeleton_index(target)


# ---------------------------------------------------------------------------
# Tier 1 — corpus composition and evidence retrieval
# ---------------------------------------------------------------------------


@mcp.tool(name="corpus.compose")
def tool_corpus_compose(
    label: str,
    videos: list[str] | None = None,
    include_handles: list[str] | None = None,
    base_handle: str | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    """Freeze an arbitrary heterogeneous research set without live acquisition.

    USE WHEN: relevant evidence spans creators, topics, disciplines, or previously
              frozen corpora rather than one channel/search result set.
    SOURCES: combine an optional base corpus, multiple include_handles, and direct
             video URLs/IDs; remove selected members in the same immutable revision.
    ORDER: base → included corpora → explicit videos → removals; duplicate IDs keep
           their first occurrence.
    DOES NOT: search YouTube, inspect videos, or mutate source corpora. New direct
              videos use cached metadata when available and can be hydrated later.
    """
    return _corpus_compose(
        label,
        videos=videos,
        include_handles=include_handles,
        base_handle=base_handle,
        remove=remove,
    )


@mcp.tool(name="corpus.prepare")
def tool_corpus_prepare(
    handle: str,
    lang: str = "en",
    chunk_tokens: int = 500,
    chunk_overlap: int = 50,
    semantic: Literal["off", "auto", "required"] = "auto",
) -> dict[str, Any]:
    """Prepare an immutable local transcript-evidence index for a frozen corpus."""
    return _corpus_prepare(
        handle,
        lang=lang,
        chunk_tokens=chunk_tokens,
        chunk_overlap=chunk_overlap,
        semantic=semantic,
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
    semantic: Literal["off", "auto", "required"] = "auto",
) -> dict[str, Any]:
    """Retrieve timestamped spoken evidence across a frozen corpus revision.

    Hybrid retrieval preserves lexical signals for exact evidence such as names,
    dates, quotations, statute/case identifiers, quantities, citations, prices,
    scientific notation, product/model identifiers, game/UI terms, and error codes,
    while semantic ranking handles paraphrases and conceptual similarity.
    """
    return _corpus_search(
        handle,
        query,
        top_k=top_k,
        lang=lang,
        index_revision=index_revision,
        validated_only=validated_only,
        auto_prepare=auto_prepare,
        semantic=semantic,
    )


@mcp.tool(name="corpus.visual_search")
def tool_corpus_visual_search(
    handle: str,
    query: str,
    top_k: int = 10,
    visual_index_revision: str | None = None,
    auto_prepare: bool = True,
    visual: Literal["auto", "required"] = "auto",
) -> dict[str, Any]:
    """Search timestamped on-screen evidence across already-cached corpus frames.

    USE WHEN: relevant evidence may be visible but not spoken — maps, charts,
              diagrams, slides, physical demonstrations, products, artwork, game/UI
              states, captions rendered into pixels, or other visual scenes.
    VISUAL: 'auto' uses paired text/image models only when already local and never
            downloads them; 'required' explicitly opts into model initialization.
    DOES NOT: download videos or create missing frames.
    """
    return _corpus_visual_search(
        handle,
        query,
        top_k=top_k,
        visual_index_revision=visual_index_revision,
        auto_prepare=auto_prepare,
        visual=visual,
    )


@mcp.resource(
    "youtube-mcp://evidence/frame/png/{sha256}",
    mime_type="image/png",
)
def resource_visual_png(sha256: str) -> bytes:
    """Read one frozen content-addressed PNG evidence frame."""
    return visual_resources.read(sha256, "png")


@mcp.resource(
    "youtube-mcp://evidence/frame/jpg/{sha256}",
    mime_type="image/jpeg",
)
def resource_visual_jpg(sha256: str) -> bytes:
    """Read one frozen content-addressed JPEG evidence frame."""
    return visual_resources.read(sha256, "jpg")


# ---------------------------------------------------------------------------
# Tier 1 — frame/audio extraction (yt-dlp + ffmpeg)
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
    """Extract one frame or a contact sheet from a YouTube video.

    USE WHEN mode='single': inspect a specific timestamp as an image.
    USE WHEN mode='sheet': scene-skimming across documentaries, lectures, reviews,
                           tutorials, interviews, performances, games, or other video.
    DO NOT USE WHEN: you only need text — call transcript.get instead.
    """
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
    """Extract YouTube audio for downstream speech, music, or acoustic analysis."""
    return _audio_get(
        url_or_id,
        fmt=fmt,
        sample_rate=sample_rate,
        start_s=start_s,
        end_s=end_s,
    )


# ---------------------------------------------------------------------------
# Tier 2 — Data API v3 (BYO YOUTUBE_API_KEY)
# ---------------------------------------------------------------------------


@mcp.tool(name="api.search")
def tool_api_search(
    query: str,
    max_results: int = 10,
    order: str = "relevance",
    published_after: str | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """Search YouTube via the Data API v3. QUOTA: 1 Search Queries unit."""
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
    "skeleton.{build,list,get,expire,index}, "
    "corpus.{compose,prepare,search,visual_search}, frame.get, audio.get | tier-2: "
    "api.{search,channel_stats,trending,video_categories}"
)
