from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def _serve() -> int:
    # Lazy import keeps `doctor` usable even when diagnosing a server-import
    # problem, and preserves the historical no-argument launch behavior.
    from .server import mcp

    mcp.run()
    return 0


def _doctor(*, as_json: bool) -> int:
    from .doctor import render_json, render_text, run_doctor

    report = run_doctor()
    renderer = render_json if as_json else render_text
    sys.stdout.write(renderer(report))
    return 0 if report["status"] == "ok" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youtube-mcp-v2",
        description="Evidence-grade YouTube MCP server",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "serve",
        help="run the stdio MCP server (also the default with no command)",
    )
    doctor = subparsers.add_parser(
        "doctor",
        help="check local dependencies, cache writability, and capabilities",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable JSON report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        return _serve()

    args = _parser().parse_args(args_list)
    if args.command == "serve":
        return _serve()
    if args.command == "doctor":
        return _doctor(as_json=bool(args.json))
    return _serve()
