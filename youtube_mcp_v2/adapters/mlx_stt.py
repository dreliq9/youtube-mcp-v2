"""Optional mlx-whisper speech-to-text backend for Apple Silicon.

Like whisper.cpp this backend is configuration-only and never downloads a
model: `YOUTUBE_MCP_MLX_WHISPER_MODEL` must point at a local MLX Whisper model
directory (for example a Hugging Face snapshot of
`mlx-community/whisper-large-v3-turbo`) containing `config.json` and weights.

The engine runs in a spawned worker through `isolation.run_isolated`, so a hung
or crashing decode is terminated at `YOUTUBE_MCP_MLX_WHISPER_TIMEOUT_S` instead
of wedging the MCP server. Install with the `stt-mlx` extra (macOS arm64 only).
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import platform
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from .. import isolation
from . import stt_common
from .stt_common import LocalSttError, LocalSttTimeout, LocalSttUnavailable
from .transcript_api import Segment, TranscriptFetch

NAME = "mlx-whisper"
ENV_MODEL = "YOUTUBE_MCP_MLX_WHISPER_MODEL"
ENV_TIMEOUT = "YOUTUBE_MCP_MLX_WHISPER_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 3600
WEIGHT_FILES = ("model.safetensors", "weights.safetensors", "weights.npz")
_WORKER = "youtube_mcp_v2.adapters.mlx_stt.transcribe_worker"

# Mirrors mlx_whisper.transcribe defaults; recorded verbatim in provenance so a
# later default change upstream cannot silently alter what was persisted.
DEFAULT_DECODE: dict[str, Any] = {
    "task": "transcribe",
    "temperature": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    "compression_ratio_threshold": 2.4,
    "logprob_threshold": -1.0,
    "no_speech_threshold": 0.6,
    "condition_on_previous_text": True,
    "word_timestamps": False,
}


@dataclass(frozen=True)
class MlxWhisperSettings:
    model_dir: Path
    timeout_s: int
    language: str | None = None


@dataclass(frozen=True)
class MlxWhisperResult:
    fetch: TranscriptFetch
    provenance: dict[str, Any]


def _weights_file(model_dir: Path) -> Path | None:
    for name in WEIGHT_FILES:
        candidate = model_dir / name
        if candidate.is_file():
            return candidate
    return None


def settings_from_env() -> MlxWhisperSettings | None:
    """Resolve settings; `None` when the backend is not configured (opt-in)."""
    raw = stt_common.env_value(ENV_MODEL)
    if raw is None:
        return None
    model_dir = Path(raw).expanduser()
    if not model_dir.is_dir():
        # A repo id or missing path would make mlx-whisper download weights.
        raise LocalSttUnavailable(
            "configured mlx-whisper model directory does not exist "
            "(models are never downloaded implicitly)"
        )
    if not (model_dir / "config.json").is_file() or _weights_file(model_dir) is None:
        raise LocalSttUnavailable(
            "configured mlx-whisper model directory is missing config.json or weights"
        )
    return MlxWhisperSettings(
        model_dir=model_dir,
        timeout_s=stt_common.timeout_from_env(ENV_TIMEOUT, DEFAULT_TIMEOUT_S),
        language=stt_common.language_hint(),
    )


def is_supported_platform() -> bool:
    return platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}


def ensure_runtime() -> None:
    if not is_supported_platform():
        raise LocalSttUnavailable("mlx-whisper requires macOS on Apple Silicon")
    if importlib.util.find_spec("mlx_whisper") is None:
        raise LocalSttUnavailable(
            "mlx-whisper backend requires the optional 'stt-mlx' extra"
        )


def _model_name_and_revision(model_dir: Path) -> tuple[str, str | None]:
    # Hugging Face cache layout: models--<org>--<name>/snapshots/<commit>
    resolved = model_dir.resolve()
    if resolved.parent.name == "snapshots" and resolved.parent.parent.name.startswith("models--"):
        repo = resolved.parent.parent.name[len("models--"):].replace("--", "/")
        return repo, resolved.name
    return resolved.name, None


def model_identity(model_dir: Path) -> dict[str, Any]:
    weights = _weights_file(model_dir)
    files = [model_dir / "config.json"] + ([weights] if weights is not None else [])
    entries = [
        {
            "name": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": stt_common.cached_file_sha256(path),
        }
        for path in files
    ]
    digest = hashlib.sha256(
        "\n".join(f"{e['name']}:{e['sha256']}" for e in entries).encode("utf-8")
    ).hexdigest()
    name, revision = _model_name_and_revision(model_dir)
    return {
        "name": name,
        "revision": revision,
        "format": "mlx",
        "size_bytes": sum(e["size_bytes"] for e in entries),
        # Digest over the per-file digests of config.json + weights.
        "sha256": digest,
        "files": entries,
    }


def _engine_version() -> str | None:
    parts = []
    for dist in ("mlx-whisper", "mlx"):
        try:
            parts.append(f"{dist} {metadata.version(dist)}")
        except metadata.PackageNotFoundError:
            continue
    return ", ".join(parts) or None


def decode_config(settings: MlxWhisperSettings) -> dict[str, Any]:
    return {**DEFAULT_DECODE, "language": settings.language or "auto", "sample_rate": stt_common.SAMPLE_RATE}


def transcribe_worker(audio_path: str, model_dir: str, decode: dict[str, Any]) -> dict[str, Any]:
    """Runs inside the isolated child: call mlx_whisper and return plain JSON."""
    # Belt and braces: the model is a verified local directory, so the Hub
    # client must never reach the network from this process.
    os.environ["HF_HUB_OFFLINE"] = "1"
    import mlx_whisper  # optional extra; imported only in the worker

    options = dict(decode)
    options.pop("sample_rate", None)
    language = options.pop("language", "auto")
    temperature = tuple(options.pop("temperature"))
    result = mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo=model_dir,
        verbose=None,
        temperature=temperature,
        language=None if language == "auto" else language,
        **options,
    )
    return {
        "language": result.get("language"),
        "segments": [
            {
                "start": float(seg.get("start", 0.0)),
                "end": float(seg.get("end", 0.0)),
                "text": str(seg.get("text") or ""),
                "avg_logprob": seg.get("avg_logprob"),
                "no_speech_prob": seg.get("no_speech_prob"),
            }
            for seg in result.get("segments") or []
        ],
    }


def _run_engine(audio_path: Path, settings: MlxWhisperSettings, decode: dict[str, Any]) -> dict[str, Any]:
    try:
        return isolation.run_isolated(
            _WORKER,
            str(audio_path),
            str(settings.model_dir),
            decode,
            timeout_s=settings.timeout_s,
        )
    except TimeoutError as exc:
        raise LocalSttTimeout(
            f"mlx-whisper exceeded the {settings.timeout_s}s local STT deadline"
        ) from exc
    except (RuntimeError, ValueError, OSError) as exc:
        # Worker diagnostics can contain model/audio paths; never forward them.
        raise LocalSttError("mlx-whisper transcription failed") from exc


def parse_output(payload: dict[str, Any]) -> tuple[TranscriptFetch, dict[str, Any]]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise LocalSttError("mlx-whisper output has no segments array")
    segments: list[Segment] = []
    logprobs: list[Any] = []
    no_speech: list[Any] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalSttError("mlx-whisper segment is missing numeric timings") from exc
        if start < 0 or end < start:
            raise LocalSttError("mlx-whisper segment timings are invalid")
        segments.append(Segment(start_s=start, duration_s=end - start, text=text))
        logprobs.append(item.get("avg_logprob"))
        no_speech.append(item.get("no_speech_prob"))
    if not segments:
        raise LocalSttError("mlx-whisper returned no non-empty transcript segments")
    language = str(payload.get("language") or "und").strip() or "und"
    fetch = TranscriptFetch(segments=segments, lang=language, is_generated=True)
    summary = stt_common.segment_summary(
        segments, avg_logprobs=logprobs, no_speech_probs=no_speech
    )
    return fetch, summary


def transcribe_video(
    video_id: str,
    *,
    settings: MlxWhisperSettings | None = None,
) -> MlxWhisperResult:
    resolved = settings if settings is not None else settings_from_env()
    if resolved is None:
        raise LocalSttUnavailable("local mlx-whisper STT is not configured")
    ensure_runtime()
    audio_path = stt_common.acquire_audio(video_id)
    audio = stt_common.audio_identity(audio_path)
    decode = decode_config(resolved)
    payload = _run_engine(audio_path, resolved, decode)
    if not isinstance(payload, dict):
        raise LocalSttError("mlx-whisper returned a non-object result")
    fetch, summary = parse_output(payload)
    provenance = {
        "schema": stt_common.PROVENANCE_SCHEMA,
        "engine": NAME,
        "engine_version": _engine_version(),
        "model": model_identity(resolved.model_dir),
        "decode": decode,
        "audio": audio,
        "segments": summary,
    }
    return MlxWhisperResult(fetch=fetch, provenance=provenance)
