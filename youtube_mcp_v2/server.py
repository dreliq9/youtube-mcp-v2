"""MCPServer entrypoint. Registers namespaced YouTube research tools."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server import MCPServer

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env", override=False)

from . import artifacts, visual_resources
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
    corpus_hydrate as _corpus_hydrate,
    corpus_prepare as _corpus_prepare,
    corpus_search as _corpus_search,
)
from .tools.visual import corpus_visual_search as _corpus_visual_search
from .tools.clip_plan import corpus_clip_plan as _corpus_clip_plan
from .tools.frame import frame_get as _frame_get
from .tools.audio import audio_get as _audio_get
from .tools.materialize import media_materialize as _media_materialize
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
    """Fetch a YouTube transcript in text, timed, or chunked form."""
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
    """Search YouTube via page scraping. No API key needed."""
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
    """Build a frozen reference of videos for a channel or topic.

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
# Tier 1 — corpus composition, hydration, retrieval, and edit planning
# ---------------------------------------------------------------------------


@mcp.tool(name="corpus.compose")
def tool_corpus_compose(
    label: str,
    videos: list[str] | None = None,
    include_handles: list[str] | None = None,
    base_handle: str | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    """Freeze an arbitrary heterogeneous research set without live acquisition."""
    return _corpus_compose(
        label,
        videos=videos,
        include_handles=include_handles,
        base_handle=base_handle,
        remove=remove,
    )


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
    """Retrieve timestamped spoken evidence across a frozen corpus revision."""
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
    """Search timestamped on-screen evidence across already-cached corpus frames."""
    return _corpus_visual_search(
        handle,
        query,
        top_k=top_k,
        visual_index_revision=visual_index_revision,
        auto_prepare=auto_prepare,
        visual=visual,
    )


@mcp.tool(name="corpus.clip_plan")
def tool_corpus_clip_plan(
    handle: str,
    query: str,
    target_duration_s: float = 60.0,
    max_clips: int = 8,
    min_clip_duration_s: float = 4.0,
    max_clip_duration_s: float = 20.0,
    context_before_s: float = 2.0,
    context_after_s: float = 2.0,
    lang: str = "en",
    validated_only: bool = False,
    modalities: Literal["auto", "transcript", "visual", "both"] = "auto",
) -> dict[str, Any]:
    """Create an immutable editor-neutral shot/clip plan from corpus evidence.

    USE WHEN: the goal is to turn retrieved moments into a highlight reel, montage,
              documentary assembly, explainer, comparison, or other remix workflow.
    DOES NOT: download source video or edit/render anything. It freezes ranked source
              ranges and evidence provenance. Call media.materialize explicitly next.
    """
    return _corpus_clip_plan(
        handle,
        query,
        target_duration_s=target_duration_s,
        max_clips=max_clips,
        min_clip_duration_s=min_clip_duration_s,
        max_clip_duration_s=max_clip_duration_s,
        context_before_s=context_before_s,
        context_after_s=context_after_s,
        lang=lang,
        validated_only=validated_only,
        modalities=modalities,
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
# Tier 1 — media extraction / explicit editor materialization
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
    """Extract one frame or a contact sheet from a YouTube video."""
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
    """Extract YouTube audio for downstream speech, music, or acoustic analysis."""
    return _audio_get(
        url_or_id,
        fmt=fmt,
        sample_rate=sample_rate,
        start_s=start_s,
        end_s=end_s,
    )


@mcp.tool(name="media.materialize")
def tool_media_materialize(
    plan_revision: str,
    max_height: Literal[360, 480, 720, 1080] = 720,
    clip_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Materialize planned source ranges as editor-ready local MP4 assets.

    HEAVY/NETWORK BOUNDARY: this is the explicit acquisition step. Each source video
    is downloaded at most once per call, selected ranges are frame-accurately
    re-encoded, and output SHA-256/source provenance are recorded. The resulting
    manifest is designed for handoff to local editing MCPs such as Declip/FCP-MCP.
    """
    return _media_materialize(
        plan_revision,
        max_height=max_height,
        clip_ids=clip_ids,
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
    "corpus.{compose,hydrate,prepare,search,visual_search,clip_plan}, "
    "frame.get, audio.get, media.materialize | tier-2: "
    "api.{search,channel_stats,trending,video_categories} | binary media resources registered"
)
