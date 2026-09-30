"""`youtube-mcp-v2 config check` — validate environment configuration offline.

Every check reports *presence and validity only*. Values of credentials,
proxy URLs, cookie sources, and HTTP tokens are never included in messages or
details; tests enforce this with canary values (tests/test_cli_diagnostics.py).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from .doctor import Check

# Every environment variable this package (or a pending v0.3 feature branch)
# reads. Anything else with a YOUTUBE_ prefix is reported as unrecognized so
# typos do not silently fall back to defaults.
KNOWN_ENV_VARS: frozenset[str] = frozenset(
    {
        "YOUTUBE_API_KEY",
        "YOUTUBE_TRANSCRIPT_PROXY",
        "YOUTUBE_TRANSCRIPT_HTTP_PROXY",
        "YOUTUBE_TRANSCRIPT_HTTPS_PROXY",
        "YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME",
        "YOUTUBE_TRANSCRIPT_WEBSHARE_PASSWORD",
        "YOUTUBE_TRANSCRIPT_WEBSHARE_LOCATIONS",
        "YOUTUBE_YTDLP_PROXY",
        "YOUTUBE_COOKIES_FILE",
        "YOUTUBE_COOKIES_FROM_BROWSER",
        "YOUTUBE_MCP_WHISPER_CPP_MODEL",
        "YOUTUBE_MCP_WHISPER_CPP_BIN",
        "YOUTUBE_MCP_WHISPER_CPP_TIMEOUT_S",
        "YOUTUBE_MCP_EMBED_MODEL",
        "YOUTUBE_MCP_EMBED_LOCAL_ONLY",
        "YOUTUBE_MCP_VISUAL_IMAGE_MODEL",
        "YOUTUBE_MCP_VISUAL_TEXT_MODEL",
        "YOUTUBE_MCP_MAX_RESOURCE_BYTES",
        "YOUTUBE_MCP_MAX_VISUAL_RESOURCE_BYTES",
        "YOUTUBE_MCP_CACHE_QUOTA_BYTES",
        "YOUTUBE_MCP_REMOTE",
        "YOUTUBE_MCP_HTTP_TOKEN",
        "YOUTUBE_MCP_HTTP_TOKEN_FILE",
        "YOUTUBE_MCP_HTTP_RATE_LIMIT_PER_MIN",
        "YOUTUBE_MCP_HTTP_RATE_BURST",
        "YOUTUBE_MCP_HTTP_MAX_BODY_BYTES",
        "YOUTUBE_MCP_HTTP_ALLOWED_HOSTS",
        # Pending feature branches (#30 provider waterfall, #32 corpus revisions).
        "YOUTUBE_MCP_PROXY",
        "YOUTUBE_MCP_ACQUISITION_RETRIES",
        "YOUTUBE_MCP_ACQUISITION_BACKOFF_S",
        "YOUTUBE_MCP_BREAKER_THRESHOLD",
        "YOUTUBE_MCP_BREAKER_COOLDOWN_S",
        "YOUTUBE_TRANSCRIPT_TIMEOUT_S",
        "YOUTUBE_MCP_LEGACY_SKELETON_TOOLS",
    }
)


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _positive_int_check(name: str, default_label: str) -> Check | None:
    raw = _env(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return Check(f"config:{name}", "warn", f"{name} is not an integer; the default ({default_label}) is used")
    if value <= 0:
        return Check(f"config:{name}", "warn", f"{name} must be > 0; the default ({default_label}) is used")
    return Check(f"config:{name}", "pass", f"{name} = {value}", {"value": value})


def check_cache_root() -> Check:
    raw = os.environ.get("XDG_CACHE_HOME")
    if raw and raw.strip() and not Path(raw.strip()).expanduser().is_absolute():
        return Check(
            "config:XDG_CACHE_HOME",
            "warn",
            "XDG_CACHE_HOME is relative and is ignored (XDG requires an absolute path); ~/.cache is used",
        )
    return Check(
        "config:XDG_CACHE_HOME",
        "pass",
        "cache root follows XDG_CACHE_HOME" if raw and raw.strip() else "cache root uses ~/.cache (XDG_CACHE_HOME unset)",
    )


def check_api_key() -> Check:
    configured = _env("YOUTUBE_API_KEY") is not None
    if not configured:
        return Check("config:youtube_api_key", "info", "YouTube Data API key not configured (tier-2 api.* tools optional)", {"configured": False})
    client = importlib.util.find_spec("googleapiclient") is not None
    if not client:
        return Check(
            "config:youtube_api_key",
            "warn",
            "YouTube Data API key configured but google-api-python-client is missing; install the `api` extra",
            {"configured": True, "client_installed": False},
        )
    return Check("config:youtube_api_key", "pass", "YouTube Data API key configured", {"configured": True, "client_installed": True})


def check_transcript_proxy() -> Check:
    from .adapters import transcript_api

    try:
        settings = transcript_api.proxy_settings_from_env()
    except ValueError as exc:
        # These messages are static text naming variables, never their values.
        return Check("config:transcript_proxy", "fail", f"transcript proxy misconfigured: {exc}")
    return Check("config:transcript_proxy", "pass", f"transcript proxy mode: {settings.mode}", dict(settings.summary))


def check_ytdlp_network() -> Check:
    from .adapters import ytdlp_transcript

    try:
        settings = ytdlp_transcript.settings_from_env()
    except ValueError as exc:
        return Check("config:ytdlp_network", "fail", f"yt-dlp network settings invalid: {exc}")
    cookies = "file" if settings.cookies_file else "browser" if settings.cookies_from_browser else "none"
    detail: dict[str, Any] = {"proxy_configured": bool(settings.proxy), "cookies": cookies}
    if settings.cookies_file:
        exists = Path(settings.cookies_file).expanduser().is_file()
        detail["cookies_file_exists"] = exists
        if not exists:
            return Check("config:ytdlp_network", "fail", "YOUTUBE_COOKIES_FILE is set but the file does not exist", detail)
    return Check(
        "config:ytdlp_network",
        "pass",
        f"yt-dlp proxy {'configured' if settings.proxy else 'not configured'}; cookies: {cookies}",
        detail,
    )


def check_whisper() -> Check:
    from .adapters import whisper_cpp

    try:
        settings = whisper_cpp.settings_from_env()
    except whisper_cpp.LocalSttUnavailable as exc:
        return Check("config:local_stt", "fail", f"local whisper.cpp STT misconfigured: {exc}")
    if settings is None:
        return Check("config:local_stt", "info", "local whisper.cpp STT disabled (YOUTUBE_MCP_WHISPER_CPP_MODEL unset)", {"enabled": False})
    return Check(
        "config:local_stt",
        "pass",
        "local whisper.cpp STT enabled",
        {"enabled": True, "timeout_s": settings.timeout_s, "model_size_bytes": settings.model.stat().st_size},
    )


def check_embeddings() -> Check:
    from . import embeddings, visual_embeddings

    installed = importlib.util.find_spec("fastembed") is not None
    text_model = embeddings.configured_model_id()
    try:
        visual_text_model, image_model = visual_embeddings.configured_models()
    except embeddings.EmbeddingError as exc:
        return Check("config:semantic", "fail", f"visual model configuration invalid: {exc}")
    detail = {
        "fastembed_installed": installed,
        "text_model": text_model,
        "visual_image_model": image_model,
        "visual_text_model": visual_text_model,
        "local_only": os.environ.get("YOUTUBE_MCP_EMBED_LOCAL_ONLY", "").strip().lower() in {"1", "true", "yes", "on"},
    }
    if not installed:
        return Check("config:semantic", "info", "semantic/visual search backend not installed (install the `semantic` extra); lexical search works", detail)
    return Check("config:semantic", "pass", f"FastEmbed backend available (text model {text_model})", detail)


def check_resource_limits() -> list[Check]:
    checks = []
    for name, default in (
        ("YOUTUBE_MCP_MAX_RESOURCE_BYTES", "32 MiB"),
        ("YOUTUBE_MCP_MAX_VISUAL_RESOURCE_BYTES", "16 MiB"),
        ("YOUTUBE_MCP_CACHE_QUOTA_BYTES", "no quota"),
    ):
        check = _positive_int_check(name, default)
        if check is not None:
            checks.append(check)
    return checks


def check_http() -> Check:
    from . import http_transport

    try:
        settings = http_transport.HttpSettings.from_env()
    except http_transport.HttpConfigError as exc:
        return Check("config:http", "fail", f"HTTP transport settings invalid: {exc}")
    summary = settings.summary()
    detail = {k: summary[k] for k in ("auth", "token_count", "rate_limit_per_min", "rate_burst", "max_body_bytes", "allowed_hosts")}
    if not settings.token_digests:
        return Check(
            "config:http",
            "info",
            "no HTTP bearer tokens configured (fine for stdio; `serve --transport streamable-http` on a non-loopback address will refuse to start)",
            detail,
        )
    return Check("config:http", "pass", f"HTTP bearer auth configured ({len(settings.token_digests)} token(s))", detail)


def check_unknown_vars() -> Check | None:
    unknown = sorted(
        name
        for name in os.environ
        if (name.startswith("YOUTUBE_") and name not in KNOWN_ENV_VARS)
    )
    if not unknown:
        return None
    return Check(
        "config:unrecognized",
        "warn",
        "unrecognized YOUTUBE_* variables (typo?): " + ", ".join(unknown),
        {"names": unknown},
    )


def run_config_check() -> dict[str, Any]:
    checks: list[Check] = [
        check_cache_root(),
        check_api_key(),
        check_transcript_proxy(),
        check_ytdlp_network(),
        check_whisper(),
        check_embeddings(),
        *check_resource_limits(),
        check_http(),
    ]
    unknown = check_unknown_vars()
    if unknown is not None:
        checks.append(unknown)
    failures = [c for c in checks if c.status == "fail"]
    warnings = [c for c in checks if c.status == "warn"]
    return {
        "report": "config",
        "status": "fail" if failures else "ok",
        "summary": {"failures": len(failures), "warnings": len(warnings)},
        "checks": [c.as_dict() for c in checks],
    }
