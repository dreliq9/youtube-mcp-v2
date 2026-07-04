"""audio.get — extract YouTube audio for downstream transcription."""

from __future__ import annotations

from typing import Any, Literal

from .. import envelope
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

    USE WHEN: a downstream transcription tool needs a local audio file path.
              Defaults to mono 22.05 kHz WAV for Basic Pitch compatibility.
    DO NOT USE WHEN: you only need captions — use transcript.get.
    OUTPUT SHAPE: envelope wrapping {id, path, format, sample_rate, mono,
                  start_s, end_s, cached}.
    """
    try:
        video_id = parse_video_id(url_or_id)
    except ValueError as e:
        return envelope.fail("bad_url", str(e), recoverable=False)

    try:
        res = audio_extract.extract_audio(
            video_id,
            fmt=fmt,
            sample_rate=sample_rate,
            start_s=start_s,
            end_s=end_s,
        )
    except AudioExtractError as e:
        return envelope.fail("audio_extract_failed", str(e))

    return envelope.ok(
        {
            "id": video_id,
            "path": res.path,
            "format": res.format,
            "sample_rate": res.sample_rate,
            "mono": res.mono,
            "start_s": res.start_s,
            "end_s": res.end_s,
            "cached": res.cached,
        },
        source="cache" if res.cached else "scrape",
    )
