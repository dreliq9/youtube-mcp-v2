"""Helpers shared by benchmark adapters (normalisation, probes, versions)."""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
import shutil
from typing import Any

from .. import mcp_stdio

INVALID_URL = "not a youtube url"
UNKNOWN_TOOL = "bench.nonexistent_tool"


def dist_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def binary_available(name: str) -> bool:
    return shutil.which(name) is not None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def exception_label(exc: BaseException) -> str:
    """Type plus a short message. Never used as a pass signal on its own."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def schema_checks(tools: list[dict[str, Any]]) -> dict[str, bool]:
    return {
        "all_object": all(
            isinstance(t.get("input_schema"), dict) and t["input_schema"].get("type") == "object"
            for t in tools
        ),
        "all_described": all(bool((t.get("description") or "").strip()) for t in tools),
    }


def protocol_probe(
    server: mcp_stdio.ServerCommand,
    *,
    documented_tools: list[str],
    transcript_tool: str,
    invalid_args: dict[str, Any],
    machine_code: Any,
    timeout_s: float,
) -> tuple[dict[str, Any], list[str]]:
    """Launch `server` over stdio and probe install/launch/list/first-call.

    `machine_code(tool_call) -> (code | None, recoverable | None)` extracts a
    server-specific machine-readable error from the invalid-input call.
    Returns (normalised probe, blobs to scan for secret leaks).
    """

    async def script(session: mcp_stdio.Session) -> dict[str, Any]:
        tools = await session.list_tools()
        names = [t["name"] for t in tools]
        invalid = await session.call(transcript_tool, invalid_args)
        code, recoverable = machine_code(invalid)
        try:
            unknown = await session.call(UNKNOWN_TOOL, {})
            rejected = unknown.is_error
        except Exception:  # noqa: BLE001 - JSON-RPC error is a valid rejection
            rejected = True
        after = await session.list_tools()
        return {
            "launched": True,
            "init_ms": round(session.init_ms, 1),
            "server_name": session.server_name,
            "server_version": session.server_version,
            "protocol_version": session.protocol_version,
            "tool_count": len(names),
            "tool_names": sorted(names),
            "transcript_tool": transcript_tool if transcript_tool in names else None,
            "documented_tools_present": all(t in names for t in documented_tools),
            "missing_documented_tools": [t for t in documented_tools if t not in names],
            "schemas": schema_checks(tools),
            "invalid_input": {
                "handled": invalid.is_error or code is not None,
                "machine_code": code,
                "recoverable": recoverable,
                "_text": invalid.text,
            },
            "unknown_tool": {"rejected": rejected, "survived": len(after) == len(names)},
        }

    try:
        result, stderr = mcp_stdio.run_session(server, script, timeout_s=timeout_s)
    except mcp_stdio.SessionError as exc:
        return {"launched": False, "error": str(exc)}, [exc.stderr]
    scan = [stderr, result["invalid_input"].pop("_text", "")]
    return result, scan


_DURATION_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def parse_humanized_duration(text: Any) -> int | None:
    """Parse strings like '19 seconds', '3 minutes', 'an hour' into seconds."""
    if not isinstance(text, str):
        return None
    total = 0
    found = False
    for amount, unit in re.findall(r"(\d+|an?|one)\s+(second|minute|hour|day)s?", text.lower()):
        value = 1 if amount in ("a", "an", "one") else int(amount)
        total += value * _DURATION_UNITS[unit]
        found = True
    return total if found else None
