"""audio.get — extract YouTube audio for downstream transcription/analysis."""

from __future__ import annotations

from typing import Any, Literal

from .. import artifacts, envelope
from ..adapters import audio_extract
from ..adapters.audio_extract import AudioExtractError
from ..adapters.url import parse_video_id


def audio_get(
    url_or_id: str,
    fmt: Literal["wav", "m4a", "mp3", "flac", "ogg"] = "wav",
    sample_rate: int = 22050,
    start_s: float | None = None,
    end_s: float | None = None,
) -> dict[str, Any]:
    """Extract audio from a YouTube video.

    Same-host callers can use `path`. When the persisted artifact is within the
    portable resource bound, `resource_uri` lets remote MCP clients read the same
    bytes without sharing the server filesystem. For oversized full-length audio,
    request a bounded start/end clip.
    """
    try:
        video_id = parse_video_id(url_or_id)
    except ValueError as exc:
        return envelope.fail("bad_url", str(exc), recoverable=False)

    try:
        result = audio_extract.extract_audio(
            video_id,
            fmt=fmt,
            sample_rate=sample_rate,
            start_s=start_s,
            end_s=end_s,
        )
    except AudioExtractError as exc:
        return envelope.fail("audio_extract_failed", str(exc))

    warnings: list[str] = []
    try:
        reference = artifacts.reference_for_path(
            result.path,
            kind="audio",
            video_id=video_id,
        )
        reference_fields = reference.as_dict()
        if not reference.portable:
            warnings.append(
                "audio artifact exceeds portable MCP resource limit; request a shorter start/end clip for remote consumption"
            )
    except artifacts.ArtifactError as exc:
        reference_fields = {
            "resource_uri": None,
            "resource_mime_type": None,
            "resource_size_bytes": None,
            "resource_portable": False,
        }
        warnings.append(f"audio resource reference unavailable: {exc}")

    return envelope.ok(
        {
            "id": video_id,
            "path": result.path,
            "format": result.format,
            "sample_rate": result.sample_rate,
            "mono": result.mono,
            "start_s": result.start_s,
            "end_s": result.end_s,
            "cached": result.cached,
            **reference_fields,
        },
        source="cache" if result.cached else "scrape",
        warnings=warnings,
    )
