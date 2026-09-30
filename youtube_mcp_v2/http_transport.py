"""Streamable HTTP deployment: auth + rate-limit boundary around the MCP SDK app.

Design:

* The MCP SDK v2 app serves both protocol eras on one endpoint: modern
  2026-07-28 single-exchange requests (no `initialize`, no session) and
  handshake-era clients (2025-03-26 … 2025-11-25) via `initialize`.
* Default mode is stateless with JSON responses, which suits horizontally
  scaled deployments. `--stateful` enables `Mcp-Session-Id` sessions and SSE
  streams for handshake-era clients that need server→client streaming.
* In front of it sits a small ASGI boundary: static bearer tokens (compared as
  SHA-256 digests in constant time), a per-principal token-bucket rate limit,
  and an unauthenticated `/healthz`. Request bodies are bounded by the SDK.
* Remote mode is always on for HTTP: tool results withhold server-local paths
  (see `remote.py`) and artifacts are delivered as bounded MCP resources.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import threading
import time
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import remote

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

ENV_TOKEN = "YOUTUBE_MCP_HTTP_TOKEN"
ENV_TOKEN_FILE = "YOUTUBE_MCP_HTTP_TOKEN_FILE"
ENV_RATE = "YOUTUBE_MCP_HTTP_RATE_LIMIT_PER_MIN"
ENV_BURST = "YOUTUBE_MCP_HTTP_RATE_BURST"
ENV_MAX_BODY = "YOUTUBE_MCP_HTTP_MAX_BODY_BYTES"
ENV_ALLOWED_HOSTS = "YOUTUBE_MCP_HTTP_ALLOWED_HOSTS"

DEFAULT_RATE_PER_MIN = 120
DEFAULT_BURST = 30
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
MIN_TOKEN_LENGTH = 16
HEALTH_PATH = "/healthz"


class HttpConfigError(ValueError):
    """Invalid HTTP deployment configuration (message never contains secrets)."""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise HttpConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise HttpConfigError(f"{name} must be >= {minimum}")
    return value


def load_token_digests(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """SHA-256 digests of accepted bearer tokens.

    YOUTUBE_MCP_HTTP_TOKEN: one or more comma-separated tokens.
    YOUTUBE_MCP_HTTP_TOKEN_FILE: one token per line, or `sha256:<hex>` so the
    plaintext never has to exist on the server. `#` starts a comment line.
    """
    env = os.environ if environ is None else environ
    digests: list[str] = []
    raw = (env.get(ENV_TOKEN) or "").strip()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if len(token) < MIN_TOKEN_LENGTH:
            raise HttpConfigError(f"{ENV_TOKEN} entries must be at least {MIN_TOKEN_LENGTH} characters")
        digests.append(_digest(token))
    file_raw = (env.get(ENV_TOKEN_FILE) or "").strip()
    if file_raw:
        path = Path(file_raw).expanduser()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise HttpConfigError(f"{ENV_TOKEN_FILE} is not readable") from exc
        for number, line in enumerate(lines, start=1):
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            if entry.lower().startswith("sha256:"):
                hexdigest = entry.split(":", 1)[1].strip().lower()
                if len(hexdigest) != 64 or any(c not in "0123456789abcdef" for c in hexdigest):
                    raise HttpConfigError(f"{ENV_TOKEN_FILE} line {number}: malformed sha256 digest")
                digests.append(hexdigest)
            elif len(entry) < MIN_TOKEN_LENGTH:
                raise HttpConfigError(
                    f"{ENV_TOKEN_FILE} line {number}: tokens must be at least {MIN_TOKEN_LENGTH} characters"
                )
            else:
                digests.append(_digest(entry))
    return list(dict.fromkeys(digests))


def is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass
class HttpSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    stateless: bool = True
    json_response: bool | None = None  # default: JSON when stateless, SSE when stateful
    token_digests: list[str] = field(default_factory=list)
    allow_unauthenticated: bool = False
    rate_per_min: int = DEFAULT_RATE_PER_MIN
    burst: int = DEFAULT_BURST
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    allowed_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, **overrides: Any) -> HttpSettings:
        hosts_raw = os.environ.get(ENV_ALLOWED_HOSTS, "")
        settings = cls(
            token_digests=load_token_digests(),
            rate_per_min=_int_env(ENV_RATE, DEFAULT_RATE_PER_MIN),
            burst=_int_env(ENV_BURST, DEFAULT_BURST),
            max_body_bytes=_int_env(ENV_MAX_BODY, DEFAULT_MAX_BODY_BYTES, minimum=1024),
            allowed_hosts=[h.strip() for h in hosts_raw.split(",") if h.strip()],
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(settings, key, value)
        return settings

    def validate(self) -> list[str]:
        """Raise on unsafe configuration; return non-fatal warnings."""
        warnings: list[str] = []
        if not self.path.startswith("/") or self.path == HEALTH_PATH:
            raise HttpConfigError("MCP path must start with '/' and differ from /healthz")
        if not 0 < self.port < 65536:
            raise HttpConfigError("port must be between 1 and 65535")
        if not self.token_digests:
            if not is_loopback(self.host) and not self.allow_unauthenticated:
                raise HttpConfigError(
                    "refusing to serve on a non-loopback address without authentication; "
                    f"set {ENV_TOKEN} or {ENV_TOKEN_FILE}, or pass --allow-unauthenticated "
                    "if an upstream proxy enforces auth"
                )
            warnings.append("HTTP authentication is disabled (no bearer tokens configured)")
        if not is_loopback(self.host) and not self.allowed_hosts:
            warnings.append(
                "Host-header (DNS-rebinding) validation is off for non-loopback binds; "
                f"set {ENV_ALLOWED_HOSTS} or --allowed-host to the public host name(s)"
            )
        return warnings

    def summary(self) -> dict[str, Any]:
        """Configuration summary safe to print (no token material)."""
        return {
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "stateless": self.stateless,
            "json_response": self.resolved_json_response,
            "auth": "bearer" if self.token_digests else "none",
            "token_count": len(self.token_digests),
            "rate_limit_per_min": self.rate_per_min,
            "rate_burst": self.burst,
            "max_body_bytes": self.max_body_bytes,
            "allowed_hosts": list(self.allowed_hosts),
        }

    @property
    def resolved_json_response(self) -> bool:
        return self.stateless if self.json_response is None else self.json_response


class TokenBucket:
    """Per-principal token bucket. Thread-safe; clock injectable for tests."""

    def __init__(self, rate_per_min: int, burst: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = rate_per_min / 60.0
        self.capacity = float(burst)
        self.clock = clock
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    MAX_PRINCIPALS = 10_000

    def _prune(self, now: float) -> None:
        # Buckets that have fully refilled carry no state worth keeping.
        full_after = self.capacity / self.rate if self.rate > 0 else math.inf
        for key in [k for k, (_t, last) in self._state.items() if now - last >= full_after]:
            del self._state[key]

    def take(self, principal: str) -> float:
        """Consume one token. Returns 0 when allowed, else seconds to wait."""
        now = self.clock()
        with self._lock:
            if len(self._state) >= self.MAX_PRINCIPALS:
                self._prune(now)
            tokens, last = self._state.get(principal, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._state[principal] = (tokens - 1.0, now)
                return 0.0
            self._state[principal] = (tokens, now)
            return (1.0 - tokens) / self.rate if self.rate > 0 else math.inf


async def _json(send: Send, status: int, body: dict[str, Any], headers: list[tuple[bytes, bytes]] | None = None) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class BoundaryMiddleware:
    """Auth + rate limit in front of the MCP app. Health check stays open."""

    def __init__(self, app: ASGIApp, settings: HttpSettings, *, bucket: TokenBucket | None = None) -> None:
        self.app = app
        self.settings = settings
        self.digests = [d.lower() for d in settings.token_digests]
        self.bucket = bucket or TokenBucket(settings.rate_per_min, settings.burst)

    def _principal(self, scope: Scope) -> tuple[str | None, bool]:
        headers = {k.lower(): v for k, v in scope.get("headers") or []}
        client = scope.get("client") or ("unknown", 0)
        if not self.digests:
            return f"ip:{client[0]}", True
        raw = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = raw.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None, False
        presented = _digest(token.strip())
        matched = None
        for digest in self.digests:
            # Compare every digest (constant work regardless of which matches).
            if hmac.compare_digest(presented, digest):
                matched = digest
        if matched is None:
            return None, False
        return f"token:{matched[:12]}", True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == HEALTH_PATH:
            await _json(send, 200, {"status": "ok"})
            return
        principal, ok = self._principal(scope)
        if not ok:
            await _json(
                send,
                401,
                {"error": "unauthorized", "message": "missing or invalid bearer token"},
                [(b"www-authenticate", b'Bearer realm="youtube-mcp-v2"')],
            )
            return
        wait = self.bucket.take(principal or "anonymous")
        if wait > 0:
            await _json(
                send,
                429,
                {"error": "rate_limited", "retry_after_s": math.ceil(wait)},
                [(b"retry-after", str(math.ceil(wait)).encode("ascii"))],
            )
            return
        await self.app(scope, receive, send)


def build_app(settings: HttpSettings, *, server: Any = None) -> BoundaryMiddleware:
    """Starlette MCP app for `settings`, wrapped in the auth/rate boundary."""
    from mcp.server.transport_security import TransportSecuritySettings

    if server is None:
        from .server import mcp as server

    transport_security = None
    if settings.allowed_hosts:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=[f"https://{h}" for h in settings.allowed_hosts]
            + [f"http://{h}" for h in settings.allowed_hosts],
        )
    app = server.streamable_http_app(
        streamable_http_path=settings.path,
        stateless_http=settings.stateless,
        json_response=settings.resolved_json_response,
        max_request_body_size=settings.max_body_bytes,
        transport_security=transport_security,
        host=settings.host,
    )
    return BoundaryMiddleware(app, settings)


def serve(settings: HttpSettings, *, log: Callable[[str], None]) -> int:
    """Run the Streamable HTTP server (blocking)."""
    # Remote mode must be on before the server module registers its tools.
    os.environ[remote.REMOTE_ENV] = "1"
    warnings = settings.validate()
    import uvicorn

    from .server import mcp

    for name in remote.LOCAL_ONLY_TOOLS:
        try:
            mcp.remove_tool(name)
        except Exception:  # already absent (registered in remote mode)
            pass
    app = build_app(settings, server=mcp)
    for warning in warnings:
        log(f"warning: {warning}")
    log("serving Streamable HTTP: " + json.dumps(settings.summary(), sort_keys=True))
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", proxy_headers=False)
    return 0
