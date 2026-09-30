"""Deterministic coverage for the v0.3 acquisition waterfall (issue #2).

No test here touches the network: every provider, subprocess and HTTP session is
replaced with an in-process fake.
"""

from __future__ import annotations

import inspect
import json
import logging
import subprocess

import pytest
import requests

from youtube_mcp_v2 import cache, server, transcript_acquisition
from youtube_mcp_v2.adapters import transcript_api, video_clip, whisper_cpp, ytdlp_transcript
from youtube_mcp_v2.adapters.transcript_api import Segment, TranscriptFetch
from youtube_mcp_v2.tools import transcript as transcript_tool
from youtube_mcp_v2.tools.transcript import transcript_get
from youtube_mcp_v2.transcript_acquisition import (
    AcquisitionPolicy,
    CircuitBreaker,
    acquire_transcript,
)


VIDEO_ID = "jNQXAC9IVRw"

SECRET_PROXY = "socks5://proxy-user:proxy-hunter2@proxy.example:1080"
SECRET_YTDLP_PROXY = "http://yt-user:yt-pass-s3cret@ytproxy.example:8080"
SECRET_COOKIE_FILE = "/home/alice/very-private-cookies.txt"
SECRET_API_KEY = "AIzaSy-SECRET-API-KEY-000"
SECRET_AUTH_HEADER = "Bearer secret-auth-token-xyz"
SECRETS = (
    SECRET_PROXY,
    "proxy-hunter2",
    SECRET_YTDLP_PROXY,
    "yt-pass-s3cret",
    SECRET_COOKIE_FILE,
    "very-private-cookies",
    SECRET_API_KEY,
    SECRET_AUTH_HEADER,
    "secret-auth-token",
)


def _fetch(text: str = "hello world", lang: str = "en", generated: bool = False):
    return TranscriptFetch(
        segments=[Segment(start_s=0.0, duration_s=2.0, text=text)],
        lang=lang,
        is_generated=generated,
    )


def _raise(exc: Exception):
    def fn(*_a, **_k):
        raise exc

    return fn


class _Calls:
    """Records provider invocations (args + kwargs) and returns scripted results."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class _HttpError429(Exception):
    """Shape of requests.HTTPError: carries a response with status_code."""

    def __init__(self) -> None:
        super().__init__("HTTP error from upstream")

        class _Resp:
            status_code = 429

        self.response = _Resp()


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "youtube-mcp"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_PATH", cache_dir / "v2.sqlite")
    # Avoid the live inspect.video duration lookup.
    monkeypatch.setattr(transcript_tool, "_resolve_duration", lambda _vid: None)
    return cache_dir


@pytest.fixture()
def no_local_stt(monkeypatch):
    monkeypatch.setattr(whisper_cpp, "settings_from_env", lambda: None)


def _attempt_tuples(attempts):
    return [
        (a["provider"], a["method"], a["outcome"], a["detail_code"], a["retry"])
        for a in attempts
    ]


# ---------------------------------------------------------------------------
# AC1: primary failure + secondary success → data + provenance for both
# ---------------------------------------------------------------------------


def test_primary_failure_secondary_success_via_public_tool(
    monkeypatch, isolated_cache, no_local_stt
) -> None:
    monkeypatch.setattr(
        transcript_api, "fetch_transcript", _raise(transcript_api.RequestBlocked(VIDEO_ID))
    )
    ytdlp = _Calls(_fetch("fallback text", generated=True))
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", ytdlp)

    env = transcript_get(VIDEO_ID)

    assert env["error"] is None
    assert env["data"]["text"] == "fallback text"
    acquisition = env["provenance"]["acquisition"]
    assert acquisition["provider"] == "yt-dlp"
    assert acquisition["method"] == "captions"
    assert acquisition["provider_class"] == "scrape"
    assert _attempt_tuples(acquisition["attempts"]) == [
        ("youtube-transcript-api", "captions", "blocked", "request_blocked", 0),
        ("yt-dlp", "captions", "success", None, 0),
    ]
    assert all(isinstance(a["duration_ms"], int) for a in acquisition["attempts"])
    assert "transcript fallback used: yt-dlp" in env["warnings"]
    # The anonymous yt-dlp stage never sends cookies.
    assert ytdlp.calls[0]["kwargs"]["cookies"] is False


# ---------------------------------------------------------------------------
# AC2: 429 / timeout / malformed / disabled / total failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [_HttpError429(), RuntimeError("HTTP Error 429: Too Many Requests")],
    ids=["status-429", "message-429"],
)
def test_429_is_rate_limited_and_falls_back(monkeypatch, no_local_stt, exc) -> None:
    monkeypatch.setattr(transcript_api, "fetch_transcript", _raise(exc))
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", _Calls(_fetch()))

    result = acquire_transcript(VIDEO_ID)
    assert result.provider == "yt-dlp"
    first = result.attempts[0]
    assert (first.outcome, first.detail_code, first.retry) == ("blocked", "rate_limited", 0)
    # Rate limits are not retried on the same stage.
    assert len(result.attempts) == 2


def test_ytdlp_429_is_rate_limited_and_structured_failure(
    monkeypatch, isolated_cache, no_local_stt
) -> None:
    monkeypatch.setattr(
        transcript_api, "fetch_transcript", _raise(RuntimeError("429 Too Many Requests"))
    )
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        _raise(ytdlp_transcript.YtDlpTranscriptError("ERROR: HTTP Error 429: Too Many Requests")),
    )

    env = transcript_get(VIDEO_ID)
    assert env["data"] is None
    assert env["error"]["code"] == "transcript_fetch_failed"
    assert env["error"]["recoverable"] is True
    assert _attempt_tuples(env["provenance"]["acquisition"]["attempts"]) == [
        ("youtube-transcript-api", "captions", "blocked", "rate_limited", 0),
        ("yt-dlp", "captions", "blocked", "rate_limited", 0),
    ]


def test_timeout_is_retried_with_bounded_backoff_then_falls_back(
    monkeypatch, no_local_stt
) -> None:
    primary = _Calls(requests.exceptions.ReadTimeout("read timed out"))
    monkeypatch.setattr(transcript_api, "fetch_transcript", primary)
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", _Calls(_fetch()))
    sleeps: list[float] = []
    monkeypatch.setattr(transcript_acquisition, "_sleep", sleeps.append)

    policy = AcquisitionPolicy(max_retries=2, backoff_s=0.5)
    result = acquire_transcript(VIDEO_ID, policy=policy, breaker=CircuitBreaker())

    assert len(primary.calls) == 3  # first try + 2 bounded retries
    assert sleeps == [0.5, 1.0]
    assert [(a.provider, a.outcome, a.detail_code, a.retry) for a in result.attempts] == [
        ("youtube-transcript-api", "timeout", "timeout", 0),
        ("youtube-transcript-api", "timeout", "timeout", 1),
        ("youtube-transcript-api", "timeout", "timeout", 2),
        ("yt-dlp", "success", None, 0),
    ]


def test_timeout_retry_can_recover_on_same_provider(monkeypatch, no_local_stt) -> None:
    primary = _Calls(TimeoutError("slow"), _fetch("second try"))
    monkeypatch.setattr(transcript_api, "fetch_transcript", primary)
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        lambda *_a, **_k: pytest.fail("fallback used after successful retry"),
    )

    result = acquire_transcript(VIDEO_ID, policy=AcquisitionPolicy(max_retries=1))
    assert result.provider == "youtube-transcript-api"
    assert [(a.outcome, a.retry) for a in result.attempts] == [("timeout", 0), ("success", 1)]


def test_retry_policy_env_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_MCP_ACQUISITION_RETRIES", "99")
    monkeypatch.setenv("YOUTUBE_MCP_ACQUISITION_BACKOFF_S", "not-a-number")
    policy = AcquisitionPolicy.from_env()
    assert policy.max_retries == 3
    assert policy.backoff_s == 0.5


def test_ytdlp_timeout_is_structured(monkeypatch, isolated_cache, no_local_stt) -> None:
    monkeypatch.setattr(
        transcript_api, "fetch_transcript", _raise(transcript_api.NoTranscriptFound(VIDEO_ID, ["en"], None))
    )
    monkeypatch.setattr(
        ytdlp_transcript, "fetch_transcript", _raise(TimeoutError("yt-dlp exceeded 60s"))
    )
    env = transcript_get(VIDEO_ID)
    attempts = env["provenance"]["acquisition"]["attempts"]
    assert [(a["provider"], a["outcome"], a["retry"]) for a in attempts] == [
        ("youtube-transcript-api", "unavailable", 0),
        ("yt-dlp", "timeout", 0),
        ("yt-dlp", "timeout", 1),
    ]
    # Existing semantic mapping is preserved: the primary said "no transcript".
    assert env["error"]["code"] == "no_transcript"


def test_native_malformed_payload_is_classified(monkeypatch, no_local_stt) -> None:
    class BadSnippet:
        start = "not-a-number"
        duration = 1.0
        text = "x"

    class BadResult:
        snippets = [BadSnippet()]
        language_code = "en"

    class FakeApi:
        def __init__(self, **_kwargs):
            pass

        def fetch(self, *_a, **_k):
            return BadResult()

    monkeypatch.setattr(transcript_api, "YouTubeTranscriptApi", FakeApi)
    with pytest.raises(transcript_api.TranscriptPayloadError):
        transcript_api.fetch_transcript(VIDEO_ID)

    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", _Calls(_fetch()))
    result = acquire_transcript(VIDEO_ID)
    assert (result.attempts[0].outcome, result.attempts[0].detail_code) == (
        "error",
        "malformed_payload",
    )
    # Malformed payloads are deterministic; they are not retried.
    assert [a.provider for a in result.attempts] == ["youtube-transcript-api", "yt-dlp"]


def test_ytdlp_malformed_metadata_is_classified(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="{not json", stderr=""),
    )
    with pytest.raises(ytdlp_transcript.YtDlpMalformedPayload):
        ytdlp_transcript.fetch_transcript(VIDEO_ID, cookies=False)

    outcome = transcript_acquisition._fallback_failure_detail(
        ytdlp_transcript.YtDlpMalformedPayload("bad")
    )
    assert outcome == ("error", "malformed_payload")


def test_disabled_captions_are_stable_nonrecoverable(
    monkeypatch, isolated_cache, no_local_stt
) -> None:
    monkeypatch.setattr(
        transcript_api, "fetch_transcript", _raise(transcript_api.TranscriptsDisabled(VIDEO_ID))
    )
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        _raise(ytdlp_transcript.YtDlpNoTranscript("no tracks")),
    )

    env = transcript_get(VIDEO_ID)
    assert env["error"]["code"] == "transcripts_disabled"
    assert env["error"]["recoverable"] is False
    assert _attempt_tuples(env["provenance"]["acquisition"]["attempts"]) == [
        ("youtube-transcript-api", "captions", "unavailable", "transcripts_disabled", 0),
        ("yt-dlp", "captions", "unavailable", "no_transcript", 0),
    ]


def test_total_failure_is_structured_and_not_cached(
    monkeypatch, isolated_cache, no_local_stt
) -> None:
    monkeypatch.setattr(transcript_api, "fetch_transcript", _raise(RuntimeError("boom")))
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        _raise(ytdlp_transcript.YtDlpUnavailable("yt-dlp executable not found")),
    )

    env = transcript_get(VIDEO_ID)
    assert env["data"] is None
    assert env["error"] == {
        "code": "transcript_fetch_failed",
        "message": "all configured transcript acquisition providers failed; see provenance attempts",
        "recoverable": True,
    }
    acquisition = env["provenance"]["acquisition"]
    assert acquisition["provider"] is None
    assert acquisition["provider_class"] is None
    assert _attempt_tuples(acquisition["attempts"]) == [
        ("youtube-transcript-api", "captions", "error", "provider_error", 0),
        ("yt-dlp", "captions", "unavailable", "missing_dependency", 0),
    ]
    assert cache.get_transcript(VIDEO_ID, "en") is None


def test_config_errors_are_structured_without_values(monkeypatch, no_local_stt) -> None:
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_TIMEOUT_S", "abc-secret")
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", SECRET_COOKIE_FILE)
    monkeypatch.setenv("YOUTUBE_COOKIES_FROM_BROWSER", "firefox:secret-profile")
    monkeypatch.setattr(
        ytdlp_transcript,
        "_dump_info",
        _raise(ytdlp_transcript.YtDlpTranscriptError("ERROR: Sign in to confirm you're not a bot")),
    )

    with pytest.raises(transcript_acquisition.TranscriptAcquisitionFailed) as exc_info:
        acquire_transcript(VIDEO_ID)
    exc = exc_info.value
    assert [(a.provider, a.method, a.outcome, a.detail_code) for a in exc.attempts] == [
        ("youtube-transcript-api", "captions", "error", "config_error"),
        # Anonymous stage ignores the conflicting cookie config entirely.
        ("yt-dlp", "captions", "blocked", "request_blocked"),
        ("yt-dlp", "captions_authenticated", "error", "config_error"),
    ]
    assert "abc-secret" not in str(exc.primary_error)
    assert SECRET_COOKIE_FILE not in str(exc.fallback_error)
    assert "secret-profile" not in str(exc.fallback_error)


# ---------------------------------------------------------------------------
# Cookie-authenticated stage
# ---------------------------------------------------------------------------


def test_cookie_stage_runs_after_anonymous_block_when_configured(
    monkeypatch, isolated_cache, no_local_stt
) -> None:
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", SECRET_COOKIE_FILE)
    monkeypatch.setattr(
        transcript_api, "fetch_transcript", _raise(transcript_api.RequestBlocked(VIDEO_ID))
    )
    ytdlp = _Calls(
        ytdlp_transcript.YtDlpTranscriptError("ERROR: Sign in to confirm you're not a bot"),
        _fetch("authed text"),
    )
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", ytdlp)

    env = transcript_get(VIDEO_ID)
    assert env["data"]["text"] == "authed text"
    assert [c["kwargs"]["cookies"] for c in ytdlp.calls] == [False, True]
    acquisition = env["provenance"]["acquisition"]
    assert (acquisition["provider"], acquisition["method"]) == ("yt-dlp", "captions_authenticated")
    assert _attempt_tuples(acquisition["attempts"]) == [
        ("youtube-transcript-api", "captions", "blocked", "request_blocked", 0),
        ("yt-dlp", "captions", "blocked", "request_blocked", 0),
        ("yt-dlp", "captions_authenticated", "success", None, 0),
    ]
    assert "transcript fallback used: yt-dlp (cookie-authenticated)" in env["warnings"]
    assert SECRET_COOKIE_FILE not in json.dumps(env)


def test_cookie_stage_is_skipped_without_cookie_config(monkeypatch, no_local_stt) -> None:
    monkeypatch.setattr(
        transcript_api, "fetch_transcript", _raise(transcript_api.RequestBlocked(VIDEO_ID))
    )
    ytdlp = _Calls(ytdlp_transcript.YtDlpTranscriptError("HTTP Error 429"))
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", ytdlp)

    with pytest.raises(transcript_acquisition.TranscriptAcquisitionFailed) as exc_info:
        acquire_transcript(VIDEO_ID)
    assert len(ytdlp.calls) == 1
    assert [a.method for a in exc_info.value.attempts] == ["captions", "captions"]


def test_cookie_stage_is_skipped_when_captions_simply_absent(monkeypatch, no_local_stt) -> None:
    monkeypatch.setenv("YOUTUBE_COOKIES_FROM_BROWSER", "firefox")
    monkeypatch.setattr(
        transcript_api, "fetch_transcript", _raise(transcript_api.NoTranscriptFound(VIDEO_ID, ["en"], None))
    )
    ytdlp = _Calls(ytdlp_transcript.YtDlpNoTranscript("none"))
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", ytdlp)

    with pytest.raises(transcript_acquisition.TranscriptAcquisitionFailed):
        acquire_transcript(VIDEO_ID)
    assert len(ytdlp.calls) == 1  # cookies cannot create missing captions


def test_membership_with_cookies_tries_auth_but_never_local_stt(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", SECRET_COOKIE_FILE)
    monkeypatch.setattr(
        transcript_api, "fetch_transcript", _raise(transcript_api.NoTranscriptFound(VIDEO_ID, ["en"], None))
    )
    ytdlp = _Calls(ytdlp_transcript.YtDlpTranscriptError("Join this channel to get access to members-only content"))
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", ytdlp)
    monkeypatch.setattr(
        whisper_cpp, "settings_from_env", lambda: pytest.fail("local STT inspected for member-only video")
    )

    with pytest.raises(transcript_acquisition.TranscriptAcquisitionFailed) as exc_info:
        acquire_transcript(VIDEO_ID)
    assert [c["kwargs"]["cookies"] for c in ytdlp.calls] == [False, True]
    assert [(a.method, a.detail_code) for a in exc_info.value.attempts] == [
        ("captions", "no_transcript"),
        ("captions", "membership_required"),
        ("captions_authenticated", "membership_required"),
    ]
    assert "membership" in str(exc_info.value)


def test_anonymous_settings_never_read_cookies(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", SECRET_COOKIE_FILE)
    monkeypatch.setenv("YOUTUBE_COOKIES_FROM_BROWSER", "chrome")  # conflicting on purpose
    anonymous = ytdlp_transcript.settings_from_env(cookies=False)
    assert anonymous.cookies_file is None and anonymous.cookies_from_browser is None
    assert "--cookies" not in ytdlp_transcript._auth_network_args(anonymous)
    assert ytdlp_transcript.cookies_configured() is True


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_opens_after_repeated_blocks_and_half_opens(monkeypatch, no_local_stt) -> None:
    now = [1000.0]
    breaker = CircuitBreaker(clock=lambda: now[0])
    policy = AcquisitionPolicy(breaker_threshold=2, breaker_cooldown_s=60)
    primary = _Calls(transcript_api.RequestBlocked(VIDEO_ID))
    monkeypatch.setattr(transcript_api, "fetch_transcript", primary)
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", _Calls(_fetch()))

    for _ in range(2):
        acquire_transcript(VIDEO_ID, policy=policy, breaker=breaker)
    assert len(primary.calls) == 2
    assert breaker.is_open("youtube-transcript-api:captions")

    # Open: primary is not called and the skip is visible in provenance.
    result = acquire_transcript(VIDEO_ID, policy=policy, breaker=breaker)
    assert len(primary.calls) == 2
    assert [(a.provider, a.outcome, a.detail_code, a.duration_ms) for a in result.attempts] == [
        ("youtube-transcript-api", "skipped", "circuit_open", 0),
        ("yt-dlp", "success", None, result.attempts[1].duration_ms),
    ]
    # yt-dlp succeeded every time, so its breaker stays closed.
    assert not breaker.is_open("yt-dlp:captions")

    # Cooldown elapsed: one half-open trial; success closes the breaker.
    now[0] += 61
    primary.results = [_fetch("recovered")]
    result = acquire_transcript(VIDEO_ID, policy=policy, breaker=breaker)
    assert result.provider == "youtube-transcript-api"
    assert not breaker.is_open("youtube-transcript-api:captions")


def test_half_open_block_reopens_immediately() -> None:
    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0])
    policy = AcquisitionPolicy(breaker_threshold=3, breaker_cooldown_s=10)
    for _ in range(3):
        breaker.record("k", "blocked", policy)
    assert not breaker.allow("k", policy)
    now[0] = 10
    assert breaker.allow("k", policy)
    breaker.record("k", "blocked", policy)  # single failure in half-open
    assert not breaker.allow("k", policy)


def test_non_block_outcome_resets_consecutive_count() -> None:
    breaker = CircuitBreaker()
    policy = AcquisitionPolicy(breaker_threshold=2)
    breaker.record("k", "blocked", policy)
    breaker.record("k", "unavailable", policy)
    breaker.record("k", "blocked", policy)
    assert breaker.allow("k", policy)


def test_all_breakers_open_yields_recoverable_failure(monkeypatch, isolated_cache, no_local_stt) -> None:
    policy = AcquisitionPolicy(breaker_threshold=1, breaker_cooldown_s=3600)
    breaker = CircuitBreaker()
    breaker.record("youtube-transcript-api:captions", "blocked", policy)
    breaker.record("yt-dlp:captions", "blocked", policy)
    monkeypatch.setattr(transcript_acquisition, "_BREAKER", breaker)
    monkeypatch.setattr(transcript_acquisition.AcquisitionPolicy, "from_env", classmethod(lambda cls: policy))
    monkeypatch.setattr(transcript_api, "fetch_transcript", lambda *_a, **_k: pytest.fail("called while open"))
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", lambda *_a, **_k: pytest.fail("called while open"))

    env = transcript_get(VIDEO_ID)
    assert env["error"]["code"] == "transcript_fetch_failed"
    assert env["error"]["recoverable"] is True
    assert [a["detail_code"] for a in env["provenance"]["acquisition"]["attempts"]] == [
        "circuit_open",
        "circuit_open",
    ]


# ---------------------------------------------------------------------------
# Proxy propagation + bounded HTTP timeout
# ---------------------------------------------------------------------------


def test_shared_proxy_propagates_to_native_http_and_ytdlp(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_MCP_PROXY", SECRET_PROXY)
    for name in ("YOUTUBE_TRANSCRIPT_PROXY", "YOUTUBE_TRANSCRIPT_HTTP_PROXY", "YOUTUBE_TRANSCRIPT_HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)

    native = transcript_api.proxy_settings_from_env()
    assert native.mode == "generic"
    assert native.config.to_requests_dict() == {"http": SECRET_PROXY, "https": SECRET_PROXY}
    assert native.summary == {
        "mode": "generic",
        "http_configured": True,
        "https_configured": True,
        "shared": True,
    }

    ytdlp = ytdlp_transcript.settings_from_env(cookies=False)
    args = ytdlp_transcript._auth_network_args(ytdlp)
    assert args == ["--proxy", SECRET_PROXY]
    assert video_clip._yt_dlp_auth_args() == ["--proxy", SECRET_PROXY]

    for summary in (native.summary, ytdlp.summary):
        assert "proxy-hunter2" not in json.dumps(summary)


def test_provider_specific_proxy_wins_over_shared(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_MCP_PROXY", SECRET_PROXY)
    monkeypatch.setenv("YOUTUBE_YTDLP_PROXY", SECRET_YTDLP_PROXY)
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_PROXY", "http://native.example:3128")
    assert ytdlp_transcript.settings_from_env().proxy == SECRET_YTDLP_PROXY
    native = transcript_api.proxy_settings_from_env()
    assert native.config.to_requests_dict()["https"] == "http://native.example:3128"
    assert "shared" not in native.summary


def test_native_http_session_applies_default_timeout(monkeypatch) -> None:
    seen: dict = {}

    def fake_request(self, method, url, *args, **kwargs):
        seen.update(kwargs)
        return "response"

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_TIMEOUT_S", "7.5")
    session = transcript_api._TimeoutSession(transcript_api.timeout_from_env())
    assert session.get("https://www.youtube.com/watch?v=x") == "response"
    assert seen["timeout"] == 7.5
    session.request("GET", "https://example.invalid", timeout=3)
    assert seen["timeout"] == 3


@pytest.mark.parametrize("raw", ["0", "-1", "999", "nan", "secret-ish"])
def test_native_timeout_env_is_bounded(monkeypatch, raw) -> None:
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_TIMEOUT_S", raw)
    with pytest.raises(transcript_api.TranscriptConfigError) as exc_info:
        transcript_api.timeout_from_env()
    # The message names the variable and bounds, never the configured value.
    assert str(exc_info.value).startswith("YOUTUBE_TRANSCRIPT_TIMEOUT_S must be")
    assert "secret-ish" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# AC3: no secrets in tool results or logs
# ---------------------------------------------------------------------------


def test_no_secrets_in_results_or_logs(monkeypatch, isolated_cache, caplog) -> None:
    monkeypatch.setenv("YOUTUBE_MCP_PROXY", SECRET_PROXY)
    monkeypatch.setenv("YOUTUBE_YTDLP_PROXY", SECRET_YTDLP_PROXY)
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", SECRET_COOKIE_FILE)
    monkeypatch.setenv("YOUTUBE_API_KEY", SECRET_API_KEY)
    monkeypatch.setenv("YOUTUBE_MCP_WHISPER_CPP_MODEL", "/nonexistent/secret-model-dir/model.bin")

    # Upstream diagnostics that echo every secret we know about.
    leaky = (
        f"proxy={SECRET_PROXY} ytproxy={SECRET_YTDLP_PROXY} cookies={SECRET_COOKIE_FILE} "
        f"key={SECRET_API_KEY} Authorization: {SECRET_AUTH_HEADER}"
    )
    monkeypatch.setattr(transcript_api, "fetch_transcript", _raise(RuntimeError(leaky)))
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        _raise(ytdlp_transcript.YtDlpTranscriptError("Sign in to confirm you're not a bot " + leaky)),
    )

    caplog.set_level(logging.DEBUG)
    failed = transcript_get(VIDEO_ID)
    assert failed["error"]["code"] == "transcript_fetch_failed"
    assert len(failed["provenance"]["acquisition"]["attempts"]) == 4  # incl. auth + STT config

    # Now let the cookie-authenticated stage succeed.
    ytdlp = _Calls(
        ytdlp_transcript.YtDlpTranscriptError("Sign in to confirm you're not a bot " + leaky),
        _fetch("ok"),
    )
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", ytdlp)
    succeeded = transcript_get(VIDEO_ID)
    assert succeeded["error"] is None
    cached = transcript_get(VIDEO_ID)
    assert cached["source"] == "cache"

    rendered = json.dumps([failed, succeeded, cached]) + caplog.text
    assert "transcript acquisition" in caplog.text  # attempts are logged…
    for secret in SECRETS:
        assert secret not in rendered  # …but never secrets
    assert "secret-model-dir" not in rendered


def test_ytdlp_subprocess_diagnostics_are_redacted(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_YTDLP_PROXY", SECRET_YTDLP_PROXY)
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", SECRET_COOKIE_FILE)
    seen_cmd: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        seen_cmd.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr=f"ERROR: bad proxy {SECRET_YTDLP_PROXY} cookies {SECRET_COOKIE_FILE}"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ytdlp_transcript.YtDlpTranscriptError) as exc_info:
        ytdlp_transcript.fetch_transcript(VIDEO_ID, cookies=True)
    assert "--cookies" in seen_cmd[0]  # the value is passed to yt-dlp…
    message = str(exc_info.value)
    assert "yt-pass-s3cret" not in message  # …but never surfaces in errors
    assert SECRET_COOKIE_FILE not in message


# ---------------------------------------------------------------------------
# AC4: cache hits resolve original acquisition provenance
# ---------------------------------------------------------------------------


def test_cache_hit_resolves_original_fallback_provenance(
    monkeypatch, isolated_cache, no_local_stt
) -> None:
    monkeypatch.setattr(transcript_api, "fetch_transcript", _raise(_HttpError429()))
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", _Calls(_fetch("from yt-dlp")))
    live = transcript_get(VIDEO_ID, mode="timed")
    assert live["source"] == "scrape"

    monkeypatch.setattr(transcript_api, "fetch_transcript", lambda *_a, **_k: pytest.fail("upstream hit on cache"))
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", lambda *_a, **_k: pytest.fail("upstream hit on cache"))
    hit = transcript_get(VIDEO_ID, mode="timed")

    assert hit["source"] == "cache"
    assert hit["data"]["segments"] == live["data"]["segments"]
    live_acq = live["provenance"]["acquisition"]
    hit_acq = hit["provenance"]["acquisition"]
    assert hit_acq["provider"] == live_acq["provider"] == "yt-dlp"
    assert hit_acq["method"] == live_acq["method"] == "captions"
    assert hit_acq["provider_class"] == "scrape"
    assert hit_acq["attempts"] == live_acq["attempts"]
    assert hit_acq["attempts"][0]["detail_code"] == "rate_limited"
    assert hit["provenance"]["cache"]["hit"] is True
    assert hit["provenance"]["cache"]["revision_fetched_at"]


def test_malformed_cache_row_falls_through_to_live_waterfall(
    monkeypatch, isolated_cache, no_local_stt
) -> None:
    cache.put_transcript(
        video_id=VIDEO_ID,
        lang="en",
        text="corrupt",
        segments=None,
        word_count=1,
        validated=True,
        warnings=[],
    )
    with cache.connect() as conn:
        conn.execute("UPDATE transcripts SET segments_json = '{broken'")

    monkeypatch.setattr(transcript_api, "fetch_transcript", _Calls(_fetch("fresh")))
    env = transcript_get(VIDEO_ID)
    assert env["source"] == "scrape"
    assert env["data"]["text"] == "fresh"
    assert _attempt_tuples(env["provenance"]["acquisition"]["attempts"]) == [
        ("cache", "transcript_revision", "error", "cache_malformed", 0),
        ("youtube-transcript-api", "captions", "success", None, 0),
    ]
    assert "discarded malformed cached transcript revision" in env["warnings"]
    # The new, valid revision now answers from cache.
    assert transcript_get(VIDEO_ID)["source"] == "cache"


# ---------------------------------------------------------------------------
# AC5: public workflow unchanged; callers never pick providers
# ---------------------------------------------------------------------------


def test_public_transcript_tool_signature_is_unchanged() -> None:
    params = list(inspect.signature(server.tool_transcript_get).parameters)
    assert params == ["url_or_id", "mode", "lang", "cursor", "chunk_tokens", "chunk_overlap"]
    tool_params = list(inspect.signature(transcript_get).parameters)
    assert tool_params == params


def test_default_waterfall_order_and_stage_identity() -> None:
    stages = transcript_acquisition.default_providers()
    assert [(s.provider, s.method) for s in stages] == [
        ("youtube-transcript-api", "captions"),
        ("yt-dlp", "captions"),
        ("yt-dlp", "captions_authenticated"),
        ("whisper.cpp", "local_stt"),
    ]
    assert [s.retryable for s in stages] == [True, True, True, False]


def test_custom_provider_satisfies_interface(monkeypatch) -> None:
    class StubProvider:
        provider = "stub"
        method = "fixture"
        retryable = False

        def is_eligible(self, attempts):
            return True

        def is_configured(self):
            return True

        def acquire(self, video_id, lang):
            return transcript_acquisition.ProviderOutput(fetch=_fetch(), details={"k": "v"})

        def classify(self, exc):
            return "error", "provider_error"

    result = acquire_transcript(VIDEO_ID, providers=[StubProvider()])
    assert result.provenance["acquisition"]["details"] == {"k": "v"}
    assert result.provenance["acquisition"]["provider_class"] is None


def test_local_stt_stub_reports_unconfigured_as_no_attempt(monkeypatch) -> None:
    # Reserved final stage: disabled by default and invisible until configured.
    monkeypatch.setattr(transcript_api, "fetch_transcript", _raise(RuntimeError("x")))
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", _raise(ytdlp_transcript.YtDlpNoTranscript("x")))
    with pytest.raises(transcript_acquisition.TranscriptAcquisitionFailed) as exc_info:
        acquire_transcript(VIDEO_ID)
    assert all(a.provider != "whisper.cpp" for a in exc_info.value.attempts)
