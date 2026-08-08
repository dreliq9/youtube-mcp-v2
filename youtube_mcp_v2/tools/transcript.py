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
_VALID_MODES = {"text", "timed", "chunked"}


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
    """Cursor-based pagination over segments."""
    start_idx = 0
    if cursor is not None:
        try:
            start_idx = max(0, int(cursor))
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
    """Pack segments into token-budgeted chunks with overlap."""
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


def _resolve_duration(video_id: str) -> int | float | None:
    """Resolve duration for the word-rate validation gate.

    Prefer any cached metadata. If transcript.get was called directly without an
    earlier inspect.video, run the same cheap pre-flight once so the transcript
    does not receive a misleading `validated=True` merely because duration was
    absent from cache.
    """
    meta = cache.get_video_meta(video_id, fresh_only=False)
    if meta:
        duration = meta[0].get("duration_s")
        if duration:
            return duration

    try:
        from .inspect import inspect_video

        inspected = inspect_video(video_id)
        if inspected.get("error") is None and inspected.get("data"):
            return inspected["data"].get("duration_s")
    except Exception:
        # Validation handles an unresolved duration explicitly. Transcript
        # acquisition itself should still succeed when pre-flight metadata fails.
        pass
    return None


def transcript_get(
    url_or_id: str,
    mode: Literal["text", "timed", "chunked"] = "text",
    lang: str = "en",
    cursor: str | None = None,
    chunk_tokens: int = 500,
    chunk_overlap: int = 50,
) -> dict[str, Any]:
    """Fetch a YouTube transcript in one of three shapes."""
    try:
        video_id = parse_video_id(url_or_id)
    except ValueError as e:
        return envelope.fail("bad_url", str(e), recoverable=False)

    if mode not in _VALID_MODES:
        return envelope.fail(
            "bad_mode",
            f"mode must be one of {sorted(_VALID_MODES)}, got {mode!r}",
            recoverable=False,
        )
    if mode == "chunked":
        if chunk_tokens <= 0:
            return envelope.fail(
                "bad_chunk_tokens", "chunk_tokens must be > 0", recoverable=False
            )
        if chunk_overlap < 0 or chunk_overlap >= chunk_tokens:
            return envelope.fail(
                "bad_chunk_overlap",
                "chunk_overlap must be >= 0 and smaller than chunk_tokens",
                recoverable=False,
            )

    # Cache hit path: reshape the preserved transcript revision on the fly.
    cache_hit = cache.get_transcript(video_id, lang)
    if cache_hit is not None:
        row, age = cache_hit
        import json as _json

        segs = _json.loads(row["segments_json"]) if row.get("segments_json") else []
        warnings = _json.loads(row["warnings_json"]) if row.get("warnings_json") else []
        validated = bool(row["validated"])
        actual_lang = row.get("actual_lang") or lang
        raw_generated = row.get("is_generated")
        is_generated = None if raw_generated is None else bool(raw_generated)
        payload = _shape_payload(
            video_id,
            segs,
            requested_lang=lang,
            actual_lang=actual_lang,
            is_generated=is_generated,
            mode=mode,
            cursor=cursor,
            chunk_tokens=chunk_tokens,
            chunk_overlap=chunk_overlap,
        )
        return envelope.ok(
            payload,
            source="cache",
            cache_age_s=age,
            validated=validated,
            warnings=warnings,
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

    segments = [
        {
            "start_s": s.start_s,
            "duration_s": s.duration_s,
            "end_s": s.end_s,
            "text": s.text,
        }
        for s in fetch.segments
    ]

    duration_s = _resolve_duration(video_id)
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
        actual_lang=fetch.lang,
        is_generated=fetch.is_generated,
        text=text,
        segments=segments,
        word_count=len(text.split()),
        validated=validated,
        warnings=warnings,
    )

    payload = _shape_payload(
        video_id,
        segments,
        requested_lang=lang,
        actual_lang=fetch.lang,
        is_generated=fetch.is_generated,
        mode=mode,
        cursor=cursor,
        chunk_tokens=chunk_tokens,
        chunk_overlap=chunk_overlap,
    )
    return envelope.ok(
        payload,
        source="scrape",
        validated=validated,
        warnings=warnings,
    )


def _shape_payload(
    video_id: str,
    segments: list[dict],
    *,
    requested_lang: str,
    actual_lang: str,
    is_generated: bool | None,
    mode: str,
    cursor: str | None,
    chunk_tokens: int,
    chunk_overlap: int,
) -> dict[str, Any]:
    if mode == "text":
        payload = _build_text_payload(video_id, segments, actual_lang)
    elif mode == "timed":
        payload = _build_timed_payload(video_id, segments, actual_lang, cursor)
    else:
        payload = _build_chunked_payload(
            video_id, segments, actual_lang, chunk_tokens, chunk_overlap
        )

    payload["requested_lang"] = requested_lang
    payload["is_generated"] = is_generated
    return payload
