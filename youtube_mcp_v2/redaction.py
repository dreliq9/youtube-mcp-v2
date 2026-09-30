"""Secret redaction shared by diagnostics, tool responses, and HTTP errors.

The rule everywhere is *presence, never value*: diagnostics may say that a key,
proxy, cookie source, or HTTP token is configured, but no configured secret
value (or credential-bearing URL userinfo) may leave the process in output.
"""

from __future__ import annotations

import os
import re
from typing import Any

# Environment variables whose values are credentials or credential-bearing.
# Cookie *file paths* are included: a path can reveal account/user names and the
# file is itself a credential.
SECRET_ENV_VARS: tuple[str, ...] = (
    "YOUTUBE_API_KEY",
    "YOUTUBE_TRANSCRIPT_PROXY",
    "YOUTUBE_TRANSCRIPT_HTTP_PROXY",
    "YOUTUBE_TRANSCRIPT_HTTPS_PROXY",
    "YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME",
    "YOUTUBE_TRANSCRIPT_WEBSHARE_PASSWORD",
    "YOUTUBE_YTDLP_PROXY",
    "YOUTUBE_MCP_PROXY",
    "YOUTUBE_COOKIES_FILE",
    "YOUTUBE_MCP_HTTP_TOKEN",
)

REDACTED = "<redacted>"
_MIN_SECRET_LEN = 4  # never "redact" trivially short values like "1"
_URL_USERINFO_RE = re.compile(r"(?i)\b((?:https?|socks[45]h?)://)[^\s/@:]+:[^\s/@]+@")


def configured_secret_values() -> list[str]:
    """Current secret values from the environment, longest first."""
    values: set[str] = set()
    for name in SECRET_ENV_VARS:
        raw = os.environ.get(name)
        if not raw:
            continue
        stripped = raw.strip()
        for candidate in {raw, stripped, *(part.strip() for part in stripped.split(","))}:
            if len(candidate) >= _MIN_SECRET_LEN:
                values.add(candidate)
    return sorted(values, key=len, reverse=True)


def redact_text(text: str, secrets: list[str] | None = None) -> str:
    for value in configured_secret_values() if secrets is None else secrets:
        if value in text:
            text = text.replace(value, REDACTED)
    return _URL_USERINFO_RE.sub(rf"\1{REDACTED}@", text)


def redact(value: Any, secrets: list[str] | None = None) -> Any:
    """Return a copy of a JSON-like object with configured secrets removed."""
    if secrets is None:
        secrets = configured_secret_values()
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {
            (redact_text(k, secrets) if isinstance(k, str) else k): redact(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v, secrets) for v in value)
    return value
