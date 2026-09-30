"""Transcript acquisition orchestration.

The public transcript tool should ask for a transcript, not choose a fragile
provider. This module owns the acquisition waterfall while making every attempt
visible to provenance consumers.

Waterfall (the fresh-cache stage lives in ``tools.transcript`` because a cache
hit must avoid all upstream/local work):

  1. native captions        youtube-transcript-api          (HTTP)
  2. alternate captions     yt-dlp, anonymous                (subprocess)
  3. authenticated captions yt-dlp + configured cookies      (only when cookies
                            are configured and stage 2 was blocked or needs a
                            signed-in/member session)
  4. local STT              whisper.cpp                      (only when a local
                            model path is explicitly configured)

Each stage is a :class:`TranscriptProvider`. The orchestrator adds bounded
retries for transient failures (timeouts / network errors) and a small
per-stage circuit breaker so repeated block/rate-limit responses stop hammering
an upstream that is actively refusing us. Attempts carry only machine-safe
fields: provider, method, duration, outcome, stable detail code and retry
index — never exception text, URLs, proxy credentials or cookie values.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable, Protocol, Sequence

from .adapters import transcript_api, whisper_cpp, ytdlp_transcript
from .adapters.transcript_api import TranscriptFetch

log = logging.getLogger("youtube-mcp-v2.acquisition")

# Stable outcome vocabulary.
OUTCOMES = ("success", "unavailable", "blocked", "timeout", "error", "skipped")

# Detail codes where a retry of the *same* stage is reasonable.
RETRYABLE_DETAILS = frozenset({"timeout", "network_error"})

# Detail codes where a cookie-authenticated session could plausibly succeed.
AUTH_RECOVERABLE_DETAILS = frozenset(
    {
        "request_blocked",
        "rate_limited",
        "login_required",
        "membership_required",
        "circuit_open",
    }
)

# Outcomes that feed the circuit breaker.
_BREAKER_OUTCOMES = frozenset({"blocked"})

# Indirection so tests can make backoff instantaneous and deterministic.
_sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class AcquisitionAttempt:
    provider: str
    method: str
    duration_ms: int
    outcome: str  # success | unavailable | blocked | timeout | error | skipped
    detail_code: str | None = None
    retry: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "method": self.method,
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
            "detail_code": self.detail_code,
            "retry": self.retry,
        }


PROVIDER_CLASSES = {
    "youtube-transcript-api": "scrape",
    "yt-dlp": "scrape",
    "whisper.cpp": "local",
}


def provider_class(provider: str | None) -> str | None:
    """Coarse provider class from EVIDENCE_MODEL.md (`scrape`, `local`, …)."""
    if provider is None:
        return None
    return PROVIDER_CLASSES.get(provider)


@dataclass(frozen=True)
class AcquisitionResult:
    fetch: TranscriptFetch
    provider: str
    method: str
    attempts: list[AcquisitionAttempt]
    details: dict[str, Any] | None = None

    @property
    def provenance(self) -> dict[str, Any]:
        acquisition: dict[str, Any] = {
            "provider": self.provider,
            "method": self.method,
            "provider_class": provider_class(self.provider),
            "attempts": [attempt.as_dict() for attempt in self.attempts],
        }
        if self.details is not None:
            acquisition["details"] = self.details
        return {"acquisition": acquisition}


class TranscriptAcquisitionFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        attempts: list[AcquisitionAttempt],
        primary_error: Exception | None,
        fallback_error: Exception | None,
        local_stt_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.primary_error = primary_error
        self.fallback_error = fallback_error
        self.local_stt_error = local_stt_error

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "acquisition": {
                "provider": None,
                "method": None,
                "provider_class": None,
                "attempts": [attempt.as_dict() for attempt in self.attempts],
            }
        }


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderOutput:
    fetch: TranscriptFetch
    details: dict[str, Any] | None = None


class TranscriptProvider(Protocol):
    """One stage of the acquisition waterfall.

    `provider`/`method` identify the stage in provenance. The orchestrator calls
    `is_eligible` (pure, based on earlier attempts), then `is_configured`
    (may read env; raising records a failed attempt), then `acquire`.
    """

    provider: str
    method: str
    retryable: bool

    def is_eligible(self, attempts: Sequence[AcquisitionAttempt]) -> bool: ...

    def is_configured(self) -> bool: ...

    def acquire(self, video_id: str, lang: str) -> ProviderOutput: ...

    def classify(self, exc: Exception) -> tuple[str, str]: ...


def _message(exc: BaseException) -> str:
    return str(exc).lower()


def _is_rate_limited(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 429:
        return True
    message = _message(exc)
    return "429" in message or "too many requests" in message


def _primary_failure_detail(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, transcript_api.TranscriptsDisabled):
        return "unavailable", "transcripts_disabled"
    if isinstance(exc, (transcript_api.VideoUnavailable, transcript_api.InvalidVideoId)):
        return "unavailable", "video_unavailable"
    if isinstance(exc, transcript_api.NoTranscriptFound):
        return "unavailable", "no_transcript"
    if isinstance(exc, transcript_api.AgeRestricted):
        return "unavailable", "login_required"
    if isinstance(exc, transcript_api.VideoUnplayable):
        return "unavailable", "video_unplayable"
    if isinstance(exc, transcript_api.PoTokenRequired):
        return "blocked", "po_token_required"
    if isinstance(exc, transcript_api.RequestBlocked):
        # Includes IpBlocked. The library raises it for HTTP 429 *and* for
        # reCAPTCHA/bot walls, and its message text mentions "too many
        # requests" either way, so it must be classified before heuristics.
        return "blocked", "request_blocked"
    if isinstance(
        exc, (transcript_api.TranscriptPayloadError, transcript_api.YouTubeDataUnparsable)
    ):
        return "error", "malformed_payload"
    if isinstance(exc, transcript_api.TranscriptConfigError):
        return "error", "config_error"
    if _is_rate_limited(exc):
        return "blocked", "rate_limited"

    name = type(exc).__name__.lower()
    message = _message(exc)
    if "blocked" in name or "ipblock" in name:
        return "blocked", "request_blocked"
    if isinstance(exc, TimeoutError) or "timeout" in name or "timed out" in message:
        return "timeout", "timeout"
    if "proxy" in name:
        return "error", "proxy_error"
    if "socks" in message and "missing dependencies" in message:
        return "error", "config_error"
    if isinstance(exc, ConnectionError) or "connectionerror" in name:
        return "error", "network_error"
    return "error", "provider_error"


def _fallback_failure_detail(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ytdlp_transcript.YtDlpUnavailable):
        return "unavailable", "missing_dependency"
    if isinstance(exc, ytdlp_transcript.YtDlpNoTranscript):
        return "unavailable", "no_transcript"
    if isinstance(exc, ytdlp_transcript.YtDlpMalformedPayload):
        return "error", "malformed_payload"
    if isinstance(exc, ytdlp_transcript.YtDlpConfigError):
        return "error", "config_error"
    if isinstance(exc, TimeoutError):
        return "timeout", "timeout"
    message = _message(exc)
    if any(
        marker in message
        for marker in (
            "members-only",
            "members only",
            "join this channel to get access",
        )
    ):
        return "unavailable", "membership_required"
    if any(
        marker in message
        for marker in ("confirm your age", "age-restricted", "age restricted")
    ):
        return "unavailable", "login_required"
    if "429" in message or "too many requests" in message:
        return "blocked", "rate_limited"
    if "sign in" in message or "not a bot" in message:
        return "blocked", "request_blocked"
    if "timed out" in message:
        return "timeout", "timeout"
    if any(
        marker in message
        for marker in (
            "urlopen error",
            "connection reset",
            "connection refused",
            "name resolution",
            "network is unreachable",
        )
    ):
        return "error", "network_error"
    return "error", "provider_error"


def _local_stt_failure_detail(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, whisper_cpp.LocalSttUnavailable):
        return "unavailable", "local_stt_unavailable"
    if isinstance(exc, whisper_cpp.LocalSttTimeout):
        return "timeout", "local_stt_timeout"
    return "error", "local_stt_failed"


def _has_detail(attempts: Sequence[AcquisitionAttempt], detail: str) -> bool:
    return any(attempt.detail_code == detail for attempt in attempts)


class NativeCaptionsProvider:
    provider = "youtube-transcript-api"
    method = "captions"
    retryable = True

    def is_eligible(self, attempts: Sequence[AcquisitionAttempt]) -> bool:
        return True

    def is_configured(self) -> bool:
        return True

    def acquire(self, video_id: str, lang: str) -> ProviderOutput:
        return ProviderOutput(fetch=transcript_api.fetch_transcript(video_id, lang=lang))

    def classify(self, exc: Exception) -> tuple[str, str]:
        return _primary_failure_detail(exc)


class YtDlpCaptionsProvider:
    provider = "yt-dlp"
    method = "captions"
    retryable = True

    def is_eligible(self, attempts: Sequence[AcquisitionAttempt]) -> bool:
        return True

    def is_configured(self) -> bool:
        return True

    def acquire(self, video_id: str, lang: str) -> ProviderOutput:
        return ProviderOutput(
            fetch=ytdlp_transcript.fetch_transcript(video_id, lang=lang, cookies=False)
        )

    def classify(self, exc: Exception) -> tuple[str, str]:
        return _fallback_failure_detail(exc)


class YtDlpAuthenticatedCaptionsProvider:
    """yt-dlp with the configured cookie file/browser session.

    Runs only after the anonymous yt-dlp stage was blocked, rate limited,
    circuit-open, or reported that a signed-in/member session is required.
    Cookies are therefore never sent for requests that did not need them.
    """

    provider = "yt-dlp"
    method = "captions_authenticated"
    retryable = True

    def is_eligible(self, attempts: Sequence[AcquisitionAttempt]) -> bool:
        anonymous = [
            a for a in attempts if a.provider == "yt-dlp" and a.method == "captions"
        ]
        return bool(anonymous) and anonymous[-1].detail_code in AUTH_RECOVERABLE_DETAILS

    def is_configured(self) -> bool:
        return ytdlp_transcript.cookies_configured()

    def acquire(self, video_id: str, lang: str) -> ProviderOutput:
        return ProviderOutput(
            fetch=ytdlp_transcript.fetch_transcript(video_id, lang=lang, cookies=True)
        )

    def classify(self, exc: Exception) -> tuple[str, str]:
        return _fallback_failure_detail(exc)


class LocalSttProvider:
    """Reserved final stage: explicitly configured local whisper.cpp STT."""

    provider = "whisper.cpp"
    method = "local_stt"
    retryable = False  # expensive; failures are configuration/local problems

    def __init__(self) -> None:
        self._settings: whisper_cpp.WhisperCppSettings | None = None

    def is_eligible(self, attempts: Sequence[AcquisitionAttempt]) -> bool:
        # Never download member-only media for local transcription unless an
        # authenticated caption stage has since succeeded (which ends the loop).
        return not _has_detail(attempts, "membership_required")

    def is_configured(self) -> bool:
        # The model path is the explicit opt-in. If no model is configured, do
        # not report a phantom STT attempt.
        self._settings = whisper_cpp.settings_from_env()
        return self._settings is not None

    def acquire(self, video_id: str, lang: str) -> ProviderOutput:
        local = whisper_cpp.transcribe_video(video_id, settings=self._settings)
        return ProviderOutput(fetch=local.fetch, details=local.provenance)

    def classify(self, exc: Exception) -> tuple[str, str]:
        return _local_stt_failure_detail(exc)


def default_providers() -> list[TranscriptProvider]:
    """Fresh provider instances in waterfall order."""
    return [
        NativeCaptionsProvider(),
        YtDlpCaptionsProvider(),
        YtDlpAuthenticatedCaptionsProvider(),
        LocalSttProvider(),
    ]


# ---------------------------------------------------------------------------
# Policy: retries + circuit breaker
# ---------------------------------------------------------------------------


def _env_number(name: str, default: float, *, lo: float, hi: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("ignoring non-numeric %s; using default", name)
        return default
    return min(hi, max(lo, value))


@dataclass(frozen=True)
class AcquisitionPolicy:
    max_retries: int = 1
    backoff_s: float = 0.5
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 300.0

    @classmethod
    def from_env(cls) -> "AcquisitionPolicy":
        return cls(
            max_retries=int(
                _env_number("YOUTUBE_MCP_ACQUISITION_RETRIES", 1, lo=0, hi=3)
            ),
            backoff_s=_env_number("YOUTUBE_MCP_ACQUISITION_BACKOFF_S", 0.5, lo=0, hi=10),
            breaker_threshold=int(
                _env_number("YOUTUBE_MCP_BREAKER_THRESHOLD", 3, lo=1, hi=100)
            ),
            breaker_cooldown_s=_env_number(
                "YOUTUBE_MCP_BREAKER_COOLDOWN_S", 300, lo=0, hi=86400
            ),
        )


@dataclass
class _BreakerState:
    consecutive_blocks: int = 0
    opened_at: float | None = None
    half_open: bool = False


@dataclass
class CircuitBreaker:
    """Per-stage breaker for repeated block/rate-limit responses.

    closed → (threshold consecutive blocked outcomes) → open
    open   → (cooldown elapsed) → half-open: one trial request is allowed
    half-open success/non-block → closed; half-open block → open again
    """

    clock: Callable[[], float] = monotonic
    _states: dict[str, _BreakerState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str, policy: AcquisitionPolicy) -> bool:
        with self._lock:
            state = self._states.get(key)
            if state is None or state.opened_at is None:
                return True
            if self.clock() - state.opened_at >= policy.breaker_cooldown_s:
                state.half_open = True
                state.opened_at = None
                return True
            return False

    def record(self, key: str, outcome: str, policy: AcquisitionPolicy) -> None:
        with self._lock:
            state = self._states.setdefault(key, _BreakerState())
            if outcome in _BREAKER_OUTCOMES:
                state.consecutive_blocks += 1
                if state.half_open or state.consecutive_blocks >= policy.breaker_threshold:
                    state.opened_at = self.clock()
                    state.half_open = False
                return
            if outcome == "timeout":
                # Timeouts are retried rather than counted as refusals; they do
                # not prove the upstream is reachable either, so leave state.
                return
            self._states.pop(key, None)

    def is_open(self, key: str) -> bool:
        with self._lock:
            state = self._states.get(key)
            return state is not None and state.opened_at is not None

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


_BREAKER = CircuitBreaker()


def reset_circuit_breakers() -> None:
    """Close every breaker (tests, or an operator after fixing routing)."""
    _BREAKER.reset()


def _elapsed_ms(started: float) -> int:
    return max(0, int((monotonic() - started) * 1000))


def _log_attempt(video_id: str, attempt: AcquisitionAttempt) -> None:
    # Only fixed machine-safe fields; exception text is never logged here.
    log.info(
        "transcript acquisition video=%s provider=%s method=%s outcome=%s "
        "detail=%s duration_ms=%d retry=%d",
        video_id,
        attempt.provider,
        attempt.method,
        attempt.outcome,
        attempt.detail_code,
        attempt.duration_ms,
        attempt.retry,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def acquire_transcript(
    video_id: str,
    lang: str = "en",
    *,
    providers: Sequence[TranscriptProvider] | None = None,
    policy: AcquisitionPolicy | None = None,
    breaker: CircuitBreaker | None = None,
) -> AcquisitionResult:
    """Acquire a transcript through independent providers with attempt history.

    Callers never choose providers; `providers`/`policy`/`breaker` exist for
    tests and future configuration. Local STT never downloads a model
    implicitly.
    """
    stages = list(providers) if providers is not None else default_providers()
    policy = policy or AcquisitionPolicy.from_env()
    breaker = breaker or _BREAKER

    attempts: list[AcquisitionAttempt] = []
    primary_error: Exception | None = None
    fallback_error: Exception | None = None
    local_stt_error: Exception | None = None

    def remember(stage: TranscriptProvider, exc: Exception) -> None:
        nonlocal primary_error, fallback_error, local_stt_error
        if stage.provider == "youtube-transcript-api":
            primary_error = exc
        elif stage.provider == "yt-dlp":
            fallback_error = exc
        elif stage.provider == "whisper.cpp":
            local_stt_error = exc

    def record(attempt: AcquisitionAttempt) -> None:
        attempts.append(attempt)
        _log_attempt(video_id, attempt)

    for stage in stages:
        if not stage.is_eligible(attempts):
            continue

        try:
            configured = stage.is_configured()
        except Exception as exc:
            remember(stage, exc)
            outcome, detail = stage.classify(exc)
            record(
                AcquisitionAttempt(
                    provider=stage.provider,
                    method=stage.method,
                    duration_ms=0,
                    outcome=outcome,
                    detail_code=detail,
                )
            )
            continue
        if not configured:
            continue

        key = f"{stage.provider}:{stage.method}"
        if not breaker.allow(key, policy):
            record(
                AcquisitionAttempt(
                    provider=stage.provider,
                    method=stage.method,
                    duration_ms=0,
                    outcome="skipped",
                    detail_code="circuit_open",
                )
            )
            continue

        max_retries = policy.max_retries if stage.retryable else 0
        for retry in range(max_retries + 1):
            started = monotonic()
            try:
                output = stage.acquire(video_id, lang)
            except Exception as exc:
                remember(stage, exc)
                outcome, detail = stage.classify(exc)
                record(
                    AcquisitionAttempt(
                        provider=stage.provider,
                        method=stage.method,
                        duration_ms=_elapsed_ms(started),
                        outcome=outcome,
                        detail_code=detail,
                        retry=retry,
                    )
                )
                breaker.record(key, outcome, policy)
                if detail in RETRYABLE_DETAILS and retry < max_retries:
                    _sleep(policy.backoff_s * (2**retry))
                    continue
                break
            else:
                record(
                    AcquisitionAttempt(
                        provider=stage.provider,
                        method=stage.method,
                        duration_ms=_elapsed_ms(started),
                        outcome="success",
                        retry=retry,
                    )
                )
                breaker.record(key, "success", policy)
                return AcquisitionResult(
                    fetch=output.fetch,
                    provider=stage.provider,
                    method=stage.method,
                    attempts=attempts,
                    details=output.details,
                )

    if _has_detail(attempts, "membership_required"):
        message = "yt-dlp reported that channel membership is required"
    else:
        message = "all transcript acquisition providers failed"
    raise TranscriptAcquisitionFailed(
        message,
        attempts=attempts,
        primary_error=primary_error,
        fallback_error=fallback_error,
        local_stt_error=local_stt_error,
    )
