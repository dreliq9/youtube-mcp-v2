"""Shared pieces for local speech-to-text backends.

Every local STT backend (whisper.cpp, mlx-whisper, future engines) produces the
same `TranscriptFetch` shape plus a path-safe provenance record with one schema:

    schema    youtube-mcp.local-stt/1
    engine    backend name + compact version
    model     name, optional revision, size, content SHA-256 (never a path)
    decode    the decode configuration actually passed to the engine
    audio     SHA-256, sample rate, format, duration of the exact input audio
    segments  count and timing/confidence summary of the returned segments

The persisted transcript revision stores this record next to the segment
timings and the transcript validation state, so a corpus search hit can say
which model/audio/decode configuration produced a piece of evidence.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import wave
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from . import audio_extract

PROVENANCE_SCHEMA = "youtube-mcp.local-stt/1"
SAMPLE_RATE = 16000
ENV_LANGUAGE = "YOUTUBE_MCP_STT_LANGUAGE"
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?$")


class LocalSttError(RuntimeError):
    """Base local speech-to-text failure."""


class LocalSttUnavailable(LocalSttError):
    """Configured local STT cannot run because a required local component is absent."""


class LocalSttTimeout(LocalSttError):
    """A local STT engine exceeded its configured hard deadline."""


def env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def timeout_from_env(name: str, default: int) -> int:
    raw = env_value(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise LocalSttUnavailable(f"{name} must be an integer") from exc
    if value <= 0:
        raise LocalSttUnavailable(f"{name} must be > 0")
    return value


def language_hint() -> str | None:
    """Optional decode language (`None` means automatic language detection)."""
    raw = env_value(ENV_LANGUAGE)
    if raw is None or raw.lower() == "auto":
        return None
    if not _LANGUAGE_RE.fullmatch(raw):
        raise LocalSttUnavailable(
            f"{ENV_LANGUAGE} must be 'auto' or a language code such as 'en'"
        )
    return raw.lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=64)
def _cached_hash(path_string: str, size: int, mtime_ns: int) -> str:
    # size/mtime participate in the cache key so a replaced file at the same
    # path is rehashed; the path itself never leaves this process.
    del size, mtime_ns
    return sha256_file(Path(path_string))


def cached_file_sha256(path: Path) -> str:
    stat = path.stat()
    return _cached_hash(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def acquire_audio(video_id: str) -> Path:
    """Download/cache the 16 kHz mono WAV that every local engine transcribes."""
    try:
        audio = audio_extract.extract_audio(video_id, fmt="wav", sample_rate=SAMPLE_RATE)
    except audio_extract.AudioExtractError as exc:
        raise LocalSttError("audio acquisition for local STT failed") from exc
    path = Path(audio.path)
    if not path.is_file():
        raise LocalSttError("local STT audio artifact is unavailable")
    return path


def wav_duration_s(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as reader:
            frames = reader.getnframes()
            rate = reader.getframerate()
    except (wave.Error, EOFError, OSError):
        return None
    if rate <= 0:
        return None
    return round(frames / float(rate), 3)


def audio_identity(path: Path) -> dict[str, Any]:
    return {
        "sha256": sha256_file(path),
        "sample_rate": SAMPLE_RATE,
        "format": "wav",
        "duration_s": wav_duration_s(path),
    }


def _finite(values: Iterable[Any]) -> list[float]:
    out = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(number)
    return out


def segment_summary(
    segments: list[Any],
    *,
    avg_logprobs: Iterable[Any] = (),
    no_speech_probs: Iterable[Any] = (),
) -> dict[str, Any]:
    """Compact timing/confidence summary of engine segments (not per-segment data)."""
    starts = [float(segment.start_s) for segment in segments]
    ends = [float(segment.end_s) for segment in segments]
    summary: dict[str, Any] = {
        "count": len(segments),
        "first_start_s": round(min(starts), 3) if starts else None,
        "last_end_s": round(max(ends), 3) if ends else None,
        "speech_s": round(sum(max(0.0, e - s) for s, e in zip(starts, ends)), 3),
        "monotonic": all(b >= a for a, b in zip(starts, starts[1:])),
    }
    logprobs = _finite(avg_logprobs)
    if logprobs:
        summary["mean_avg_logprob"] = round(sum(logprobs) / len(logprobs), 4)
    no_speech = _finite(no_speech_probs)
    if no_speech:
        summary["max_no_speech_prob"] = round(max(no_speech), 4)
    return summary


def scrub_version(text: str | None) -> str | None:
    """Keep a compact first line and strip absolute filesystem paths."""
    if not text:
        return None
    first = text.strip().splitlines()[0].strip()
    first = re.sub(r"(?:[A-Za-z]:)?[/\\][^\s]+", "<path>", first)
    return first[:160] or None
