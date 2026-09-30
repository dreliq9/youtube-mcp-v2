"""Offline diagnostics for youtube-mcp-v2.

`doctor` intentionally performs no YouTube/network requests. It answers whether the
local installation is internally usable and which optional capabilities are
available without printing secret values.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .paths import CACHE_DIR


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # pass | warn | fail | info
    message: str
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _python_check() -> Check:
    minimum = (3, 10)
    current = sys.version_info[:2]
    if current >= minimum:
        return Check(
            "python",
            "pass",
            f"Python {platform.python_version()} satisfies >=3.10",
        )
    return Check(
        "python",
        "fail",
        f"Python {platform.python_version()} is unsupported; >=3.10 required",
    )


def _package_check(distribution: str, *, required: bool = True, extra: str | None = None) -> Check:
    version = _distribution_version(distribution)
    if version is not None:
        return Check(
            f"package:{distribution}",
            "pass",
            f"{distribution} {version} installed",
            {"version": version},
        )
    if not required:
        # Optional extras are capabilities, not failures.
        return Check(
            f"package:{distribution}",
            "info",
            f"{distribution} not installed" + (f" (optional `{extra}` extra)" if extra else ""),
        )
    return Check(
        f"package:{distribution}",
        "fail",
        f"{distribution} is not installed",
    )


def _backend_checks() -> list[Check]:
    """Configured provider / ML backends, presence and validity only."""
    from . import config_check

    return [
        config_check.check_transcript_proxy(),
        config_check.check_ytdlp_network(),
        config_check.check_whisper(),
        config_check.check_embeddings(),
    ]


def _disk_check(cache_dir: Path = CACHE_DIR) -> Check:
    probe = cache_dir if cache_dir.exists() else next((p for p in cache_dir.parents if p.exists()), None)
    if probe is None:
        return Check("disk", "warn", "cache volume not found")
    usage = shutil.disk_usage(probe)
    detail = {"free_bytes": usage.free, "total_bytes": usage.total}
    if usage.free < 1024 * 1024 * 1024:
        return Check("disk", "warn", "less than 1 GiB free on the cache volume (see `youtube-mcp-v2 cache status`)", detail)
    return Check("disk", "pass", f"{usage.free // (1024 * 1024)} MiB free on the cache volume", detail)


def _binary_version(path: str, flag: str) -> str | None:
    """First line of `<binary> <flag>`; local exec only, never network."""
    try:
        completed = subprocess.run(
            [path, flag], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (completed.stdout or completed.stderr or "").strip().splitlines()
    if completed.returncode != 0 or not first:
        return None
    return first[0].split(" Copyright")[0].strip()[:120]


_VERSION_FLAGS = {"yt-dlp": "--version", "ffmpeg": "-version", "ffprobe": "-version"}


def _binary_check(name: str, *, required: bool, capability: str) -> Check:
    path = shutil.which(name)
    if path:
        version = _binary_version(path, _VERSION_FLAGS.get(name, "--version"))
        return Check(
            f"binary:{name}",
            "pass",
            f"{name} available for {capability}" + (f" ({version})" if version else ""),
            {"path": path, "version": version},
        )
    return Check(
        f"binary:{name}",
        "fail" if required else "warn",
        f"{name} not found; {capability} unavailable",
    )


def _cache_check(cache_dir: Path = CACHE_DIR) -> Check:
    """Verify the configured cache root can actually be created and written."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix="doctor-",
            suffix=".tmp",
            dir=cache_dir,
            delete=True,
        ) as file:
            file.write(b"ok")
            file.flush()
    except OSError as exc:
        return Check(
            "cache",
            "fail",
            f"cache root is not writable: {cache_dir}",
            {"error_type": type(exc).__name__},
        )
    return Check(
        "cache",
        "pass",
        f"cache root writable: {cache_dir}",
        {
            "path": str(cache_dir),
            "xdg_override": bool(os.environ.get("XDG_CACHE_HOME", "").strip()),
        },
    )


def _api_key_check() -> Check:
    configured = bool(os.environ.get("YOUTUBE_API_KEY", "").strip())
    return Check(
        "youtube_api_key",
        "info",
        "YouTube Data API key configured" if configured else "YouTube Data API key not configured (tier-2 tools remain optional)",
        {"configured": configured},
    )


def _server_import_check() -> Check:
    try:
        from mcp.server import MCPServer

        from .server import mcp

        valid = isinstance(mcp, MCPServer)
    except Exception as exc:
        return Check(
            "mcp_server",
            "fail",
            "MCP server import failed",
            {"error_type": type(exc).__name__},
        )
    if valid:
        return Check("mcp_server", "pass", "MCPServer imports and initializes")
    return Check("mcp_server", "fail", "server object is not an MCPServer")


def run_doctor() -> dict[str, Any]:
    """Return a machine-readable local capability report.

    Missing ffmpeg/ffprobe/yt-dlp are warnings here because core metadata and
    transcript/search features can still work without every media capability in
    the v0.2.x line. Required Python/packages/server/cache failures make the
    overall status fail.
    """
    checks = [
        _python_check(),
        _package_check("mcp"),
        _package_check("youtube-transcript-api"),
        _package_check("yt-dlp"),
        _package_check("httpx"),
        _package_check("beautifulsoup4"),
        _package_check("python-dotenv"),
        _package_check("fastembed", required=False, extra="semantic"),
        _package_check("google-api-python-client", required=False, extra="api"),
        _binary_check("yt-dlp", required=False, capability="frame/audio acquisition"),
        _binary_check("ffmpeg", required=False, capability="frame/audio conversion"),
        _binary_check("ffprobe", required=False, capability="frame duration probing"),
        _cache_check(),
        _disk_check(),
        _api_key_check(),
        *_backend_checks(),
        _server_import_check(),
    ]

    required_failures = [check for check in checks if check.status == "fail"]
    warnings = [check for check in checks if check.status == "warn"]
    from .redaction import redact

    return redact({
        "youtube_mcp_version": __version__,
        "status": "fail" if required_failures else "ok",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "summary": {
            "failures": len(required_failures),
            "warnings": len(warnings),
        },
        "checks": [check.as_dict() for check in checks],
    })


def render_text(report: dict[str, Any]) -> str:
    glyph = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "info": "INFO"}
    lines = [
        f"youtube-mcp-v2 {report['youtube_mcp_version']} doctor",
        f"overall: {report['status'].upper()}",
        "",
    ]
    for check in report["checks"]:
        lines.append(f"[{glyph.get(check['status'], check['status'].upper())}] {check['message']}")
    lines.extend(
        [
            "",
            f"failures: {report['summary']['failures']}  warnings: {report['summary']['warnings']}",
        ]
    )
    return "\n".join(lines) + "\n"


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"
