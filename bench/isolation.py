"""Per-case isolation for deterministic benchmark runs.

Every deterministic case runs:

* with outbound network blocked at the socket layer (attempts are recorded
  and fail the case, so "no live network in CI" is enforced, not assumed),
* with the process environment scrubbed of `YOUTUBE_*`/`XDG_CACHE_HOME` and
  replaced by synthetic **secret canaries** (proxy credentials, API key,
  cookie path) that must never appear in results or logs,
* with every log record captured so the runner can scan it for canaries.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# Synthetic, obviously-fake secrets. They are configured exactly like real
# credentials so a leak through any code path is detectable.
CANARY_ENV: dict[str, str] = {
    "YOUTUBE_API_KEY": "AIzaBenchCanaryApiKey-000-never-print",
    "YOUTUBE_TRANSCRIPT_PROXY": "http://bench-user:benchCanaryProxyPw1@proxy.bench.invalid:3128",
    "YOUTUBE_YTDLP_PROXY": "socks5://bench-user:benchCanaryYtdlpPw2@proxy.bench.invalid:1080",
    "YOUTUBE_COOKIES_FILE": "/nonexistent/benchCanaryCookieJar.txt",
}

# Distinctive substrings scanned for. Full values are included too.
CANARY_TOKENS: tuple[str, ...] = (
    "AIzaBenchCanaryApiKey",
    "benchCanaryProxyPw1",
    "benchCanaryYtdlpPw2",
    "benchCanaryCookieJar",
    "Bearer benchCanaryAuthHeader",
    *CANARY_ENV.values(),
)

# A synthetic auth header value fixtures may echo back in upstream errors.
CANARY_AUTH_HEADER = "Bearer benchCanaryAuthHeader"

_SCRUB_PREFIXES = ("YOUTUBE_",)
_SCRUB_EXACT = ("XDG_CACHE_HOME",)


class NetworkBlocked(ConnectionError):
    """Raised when a deterministic case tries to open a network connection."""


@dataclass
class CaseCapture:
    network_attempts: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)

    def log_text(self) -> str:
        return "\n".join(self.log_lines)


def find_leaks(*blobs: str) -> list[str]:
    """Return the canary tokens present in any blob (deduplicated, stable)."""
    found: list[str] = []
    for token in CANARY_TOKENS:
        if any(token in blob for blob in blobs if blob) and token not in found:
            found.append(token)
    return found


def _is_local(address: Any) -> bool:
    if isinstance(address, (str, bytes)):  # AF_UNIX path
        return True
    if isinstance(address, tuple) and address:
        host = address[0]
        return host in ("localhost", "127.0.0.1", "::1")
    return False


@contextmanager
def block_network(capture: CaseCapture) -> Iterator[None]:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo

    def _deny(target: Any) -> NetworkBlocked:
        capture.network_attempts.append(repr(target)[:120])
        return NetworkBlocked(f"benchmark blocked network access to {target!r}")

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        if self.family == getattr(socket, "AF_UNIX", object()) or _is_local(address):
            return original_connect(self, address)
        raise _deny(address)

    def guarded_connect_ex(self, address):  # type: ignore[no-untyped-def]
        if self.family == getattr(socket, "AF_UNIX", object()) or _is_local(address):
            return original_connect_ex(self, address)
        raise _deny(address)

    def guarded_create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        if _is_local(address):
            return original_create_connection(address, *args, **kwargs)
        raise _deny(address)

    def guarded_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host in (None, "localhost", "127.0.0.1", "::1"):
            return original_getaddrinfo(host, *args, **kwargs)
        raise _deny(host)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    socket.create_connection = guarded_create_connection  # type: ignore[assignment]
    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.create_connection = original_create_connection  # type: ignore[assignment]
        socket.getaddrinfo = original_getaddrinfo  # type: ignore[assignment]


@contextmanager
def scrubbed_env(extra: dict[str, str] | None = None) -> Iterator[None]:
    saved = dict(os.environ)
    try:
        for key in list(os.environ):
            if key.startswith(_SCRUB_PREFIXES) or key in _SCRUB_EXACT:
                del os.environ[key]
        if extra:
            os.environ.update(extra)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


class _ListHandler(logging.Handler):
    def __init__(self, sink: list[str]) -> None:
        super().__init__(level=logging.DEBUG)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = f"{record.name}:{record.levelname}:{record.getMessage()}"
            if record.exc_info:
                line += "\n" + logging.Formatter().formatException(record.exc_info)
        except Exception:  # noqa: BLE001 - a broken log call must not break a case
            line = f"{record.name}:{record.levelname}:<unformattable>"
        self._sink.append(line)


@contextmanager
def capture_logs(capture: CaseCapture) -> Iterator[None]:
    root = logging.getLogger()
    handler = _ListHandler(capture.log_lines)
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


@contextmanager
def deterministic_case(capture: CaseCapture) -> Iterator[None]:
    """Full deterministic isolation: scrubbed env + canaries, logs, no network."""
    with scrubbed_env(CANARY_ENV), capture_logs(capture), block_network(capture):
        yield
