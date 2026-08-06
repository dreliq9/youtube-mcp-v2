"""Transcript acquisition orchestration.

The public transcript tool should ask for a transcript, not choose a fragile
provider. This module owns the acquisition waterfall while making every attempt
visible to provenance consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any

from .adapters import transcript_api, ytdlp_transcript
from .adapters.transcript_api import TranscriptFetch


@dataclass(frozen=True)
class AcquisitionAttempt:
    provider: str
    method: str
    duration_ms: int
    outcome: str  # success | unavailable | blocked | timeout | error
    detail_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "method": self.method,
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
            "detail_code": self.detail_code,
        }


@dataclass(frozen=True)
class AcquisitionResult:
    fetch: TranscriptFetch
    provider: str
    method: str
    attempts: list[AcquisitionAttempt]

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "acquisition": {
                "provider": self.provider,
                "method": self.method,
                "attempts": [attempt.as_dict() for attempt in self.attempts],
            }
        }


class TranscriptAcquisitionFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        attempts: list[AcquisitionAttempt],
        primary_error: Exception | None,
        fallback_error: Exception | None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.primary_error = primary_error
        self.fallback_error = fallback_error

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "acquisition": {
                "provider": None,
                "method": None,
                "attempts": [attempt.as_dict() for attempt in self.attempts],
            }
        }


def _elapsed_ms(started: float) -> int:
    return max(0, int((monotonic() - started) * 1000))


def _primary_failure_detail(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, transcript_api.TranscriptsDisabled):
        return "unavailable", "transcripts_disabled"
    if isinstance(exc, transcript_api.VideoUnavailable):
        return "unavailable", "video_unavailable"
    if isinstance(exc, transcript_api.NoTranscriptFound):
        return "unavailable", "no_transcript"

    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if (
        "blocked" in name
        or "ipblock" in name
        or "429" in message
        or "too many requests" in message
    ):
        return "blocked", "request_blocked"
    if isinstance(exc, TimeoutError) or "timeout" in name or "timed out" in message:
        return "timeout", "timeout"
    return "error", "provider_error"


def _fallback_failure_detail(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ytdlp_transcript.YtDlpUnavailable):
        return "unavailable", "missing_dependency"
    if isinstance(exc, ytdlp_transcript.YtDlpNoTranscript):
        return "unavailable", "no_transcript"
    if isinstance(exc, TimeoutError):
        return "timeout", "timeout"
    message = str(exc).lower()
    if "429" in message or "too many requests" in message or "sign in" in message:
        return "blocked", "request_blocked"
    return "error", "provider_error"


def acquire_transcript(video_id: str, lang: str = "en") -> AcquisitionResult:
    """Acquire captions through independent providers, preserving attempt history.

    Waterfall:
      1. youtube-transcript-api
      2. yt-dlp caption extraction

    Cache lookup remains above this layer because a cache hit should avoid all
    upstream acquisition. Local STT can be added as provider 3 without changing
    the public transcript tool contract.
    """
    attempts: list[AcquisitionAttempt] = []
    primary_error: Exception | None = None
    fallback_error: Exception | None = None

    started = monotonic()
    try:
        fetch = transcript_api.fetch_transcript(video_id, lang=lang)
    except Exception as exc:
        primary_error = exc
        outcome, detail = _primary_failure_detail(exc)
        attempts.append(
            AcquisitionAttempt(
                provider="youtube-transcript-api",
                method="captions",
                duration_ms=_elapsed_ms(started),
                outcome=outcome,
                detail_code=detail,
            )
        )
    else:
        attempts.append(
            AcquisitionAttempt(
                provider="youtube-transcript-api",
                method="captions",
                duration_ms=_elapsed_ms(started),
                outcome="success",
            )
        )
        return AcquisitionResult(
            fetch=fetch,
            provider="youtube-transcript-api",
            method="captions",
            attempts=attempts,
        )

    started = monotonic()
    try:
        fetch = ytdlp_transcript.fetch_transcript(video_id, lang=lang)
    except Exception as exc:
        fallback_error = exc
        outcome, detail = _fallback_failure_detail(exc)
        attempts.append(
            AcquisitionAttempt(
                provider="yt-dlp",
                method="captions",
                duration_ms=_elapsed_ms(started),
                outcome=outcome,
                detail_code=detail,
            )
        )
    else:
        attempts.append(
            AcquisitionAttempt(
                provider="yt-dlp",
                method="captions",
                duration_ms=_elapsed_ms(started),
                outcome="success",
            )
        )
        return AcquisitionResult(
            fetch=fetch,
            provider="yt-dlp",
            method="captions",
            attempts=attempts,
        )

    raise TranscriptAcquisitionFailed(
        "all transcript acquisition providers failed",
        attempts=attempts,
        primary_error=primary_error,
        fallback_error=fallback_error,
    )
