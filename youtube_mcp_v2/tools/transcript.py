"""transcript.get — unified transcript retrieval with three modes.

mode='text'    → flat string (best for LLM consumption when timing isn't needed)
mode='timed'   → segments with timestamps (jump to specific moments, build clip lists)
mode='chunked' → token-budgeted chunks with overlap (for very long videos)
"""

from __future__ import annotations

import json
from typing import Any, Literal

from .. import cache, envelope, transcript_acquisition, validate
from ..adapters import transcript_api
from ..adapters.url import parse_video_id


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
            "n": -1,
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
        i = max(rewind, i + 1)

    n = len(chunks)
    for chunk in chunks:
        chunk["n"] = n

    return {"id": video_id, "lang": lang, "chunks": chunks}


def _resolve_duration(video_id: str) -> int | float | None:
    """Resolve duration so the word-rate integrity check can actually run."""
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
        pass
    return None


def _safe_json_object(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _cache_provenance(row: dict[str, Any]) -> dict[str, Any] | None:
    provider = row.get("provider")
    method = row.get("method")
    attempts_raw = row.get("attempts_json")
    details_raw = row.get("details_json")
    if provider is None and method is None and not attempts_raw and not details_raw:
        # Historical v0.2 row: do not fabricate provenance that was never stored.
        return None
    try:
        attempts = json.loads(attempts_raw) if attempts_raw else []
    except json.JSONDecodeError:
        attempts = []
    if not isinstance(attempts, list):
        attempts = []

    acquisition: dict[str, Any] = {
        "provider": provider,
        "method": method,
        "attempts": attempts,
    }
    details = _safe_json_object(details_raw)
    if details is not None:
        acquisition["details"] = details
    return {"acquisition": acquisition}


def _failure_envelope(
    video_id: str,
    lang: str,
    exc: transcript_acquisition.TranscriptAcquisitionFailed,
) -> dict[str, Any]:
    primary = exc.primary_error

    # If local STT was explicitly configured and actually attempted but failed,
    # caption absence/disablement is no longer the definitive terminal condition.
    # The user may be able to repair a missing binary/model/audio path and retry.
    if exc.local_stt_error is not None:
        return envelope.fail(
            "transcript_fetch_failed",
            "caption providers and configured local STT failed; see provenance attempts",
            recoverable=True,
            provenance=exc.provenance,
        )

    if isinstance(primary, transcript_api.TranscriptsDisabled):
        return envelope.fail(
            "transcripts_disabled",
            f"transcripts are disabled for {video_id}",
            recoverable=False,
            provenance=exc.provenance,
        )
    if isinstance(primary, transcript_api.VideoUnavailable):
        return envelope.fail(
            "video_unavailable",
            f"video {video_id} is unavailable to configured providers",
            recoverable=False,
            provenance=exc.provenance,
        )
    if isinstance(primary, transcript_api.NoTranscriptFound):
        return envelope.fail(
            "no_transcript",
            f"no transcript for {video_id} in {lang} or any configured fallback",
            recoverable=False,
            provenance=exc.provenance,
        )
    return envelope.fail(
        "transcript_fetch_failed",
        "all configured transcript acquisition providers failed; see provenance attempts",
        recoverable=True,
        provenance=exc.provenance,
    )


def transcript_get(
    url_or_id: str,
    mode: Literal["text", "timed", "chunked"] = "text",
    lang: str = "en",
    cursor: str | None = None,
    chunk_tokens: int = 500,
    chunk_overlap: int = 50,
) -> dict[str, Any]:
    """Fetch a YouTube transcript in one of three shapes.

    Live acquisition is provider-independent: caption providers are tried first;
    an explicitly configured local whisper.cpp model is the final fallback.
    Provenance records every attempted provider and local model/audio identity.
    """
    try:
        video_id = parse_video_id(url_or_id)
    except ValueError as exc:
        return envelope.fail("bad_url", str(exc), recoverable=False)

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

    cache_hit = cache.get_transcript(video_id, lang)
    if cache_hit is not None:
        row, age = cache_hit
        segs = json.loads(row["segments_json"]) if row.get("segments_json") else []
        warnings = json.loads(row["warnings_json"]) if row.get("warnings_json") else []
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
            provenance=_cache_provenance(row),
        )

    try:
        acquired = transcript_acquisition.acquire_transcript(video_id, lang=lang)
    except transcript_acquisition.TranscriptAcquisitionFailed as exc:
        return _failure_envelope(video_id, lang, exc)

    fetch = acquired.fetch
    segments = [
        {
            "start_s": segment.start_s,
            "duration_s": segment.duration_s,
            "end_s": segment.end_s,
            "text": segment.text,
        }
        for segment in fetch.segments
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
    if acquired.provider != "youtube-transcript-api":
        warnings = [
            *warnings,
            f"transcript fallback used: {acquired.provider}",
        ]

    attempts = [attempt.as_dict() for attempt in acquired.attempts]
    cache.put_transcript(
        video_id=video_id,
        lang=lang,
        actual_lang=fetch.lang,
        is_generated=fetch.is_generated,
        provider=acquired.provider,
        method=acquired.method,
        attempts=attempts,
        details=acquired.details,
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
        provenance=acquired.provenance,
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
