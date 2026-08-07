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

# Load .env from the project root (the dir containing pyproject.toml).
# override=False so an explicit env var from the client config wins.
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
                               downstream calls (transcripts, frames) against.
    USE WHEN target='topic':   frozen snapshot of search results for later
                               comparison or semantic search.
    DO NOT USE WHEN: you only need a one-shot search — call scrape.search.
    OUTPUT SHAPE: envelope wrapping {handle, target, value, source, count}.
                  Use skeleton.get / skeleton.list to read the contents.
    """
    return _skeleton_build(target, value, limit)


@mcp.tool(name="skeleton.list")
def tool_skeleton_list(handle: str, enrich: bool = True) -> dict[str, Any]:
    """List the videos in a skeleton.

    USE WHEN: iterating videos for downstream batch ops.
    DO NOT USE WHEN: you need build provenance/channel meta — use skeleton.get.
    OUTPUT SHAPE: envelope wrapping list of video entries; nullable fields
                  enriched from cache when enrich=True.
    """
    return _skeleton_list(handle, enrich=enrich)


@mcp.tool(name="skeleton.get")
def tool_skeleton_get(handle: str) -> dict[str, Any]:
    """Load the full skeleton snapshot.

    USE WHEN: you need build_at / source / channel meta or the raw frozen list.
    DO NOT USE WHEN: you only need the videos — use skeleton.list.
    OUTPUT SHAPE: envelope wrapping the full skeleton dict.
    """
    return _skeleton_get(handle)


@mcp.tool(name="skeleton.expire")
def tool_skeleton_expire(handle: str) -> dict[str, Any]:
    """Mark a skeleton stale. Does NOT delete (revision discipline).

    USE WHEN: signalling that downstream consumers should rebuild while
              preserving the old snapshot for diff/audit.
    DO NOT USE WHEN: you want to discard data — build a new handle and ignore
                     the old one instead.
    OUTPUT SHAPE: envelope wrapping {handle, expired_at}.
    """
    return _skeleton_expire(handle)


@mcp.tool(name="skeleton.index")
def tool_skeleton_index(target: str | None = None) -> dict[str, Any]:
    """List all skeletons on disk (summary view).

    USE WHEN: discovering existing skeletons before building a new one.
    DO NOT USE WHEN: you already know the handle.
    OUTPUT SHAPE: envelope wrapping list of skeleton summaries.
    """
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
    """Populate transcript cache for one bounded/resumable corpus batch.

    USE WHEN: a frozen corpus has many uncached transcripts and issuing one
              transcript.get call per video would waste agent turns/context.
    POLICY: 'missing' accepts any historical requested-language transcript;
            'fresh' reacquires stale requested-language rows.
    RESUME: pass next_cursor into the next call. Failures still advance the cursor;
            restart at cursor='0' later to retry only unresolved members because
            prior successes are then cache skips.
    OUTPUT SHAPE: envelope wrapping progress counters plus compact per-attempt
                  status/provenance. Transcript bodies are intentionally omitted.
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
    """Prepare an immutable local evidence index for a frozen skeleton/corpus.

    USE WHEN: you expect to ask multiple questions across a frozen video set and
              want retrieval without loading every transcript into model context.
    DOES NOT: fetch missing transcripts from YouTube. It indexes exact transcript
              revisions already present in the append-only cache and reports gaps.
    OUTPUT SHAPE: envelope wrapping corpus/index revision IDs, transcript coverage,
                  missing video IDs, chunk count, and backend/model identity.
    """
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
    """Retrieve timestamped evidence across a frozen corpus revision.

    USE WHEN: the answer may live anywhere across many already-cached video
              transcripts. Returns only top evidence chunks, not full transcripts.
    REPRODUCIBILITY: pass an explicit index_revision to pin the exact transcript
                     row IDs/hashes that were searched. Omitting it uses the newest
                     prepared index for the requested language.
    OUTPUT SHAPE: envelope wrapping {corpus_revision, index_revision, query,
                  coverage, hits[]}. Each hit includes timestamp URL/excerpt,
                  retrieval scores, transcript revision/hash/language/generated
                  status/validation, and chunk hash.
    """
    return _corpus_search(
        handle,
        query,
        top_k=top_k,
        lang=lang,
        index_revision=index_revision,
        validated_only=validated_only,
        auto_prepare=auto_prepare,
    )


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

    USE WHEN mode='single': you need a specific moment as an image (timestamp_s
                            required). For in-text references, captions, or
                            feeding to a vision model.
    USE WHEN mode='sheet':  at-a-glance view of a video (LitRPG review,
                            scene-skimming, content audit). Returns the tiled
                            sheet AND the individual frames.
    DO NOT USE WHEN: you only need text — call transcript.get instead.
                     For a full video download, this is not the right tool.
    OUTPUT SHAPE: envelope wrapping
                  single → {path, timestamp_s, cached}
                  sheet  → {path, layout, frame_timestamps, frame_paths, cached}.
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
    """Extract YouTube audio to a local file for downstream transcription.

    USE WHEN: another tool needs a local audio path, especially local speech or
              music-analysis tooling. Defaults to mono 22.05 kHz WAV.
    DO NOT USE WHEN: you only need words — call transcript.get instead.
    OUTPUT SHAPE: envelope wrapping {id, path, format, sample_rate, mono,
                  start_s, end_s, cached}.
    """
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
    """Search YouTube via the Data API v3.

    USE WHEN: YOUTUBE_API_KEY is set and you want clean structured results.
    DO NOT USE WHEN: no key — use scrape.search.
    OUTPUT SHAPE: envelope wrapping list of {id, title, channel, channel_id,
                  published_at, description_excerpt, url}.
    QUOTA: 1 unit in the Search Queries bucket; default allocation is 100 calls/day.
    """
    return _api_search(query, max_results, order, published_after, channel_id)


@mcp.tool(name="api.channel_stats")
def tool_api_channel_stats(channel_id_or_handle: str) -> dict[str, Any]:
    """Channel stats — subs, total views, video count, custom URL.

    USE WHEN: you need creator-size signals or canonical channel meta.
    DO NOT USE WHEN: you only need recent uploads — use skeleton.build.
    OUTPUT SHAPE: envelope wrapping channel record.
    QUOTA: 1 unit.
    """
    return _api_channel_stats(channel_id_or_handle)


@mcp.tool(name="api.trending")
def tool_api_trending(
    region: str = "US",
    category_id: str | None = None,
    n: int = 20,
) -> dict[str, Any]:
    """Trending videos for a region.

    USE WHEN: you want what's currently popular for content research.
    DO NOT USE WHEN: you have a specific topic — use api.search or scrape.search.
    OUTPUT SHAPE: envelope wrapping list of lean video records.
    QUOTA: 1 unit.
    """
    return _api_trending(region, category_id, n)


@mcp.tool(name="api.video_categories")
def tool_api_video_categories(region: str = "US") -> dict[str, Any]:
    """List YouTube category ids for a region.

    USE WHEN: you need a numeric category id for filtering trending/search.
    DO NOT USE WHEN: you don't care about category filters.
    OUTPUT SHAPE: envelope wrapping list of {id, title}.
    QUOTA: 1 unit.
    """
    return _api_video_categories(region)


log.info(
    "youtube-mcp-v2 ready — tier-1: inspect.video, transcript.get, scrape.search, "
    "skeleton.{build,list,get,expire,index}, corpus.{hydrate,prepare,search}, "
    "frame.get, audio.get | tier-2: "
    "api.{search,channel_stats,trending,video_categories}"
)
