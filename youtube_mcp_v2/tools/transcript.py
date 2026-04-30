"""transcript.get — unified transcript retrieval with three modes.

mode='text'    → flat string (best for LLM consumption when timing isn't needed)
mode='timed'   → segments with timestamps (jump to specific moments, build clip lists)
mode='chunked' → token-budgeted chunks with overlap (for very long videos)
"""

from __future__ import annotations

from typing import Any, Literal

from .. import cache, envelope, validate
from ..adapters import transcript_api
from ..adapters.url import parse_video_id
from ..adapters.transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)


# Approx token estimate: ~4 chars/token for English. Good enough for budgeting.
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _segments_to_text(segments: list[dict]) -> str:
    return " ".join(s["text"] for s in segments).strip()


def _build_text_payload(
    video_id: str,
    segments: list[dict],
    lang: str,
) -> dict[str, Any]:
    text = _segments_to_text(segments)
    return {
        "id": video_id,
        "lang": lang,
        "text": text,
        "word_count": len(text.split()),
    }


def _build_timed_payload(
    video_id: str,
    segments: list[dict],
    lang: str,
    cursor: str | None,
    page_size: int = 200,
) -> dict[str, Any]:
    """Cursor-based pagination over segments.

    Borrowed from jkawamoto/mcp-youtube-transcript: long transcripts shouldn't blow
    a single tool response. Cursor encodes the next segment index to start at.
    """
    start_idx = 0
    if cursor is not None:
        try:
            start_idx = int(cursor)
        except (TypeError, ValueError):
            start_idx = 0

    end_idx = min(start_idx + page_size, len(segments))
    page = segments[start_idx:end_idx]
    next_cursor = str(end_idx) if end_idx < len(segments) else None

    return {
        "id": video_id,
        "lang": lang,
        "segments": page,
        "next_cursor": next_cursor,
        "total_segments": len(segments),
    }


def _build_chunked_payload(
    video_id: str,
    segments: list[dict],
    lang: str,
    chunk_tokens: int,
    chunk_overlap: int,
) -> dict[str, Any]:
    """Pack segments into token-budgeted chunks with overlap.

    Greedy fill: append segments until token budget is hit, then emit a chunk and
    rewind by `chunk_overlap` tokens worth of segments before starting the next.
    """
    chunks: list[dict[str, Any]] = []
    if not segments:
        return {"id": video_id, "lang": lang, "chunks": []}

    i = 0
    while i < len(segments):
        buf: list[dict] = []
        tokens = 0
        j = i
        while j < len(segments) and tokens < chunk_tokens:
            seg = segments[j]
            tokens += _estimate_tokens(seg["text"])
            buf.append(seg)
            j += 1

        if not buf:
            break

        chunk_text = " ".join(s["text"] for s in buf).strip()
        chunks.append({
            "i": len(chunks),
            "n": -1,  # filled in below
            "start_s": buf[0]["start_s"],
            "end_s": buf[-1]["start_s"] + buf[-1].get("duration_s", 0),
            "text": chunk_text,
            "token_estimate": _estimate_tokens(chunk_text),
        })

        if j >= len(segments):
            break

        # Rewind by overlap.
        overlap_tokens = 0
        rewind = j
        while rewind > i and overlap_tokens < chunk_overlap:
            rewind -= 1
            overlap_tokens += _estimate_tokens(segments[rewind]["text"])
        i = max(rewind, i + 1)  # ensure forward progress

    n = len(chunks)
    for c in chunks:
        c["n"] = n

    return {"id": video_id, "lang": lang, "chunks": chunks}


def transcript_get(
    url_or_id: str,
    mode: Literal["text", "timed", "chunked"] = "text",
    lang: str = "en",
    cursor: str | None = None,
    chunk_tokens: int = 500,
    chunk_overlap: int = 50,
) -> dict[str, Any]:
    """Fetch a YouTube transcript in one of three shapes.

    USE WHEN mode='text': consumer just needs the words, no timing.
    USE WHEN mode='timed': consumer needs timestamps to jump to moments or cut clips.
    USE WHEN mode='chunked': transcript is too long for a single LLM call (>~5K tokens).
    DO NOT USE: when you don't yet have a video id — call inspect.video or
                scrape.search first to confirm the video exists and has captions.
    OUTPUT SHAPE: depends on mode (see fields by mode in the docstring/spec).
    """
    try:
        video_id = parse_video_id(url_or_id)
    except ValueError as e:
        return envelope.fail("bad_url", str(e), recoverable=False)

    # Cache hit path: we keep segments + flat text in cache; reshape on the fly.
    cache_hit = cache.get_transcript(video_id, lang)
    if cache_hit is not None:
        row, age = cache_hit
        import json as _json
        segs = _json.loads(row["segments_json"]) if row.get("segments_json") else []
        warnings = _json.loads(row["warnings_json"]) if row.get("warnings_json") else []
        validated = bool(row["validated"])
        payload = _shape_payload(
            video_id, segs, lang, mode, cursor, chunk_tokens, chunk_overlap,
        )
        return envelope.ok(
            payload, source="cache", cache_age_s=age,
            validated=validated, warnings=warnings,
        )

    # Live fetch.
    try:
        fetch = transcript_api.fetch_transcript(video_id, lang=lang)
    except TranscriptsDisabled:
        return envelope.fail(
            "transcripts_disabled",
            f"transcripts are disabled for {video_id}",
            recoverable=False,
        )
    except VideoUnavailable:
        return envelope.fail(
            "video_unavailable",
            f"video {video_id} is unavailable (private, deleted, age-gated)",
            recoverable=False,
        )
    except NoTranscriptFound:
        return envelope.fail(
            "no_transcript",
            f"no transcript for {video_id} in {lang} or any fallback",
            recoverable=False,
        )
    except Exception as e:
        return envelope.fail("transcript_fetch_failed", str(e))

    # Convert to JSON-friendly segments.
    segments = [
        {"start_s": s.start_s, "duration_s": s.duration_s,
         "end_s": s.end_s, "text": s.text}
        for s in fetch.segments
    ]

    # Look up duration for validation.
    meta = cache.get_video_meta(video_id, fresh_only=False)
    duration_s = meta[0].get("duration_s") if meta else None

    text = _segments_to_text(segments)
    validated, warnings = validate.validate_transcript(
        text=text,
        segments=segments,
        duration_s=duration_s,
        requested_lang=lang,
        actual_lang=fetch.lang,
    )

    cache.put_transcript(
        video_id=video_id,
        lang=lang,
        text=text,
        segments=segments,
        word_count=len(text.split()),
        validated=validated,
        warnings=warnings,
    )

    payload = _shape_payload(
        video_id, segments, fetch.lang, mode, cursor, chunk_tokens, chunk_overlap,
    )
    return envelope.ok(
        payload, source="scrape",
        validated=validated, warnings=warnings,
    )


def _shape_payload(
    video_id: str,
    segments: list[dict],
    lang: str,
    mode: str,
    cursor: str | None,
    chunk_tokens: int,
    chunk_overlap: int,
) -> dict[str, Any]:
    if mode == "text":
        return _build_text_payload(video_id, segments, lang)
    if mode == "timed":
        return _build_timed_payload(video_id, segments, lang, cursor)
    if mode == "chunked":
        return _build_chunked_payload(
            video_id, segments, lang, chunk_tokens, chunk_overlap,
        )
    # Unknown mode — caller error, treat as text but flag.
    payload = _build_text_payload(video_id, segments, lang)
    payload["_warning"] = f"unknown mode {mode!r}, returned text"
    return payload
