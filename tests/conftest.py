"""Shared deterministic isolation for acquisition tests.

The acquisition layer reads routing/auth/policy settings from the process
environment and keeps an in-process circuit breaker. Tests must not inherit a
developer's real proxy/cookie configuration, sleep during retry backoff, or leak
breaker state between cases.
"""

from __future__ import annotations

import pytest

from youtube_mcp_v2 import transcript_acquisition

ACQUISITION_ENV = (
    "YOUTUBE_MCP_PROXY",
    "YOUTUBE_YTDLP_PROXY",
    "YOUTUBE_COOKIES_FILE",
    "YOUTUBE_COOKIES_FROM_BROWSER",
    "YOUTUBE_TRANSCRIPT_TIMEOUT_S",
    "YOUTUBE_MCP_ACQUISITION_RETRIES",
    "YOUTUBE_MCP_ACQUISITION_BACKOFF_S",
    "YOUTUBE_MCP_BREAKER_THRESHOLD",
    "YOUTUBE_MCP_BREAKER_COOLDOWN_S",
    "YOUTUBE_MCP_WHISPER_CPP_MODEL",
)


@pytest.fixture(autouse=True)
def _isolate_acquisition_state(monkeypatch):
    for name in ACQUISITION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(transcript_acquisition, "_sleep", lambda _s: None)
    transcript_acquisition.reset_circuit_breakers()
    yield
    transcript_acquisition.reset_circuit_breakers()
