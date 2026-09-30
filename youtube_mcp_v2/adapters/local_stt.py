"""Local speech-to-text adapter interface and backend selection.

The transcript waterfall asks for "a local transcription" rather than naming an
engine. Backends implement `LocalSttBackend`; selection is configuration-only:

    YOUTUBE_MCP_STT_BACKEND = auto (default) | whisper.cpp | mlx-whisper | off

`auto` prefers mlx-whisper when its model is configured and the host is Apple
Silicon with the `stt-mlx` extra installed, then falls back to a configured
whisper.cpp model. With no model configured, local STT stays disabled and the
waterfall records no phantom attempt. No backend ever downloads a model.

The backend choice is operator configuration; it never appears as an MCP tool
parameter. It is recorded in transcript provenance (engine/model/decode/audio).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from . import mlx_stt, stt_common, whisper_cpp
from .stt_common import LocalSttError, LocalSttTimeout, LocalSttUnavailable
from .transcript_api import TranscriptFetch

ENV_BACKEND = "YOUTUBE_MCP_STT_BACKEND"
BACKEND_CHOICES = ("auto", "whisper.cpp", "mlx-whisper", "off")

__all__ = [
    "BACKEND_CHOICES",
    "ENV_BACKEND",
    "LocalSttBackend",
    "LocalSttError",
    "LocalSttResult",
    "LocalSttTimeout",
    "LocalSttUnavailable",
    "MlxWhisperBackend",
    "WhisperCppBackend",
    "backend_name_for_error",
    "resolve_backend",
]


class LocalSttResult(Protocol):
    fetch: TranscriptFetch
    provenance: dict[str, Any]


class LocalSttBackend(Protocol):
    """A local engine that turns a video's audio into timed transcript segments."""

    name: str

    def transcribe_video(self, video_id: str) -> LocalSttResult: ...


@dataclass(frozen=True)
class WhisperCppBackend:
    settings: whisper_cpp.WhisperCppSettings
    name: str = whisper_cpp.NAME

    def transcribe_video(self, video_id: str) -> LocalSttResult:
        # Module attribute lookup keeps whisper_cpp monkeypatchable in tests.
        return whisper_cpp.transcribe_video(video_id, settings=self.settings)


@dataclass(frozen=True)
class MlxWhisperBackend:
    settings: mlx_stt.MlxWhisperSettings
    name: str = mlx_stt.NAME

    def transcribe_video(self, video_id: str) -> LocalSttResult:
        return mlx_stt.transcribe_video(video_id, settings=self.settings)


def _tag(exc: LocalSttError, backend: str) -> LocalSttError:
    exc.stt_backend = backend  # type: ignore[attr-defined]
    return exc


def backend_name_for_error(exc: BaseException) -> str:
    """Which backend a resolution/transcription error belongs to (for attempts)."""
    return str(getattr(exc, "stt_backend", None) or whisper_cpp.NAME)


def _choice() -> str:
    raw = (stt_common.env_value(ENV_BACKEND) or "auto").lower()
    if raw not in BACKEND_CHOICES:
        raise _tag(
            LocalSttUnavailable(
                f"{ENV_BACKEND} must be one of: {', '.join(BACKEND_CHOICES)}"
            ),
            whisper_cpp.NAME,
        )
    return raw


def _whisper_cpp() -> WhisperCppBackend | None:
    try:
        settings = whisper_cpp.settings_from_env()
    except LocalSttError as exc:
        raise _tag(exc, whisper_cpp.NAME) from None
    return WhisperCppBackend(settings) if settings is not None else None


def _mlx() -> MlxWhisperBackend | None:
    try:
        settings = mlx_stt.settings_from_env()
        if settings is None:
            return None
        mlx_stt.ensure_runtime()
    except LocalSttError as exc:
        raise _tag(exc, mlx_stt.NAME) from None
    return MlxWhisperBackend(settings)


def resolve_backend() -> LocalSttBackend | None:
    """Return the configured local backend, `None` when local STT is disabled.

    Raises `LocalSttUnavailable` (tagged with the backend name) when local STT
    was explicitly configured but cannot run, so the waterfall can record a
    structured `unavailable` attempt instead of silently skipping it.
    """
    choice = _choice()
    if choice == "off":
        return None
    if choice == whisper_cpp.NAME:
        backend = _whisper_cpp()
        if backend is None:
            raise _tag(
                LocalSttUnavailable(
                    "whisper.cpp STT selected but YOUTUBE_MCP_WHISPER_CPP_MODEL is not set"
                ),
                whisper_cpp.NAME,
            )
        return backend
    if choice == mlx_stt.NAME:
        backend = _mlx()
        if backend is None:
            raise _tag(
                LocalSttUnavailable(
                    f"mlx-whisper STT selected but {mlx_stt.ENV_MODEL} is not set"
                ),
                mlx_stt.NAME,
            )
        return backend

    # auto: prefer MLX on Apple Silicon, fall back to portable whisper.cpp.
    mlx_error: LocalSttError | None = None
    try:
        mlx_backend = _mlx()
    except LocalSttError as exc:
        mlx_backend, mlx_error = None, exc
    if mlx_backend is not None:
        return mlx_backend
    cpp_backend = _whisper_cpp()
    if cpp_backend is not None:
        return cpp_backend
    if mlx_error is not None:
        raise mlx_error
    return None
