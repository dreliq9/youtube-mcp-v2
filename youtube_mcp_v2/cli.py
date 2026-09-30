"""youtube-mcp-v2 command line.

    youtube-mcp-v2                      # stdio MCP server (historical default)
    youtube-mcp-v2 serve [--transport streamable-http ...]
    youtube-mcp-v2 doctor [--json]
    youtube-mcp-v2 config check [--json]
    youtube-mcp-v2 cache status [--json]
    youtube-mcp-v2 --version

Diagnostics never print secret values; see redaction.py.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__


def _serve() -> int:
    # Lazy import keeps diagnostics usable even when diagnosing a server-import
    # problem, and preserves the historical no-argument launch behavior.
    from .server import mcp

    mcp.run()
    return 0


def _serve_http(args: argparse.Namespace) -> int:
    from . import http_transport

    try:
        settings = http_transport.HttpSettings.from_env(
            host=args.host,
            port=args.port,
            path=args.path,
            stateless=not args.stateful,
            json_response=args.json_response,
            allow_unauthenticated=args.allow_unauthenticated or None,
            allowed_hosts=args.allowed_host or None,
        )
        settings.validate()
    except http_transport.HttpConfigError as exc:
        sys.stderr.write(f"youtube-mcp-v2: {exc}\n")
        return 2
    return http_transport.serve(settings, log=lambda line: sys.stderr.write(line + "\n"))


def _write_report(report: dict[str, Any], *, as_json: bool, title: str) -> int:
    from .doctor import render_json, render_text
    from .redaction import redact

    report = redact(report)
    if as_json:
        sys.stdout.write(render_json(report))
    elif report.get("report") == "cache":
        from .cache_status import render_text as render_cache

        sys.stdout.write(render_cache(report))
    elif title == "doctor":
        sys.stdout.write(render_text(report))
    else:
        glyph = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "info": "INFO"}
        lines = [f"youtube-mcp-v2 {__version__} {title}", f"overall: {report['status'].upper()}", ""]
        lines += [f"[{glyph[c['status']]}] {c['message']}" for c in report["checks"]]
        lines += ["", f"failures: {report['summary']['failures']}  warnings: {report['summary']['warnings']}"]
        sys.stdout.write("\n".join(lines) + "\n")
    return 0 if report["status"] == "ok" else 1


def _doctor(*, as_json: bool) -> int:
    from .doctor import run_doctor

    return _write_report(run_doctor(), as_json=as_json, title="doctor")


def _config_check(*, as_json: bool) -> int:
    from .config_check import run_config_check

    return _write_report(run_config_check(), as_json=as_json, title="config check")


def _cache_status(*, as_json: bool) -> int:
    from .cache_status import run_cache_status

    return _write_report(run_cache_status(), as_json=as_json, title="cache status")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youtube-mcp-v2",
        description="Evidence-grade YouTube MCP server",
    )
    parser.add_argument("--version", action="version", version=f"youtube-mcp-v2 {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser(
        "serve",
        help="run the MCP server (stdio by default; also the default with no command)",
    )
    serve.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    serve.add_argument("--host", default=None, help="HTTP bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=None, help="HTTP port (default 8000)")
    serve.add_argument("--path", default=None, help="MCP endpoint path (default /mcp)")
    serve.add_argument(
        "--stateful",
        action="store_true",
        help="use Mcp-Session-Id sessions and SSE streams (default: stateless JSON)",
    )
    serve.add_argument(
        "--json-response",
        dest="json_response",
        action="store_true",
        default=None,
        help="force JSON responses (default when stateless)",
    )
    serve.add_argument(
        "--sse-response",
        dest="json_response",
        action="store_false",
        help="force SSE-framed responses (default when stateful)",
    )
    serve.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help="permit a non-loopback bind without bearer tokens (only behind an authenticating proxy)",
    )
    serve.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help="public Host header value to accept (DNS-rebinding protection); repeatable",
    )

    doctor = subparsers.add_parser(
        "doctor",
        help="check local dependencies, cache writability, and capabilities",
    )
    doctor.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")

    config = subparsers.add_parser("config", help="configuration diagnostics")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_check = config_sub.add_parser("check", help="validate environment configuration (values are never printed)")
    config_check.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")

    cache = subparsers.add_parser("cache", help="cache diagnostics")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    cache_status = cache_sub.add_parser("status", help="storage usage, quota, and cache health")
    cache_status.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        return _serve()

    args = _parser().parse_args(args_list)
    if args.command == "serve":
        if args.transport == "streamable-http":
            return _serve_http(args)
        return _serve()
    if args.command == "doctor":
        return _doctor(as_json=bool(args.json))
    if args.command == "config":
        return _config_check(as_json=bool(args.json))
    if args.command == "cache":
        return _cache_status(as_json=bool(args.json))
    return _serve()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
