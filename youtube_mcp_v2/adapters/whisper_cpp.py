"""Local whisper.cpp speech-to-text adapter.

This backend is deliberately configuration-only: youtube-mcp-v2 never downloads a
Whisper model implicitly. If a local model is configured, `transcript.get` can use
whisper.cpp as the final acquisition fallback when caption providers fail.

The adapter emits the same `TranscriptFetch` shape as caption providers while
returning separate, path-safe provenance for the local engine/model/audio inputs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import stt_common
from .stt_common import LocalSttError, LocalSttTimeout, LocalSttUnavailable
from .transcript_api import Segment, TranscriptFetch


_ENV_BIN = "YOUTUBE_MCP_WHISPER_CPP_BIN"
_ENV_MODEL = "YOUTUBE_MCP_WHISPER_CPP_MODEL"
_ENV_TIMEOUT = "YOUTUBE_MCP_WHISPER_CPP_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 3600
SAMPLE_RATE = 16000


# Exceptions live in stt_common so every local backend shares them; they are
# re-exported here for existing callers.
__all__ = [
    "LocalSttError",
    "LocalSttTimeout",
    "LocalSttUnavailable",
    "WhisperCppResult",
    "WhisperCppSettings",
    "settings_from_env",
    "transcribe_video",
]

NAME = "whisper.cpp"


@dataclass(frozen=True)
class WhisperCppSettings:
    binary: str
    model: Path
    timeout_s: int
    language: str | None = None  # None = automatic detection


@dataclass(frozen=True)
class WhisperCppResult:
    fetch: TranscriptFetch
    provenance: dict[str, Any]


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def settings_from_env() -> WhisperCppSettings | None:
    """Resolve local STT settings.

    A model path is the opt-in signal. If no model is configured, local STT is
    considered disabled and the acquisition waterfall does not add a third
    provider attempt. The binary may be explicit or discovered as `whisper-cli`.
    """
    model_raw = _env(_ENV_MODEL)
    if model_raw is None:
        return None

    model = Path(model_raw).expanduser()
    if not model.is_file():
        raise LocalSttUnavailable(
            "configured whisper.cpp model file does not exist"
        )

    binary = _env(_ENV_BIN) or shutil.which("whisper-cli")
    if not binary:
        raise LocalSttUnavailable(
            "whisper.cpp model is configured but whisper-cli is not available"
        )

    timeout_raw = _env(_ENV_TIMEOUT)
    if timeout_raw is None:
        timeout_s = DEFAULT_TIMEOUT_S
    else:
        try:
            timeout_s = int(timeout_raw)
        except ValueError as exc:
            raise LocalSttUnavailable(
                "YOUTUBE_MCP_WHISPER_CPP_TIMEOUT_S must be an integer"
            ) from exc
        if timeout_s <= 0:
            raise LocalSttUnavailable(
                "YOUTUBE_MCP_WHISPER_CPP_TIMEOUT_S must be > 0"
            )

    return WhisperCppSettings(
        binary=binary,
        model=model,
        timeout_s=timeout_s,
        language=stt_common.language_hint(),
    )


def _sha256_file(path: Path) -> str:
    return stt_common.sha256_file(path)


@lru_cache(maxsize=16)
def _model_hash(path_string: str, size: int, mtime_ns: int) -> str:
    # size/mtime participate in the cache key so a replaced model at the same
    # path is rehashed without ever exposing that local path in provenance.
    del size, mtime_ns
    return _sha256_file(Path(path_string))


def _model_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        # whisper.cpp models are single ggml files without an upstream revision
        # id; the content hash is the revision identity.
        "revision": None,
        "format": "ggml",
        "size_bytes": stat.st_size,
        "sha256": _model_hash(str(path.resolve()), stat.st_size, stat.st_mtime_ns),
    }


@lru_cache(maxsize=8)
def _engine_version(binary: str) -> str | None:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    combined = f"{completed.stdout}\n{completed.stderr}".strip()
    # Keep only a compact first line and strip obvious absolute filesystem paths.
    return stt_common.scrub_version(combined)


def _parse_output(payload: dict[str, Any]) -> TranscriptFetch:
    transcription = payload.get("transcription")
    if not isinstance(transcription, list):
        raise LocalSttError("whisper.cpp JSON has no transcription array")

    segments: list[Segment] = []
    for item in transcription:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        offsets = item.get("offsets") or {}
        try:
            start_ms = float(offsets["from"])
            end_ms = float(offsets["to"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalSttError(
                "whisper.cpp segment is missing numeric millisecond offsets"
            ) from exc
        if start_ms < 0 or end_ms < start_ms:
            raise LocalSttError("whisper.cpp segment offsets are invalid")
        segments.append(
            Segment(
                start_s=start_ms / 1000.0,
                duration_s=(end_ms - start_ms) / 1000.0,
                text=text,
            )
        )

    if not segments:
        raise LocalSttError("whisper.cpp returned no non-empty transcript segments")

    result = payload.get("result") or {}
    actual_lang = str(result.get("language") or "und").strip() or "und"
    return TranscriptFetch(
        segments=segments,
        lang=actual_lang,
        is_generated=True,
    )


def transcribe_video(
    video_id: str,
    *,
    settings: WhisperCppSettings | None = None,
) -> WhisperCppResult:
    """Download/cache 16 kHz mono audio and transcribe it locally with whisper.cpp."""
    resolved = settings if settings is not None else settings_from_env()
    if resolved is None:
        raise LocalSttUnavailable("local whisper.cpp STT is not configured")

    audio_path = stt_common.acquire_audio(video_id)
    audio = stt_common.audio_identity(audio_path)
    decode = decode_config(resolved)

    with tempfile.TemporaryDirectory(prefix="ytmcp-whisper-") as temp_dir:
        output_base = Path(temp_dir) / "transcript"
        command = [
            resolved.binary,
            "-m",
            str(resolved.model),
            "-f",
            str(audio_path),
            "-oj",
            "-of",
            str(output_base),
            "-np",
            "-l",
            decode["language"],
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=resolved.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalSttTimeout(
                f"whisper.cpp exceeded the {resolved.timeout_s}s local STT deadline"
            ) from exc
        except FileNotFoundError as exc:
            raise LocalSttUnavailable("configured whisper.cpp binary is unavailable") from exc
        except OSError as exc:
            raise LocalSttError("whisper.cpp could not be started") from exc

        if completed.returncode != 0:
            # Do not echo stderr: whisper.cpp diagnostics may contain model/audio
            # filesystem paths. Detailed local paths are intentionally not MCP data.
            raise LocalSttError(
                f"whisper.cpp exited with status {completed.returncode}"
            )

        json_path = output_base.with_suffix(".json")
        if not json_path.is_file():
            raise LocalSttError("whisper.cpp completed without JSON output")
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalSttError("whisper.cpp JSON output could not be parsed") from exc

    fetch = _parse_output(payload)
    provenance = {
        "schema": stt_common.PROVENANCE_SCHEMA,
        "engine": NAME,
        "engine_version": _engine_version(resolved.binary),
        "model": _model_identity(resolved.model),
        "decode": decode,
        "audio": audio,
        "segments": stt_common.segment_summary(fetch.segments),
    }
    return WhisperCppResult(fetch=fetch, provenance=provenance)


def decode_config(settings: WhisperCppSettings) -> dict[str, Any]:
    """The decode configuration passed to whisper-cli, recorded in provenance."""
    return {
        "language": settings.language or "auto",
        "task": "transcribe",
        "output": "json",
        "sample_rate": SAMPLE_RATE,
    }
