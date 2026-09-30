"""Competitor adapter: jkawamoto/mcp-youtube-transcript (transcript-focused MCP).

Runs the server with its README default configuration — no flags, no proxy,
no paid scraping services — through the same MCP stdio client the in-repo
server is measured with:

    uvx --from git+https://github.com/jkawamoto/mcp-youtube-transcript mcp-youtube-transcript

For reproducibility the git source is pinned to `PINNED_REF`; override with
`BENCH_MCP_YOUTUBE_TRANSCRIPT_REF`, or replace the whole launch command with
`BENCH_MCP_YOUTUBE_TRANSCRIPT_CMD` (JSON list or shell-style string), e.g. a
locally installed `mcp-youtube-transcript`.

The published PyPI release (0.3.5 at the time of writing) does not start with
the `mcp` 2.x SDK (it imports `mcp.server.fastmcp.FastMCP`), which is why the
README's git install is used.

Installing the server needs network access, and its upstream calls cannot be
scripted from outside its process, so deterministic fixture cases report
`unsupported`. Protocol cases run deterministically only when a local launch
command is configured; everything else runs in the manual/scheduled live mode.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Any

from .. import evidence, mcp_stdio
from ..isolation import CANARY_ENV
from . import _common

NAME = "mcp_youtube_transcript"
UPSTREAM = "https://github.com/jkawamoto/mcp-youtube-transcript"
PINNED_REF = "9aa39b444c48112cacbfc6a4e635256001797c80"
CMD_ENV = "BENCH_MCP_YOUTUBE_TRANSCRIPT_CMD"
REF_ENV = "BENCH_MCP_YOUTUBE_TRANSCRIPT_REF"

DOCUMENTED_TOOLS = [
    "get_transcript",
    "get_timed_transcript",
    "get_video_info",
    "get_available_languages",
]
LIVE_TASKS = {"protocol_probe", "live_metadata", "live_transcript", "live_evidence"}

# Optional paid/proxy features the server supports; all disabled by default
# config and disclosed as such in run metadata.
OPTIONAL_PROVIDER_FLAGS = ("--webshare-proxy-username", "--http-proxy", "--https-proxy", "--scrapingant-api-key")

_probe_cache: dict[str, tuple[dict[str, Any], list[str]]] = {}


def reset() -> None:
    _probe_cache.clear()


def _ref() -> str:
    return os.environ.get(REF_ENV) or PINNED_REF


def command() -> list[str]:
    override = os.environ.get(CMD_ENV)
    if override:
        try:
            parsed = json.loads(override)
        except ValueError:
            parsed = shlex.split(override)
        if isinstance(parsed, list) and parsed and all(isinstance(p, str) for p in parsed):
            return parsed
        raise ValueError(f"{CMD_ENV} must be a JSON list or shell-style command string")
    return ["uvx", "--from", f"git+{UPSTREAM}@{_ref()}", "mcp-youtube-transcript"]


def _local_command_configured() -> bool:
    return bool(os.environ.get(CMD_ENV))


def _enabled_optional_providers(cmd: list[str]) -> list[str]:
    return [flag.lstrip("-") for flag in OPTIONAL_PROVIDER_FLAGS if any(a.startswith(flag) for a in cmd)]


def _resolve_version(cmd: list[str]) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        cp = subprocess.run(
            [*cmd, "--version"], capture_output=True, text=True, timeout=300, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (cp.stdout or "").strip().splitlines()
    if cp.returncode != 0 or not out:
        return None
    # "mcp-youtube-transcript, version 1.9.9" -> "1.9.9"
    return out[-1].rsplit(" ", 1)[-1].strip()


def describe(mode: str = "deterministic") -> dict[str, Any]:
    cmd = command()
    # Resolving the version may install the server from git, so only do it
    # when network is expected (live) or a local command was configured.
    version = _resolve_version(cmd) if (mode == "live" or _local_command_configured()) else None
    return {
        "name": NAME,
        "upstream": UPSTREAM,
        "ref": None if _local_command_configured() else _ref(),
        "version": version,
        "kind": "external MCP server over stdio (default README configuration)",
        "launch": " ".join(cmd),
        "config": "README default: no flags, no proxy, no paid services",
        "dependencies": {"uv": _common.dist_version("uv") or ("present" if shutil.which("uv") else None)},
        "enabled_optional_providers": _enabled_optional_providers(cmd),
        "notes": "PyPI 0.3.5 fails to import under mcp 2.x; the README git install is used.",
    }


def supports(task: str, mode: str) -> tuple[bool, str | None]:
    if mode == "live":
        if task in LIVE_TASKS:
            return True, None
        return False, f"task {task!r} has no equivalent in this server's public tools"
    if task == "protocol_probe" and _local_command_configured():
        return True, None
    if task == "protocol_probe":
        return False, f"launching the server requires a network install (uvx); set {CMD_ENV} or use --mode live"
    return False, "deterministic fixtures cannot be injected into an external server process"


# ---------------------------------------------------------------------------
# Output mapping (unit-tested with fake sessions)
# ---------------------------------------------------------------------------


def _url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _machine_code(call: mcp_stdio.ToolCall) -> tuple[str | None, bool | None]:
    payload = call.payload()
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and err.get("code"):
            return str(err["code"]), err.get("recoverable")
        if isinstance(payload.get("code"), str):
            return payload["code"], payload.get("recoverable")
    return None, None


def _error_message(call: mcp_stdio.ToolCall | None) -> str | None:
    """Short prose error (this server has no machine-readable error codes)."""
    if call is None:
        return None
    return call.text[:200] if call.is_error else None


def normalize_video_info(call: mcp_stdio.ToolCall, languages: Any = None) -> dict[str, Any]:
    payload = call.payload()
    if call.is_error or not isinstance(payload, dict):
        return {
            "ok": False,
            "exception": None,
            "error_code": None,
            "recoverable": None,
            "error_message": _error_message(call),
        }
    langs = _parse_languages(languages)
    return {
        "ok": True,
        "exception": None,
        "error_code": None,
        "recoverable": None,
        "title": payload.get("title"),
        "channel": payload.get("uploader"),
        "channel_id": None,
        "duration_s": _common.parse_humanized_duration(payload.get("duration")),
        "duration_raw": payload.get("duration"),
        "has_transcript": (bool(langs) if langs is not None else None),
        "available_caption_langs": langs,
    }


def _parse_languages(call: Any) -> list[str] | None:
    if not isinstance(call, mcp_stdio.ToolCall) or call.is_error:
        return None
    payload = call.payload()
    if isinstance(payload, dict):
        for key in ("languages", "result", "available_languages"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if isinstance(payload, list):
        codes = []
        for item in payload:
            raw = (item.get("language_code") or item.get("code")) if isinstance(item, dict) else item
            # youtube-transcript-api renders tracks as 'en ("English")[TRANSLATABLE]'.
            found = re.match(r"\s*([A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*)", raw) if isinstance(raw, str) else None
            if found:
                codes.append(found.group(1))
        return codes
    if isinstance(payload, str):
        # Text form, e.g. "en: English (manual)\nde: German (generated)".
        found = re.findall(r"^\s*-?\s*([a-z]{2,3}(?:-[A-Za-z0-9]+)?)\b", payload, flags=re.M)
        return found or None
    return None


def normalize_transcript(pages: list[mcp_stdio.ToolCall], requested_lang: str) -> dict[str, Any]:
    """Map get_timed_transcript pages to the adapter-neutral transcript shape."""
    first = pages[0] if pages else None
    if first is None or first.is_error or not isinstance(first.payload(), dict):
        return {
            "ok": False,
            "exception": None,
            "error_code": None,
            "recoverable": None,
            "provider": None,
            "provenance": {"requested_lang": None},
            "error_message": _error_message(first),
            "segments": [],
        }
    segments: list[dict[str, Any]] = []
    for page in pages:
        payload = page.payload()
        if not isinstance(payload, dict):
            break
        for snip in payload.get("snippets") or []:
            start = float(snip.get("start", 0.0))
            segments.append(
                {
                    "start_s": start,
                    "end_s": start + float(snip.get("duration", 0.0)),
                    "text": str(snip.get("text", "")),
                }
            )
    text = " ".join(s["text"] for s in segments).strip()
    return {
        "ok": True,
        "exception": None,
        "error_code": None,
        "recoverable": None,
        "source": None,
        "validated": None,
        "lang": None,
        "requested_lang": None,
        "is_generated": None,
        "provider": None,
        "method": None,
        "attempts": [],
        "attempt_count": 0,
        "text_sha256": _common.sha256_text(text),
        "segment_count": len(segments),
        # Only what the server itself returns counts toward provenance.
        "provenance": {
            "video_id": None,
            "provider": None,
            "method": None,
            "fetched_at": None,
            "requested_lang": None,
            "actual_lang": None,
            "transcript_type": None,
            "validation_state": None,
            "attempts": None,
            "content_hash": None,
        },
        "segments": segments,
        "_requested_lang": requested_lang,
    }


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _server(mode: str, cwd: str) -> mcp_stdio.ServerCommand:
    cmd = command()
    extra = dict(CANARY_ENV) if mode == "deterministic" else {}
    return mcp_stdio.ServerCommand(command=cmd[0], args=cmd[1:], env=mcp_stdio.base_env(extra), cwd=cwd)


def _probe(mode: str) -> tuple[dict[str, Any], list[str]]:
    if mode not in _probe_cache:
        with tempfile.TemporaryDirectory(prefix="bench-competitor-") as tmp:
            _probe_cache[mode] = _common.protocol_probe(
                _server(mode, tmp),
                documented_tools=DOCUMENTED_TOOLS,
                transcript_tool="get_transcript",
                invalid_args={"url": _common.INVALID_URL},
                machine_code=_machine_code,
                timeout_s=300,
            )
    probe, scan = _probe_cache[mode]
    return json.loads(json.dumps(probe)), list(scan)


async def _fetch_timed(session: mcp_stdio.Session, video_id: str, lang: str) -> list[mcp_stdio.ToolCall]:
    pages: list[mcp_stdio.ToolCall] = []
    cursor = None
    for _ in range(1000):
        args: dict[str, Any] = {"url": _url(video_id), "lang": lang}
        if cursor:
            args["next_cursor"] = cursor
        call = await session.call("get_timed_transcript", args)
        pages.append(call)
        payload = call.payload()
        cursor = payload.get("next_cursor") if isinstance(payload, dict) and not call.is_error else None
        if not cursor:
            break
    return pages


def _session(script) -> tuple[Any, list[str]]:
    with tempfile.TemporaryDirectory(prefix="bench-competitor-") as tmp:
        result, stderr = mcp_stdio.run_session(_server("live", tmp), script, timeout_s=300)
    return result, [stderr]


def run_case(
    task: str,
    inputs: dict[str, Any],
    *,
    case: dict[str, Any] | None = None,
    mode: str = "deterministic",
) -> dict[str, Any]:
    if task == "protocol_probe":
        probe, scan = _probe(mode)
        probe["_scan"] = scan
        return probe

    video = inputs["video_id"]
    lang = inputs.get("lang", "en")

    if task == "live_metadata":

        async def script(session: mcp_stdio.Session) -> dict[str, Any]:
            info = await session.call("get_video_info", {"url": _url(video)})
            langs = await session.call("get_available_languages", {"url": _url(video)})
            return normalize_video_info(info, langs)

        result, scan = _session(script)
        result["_scan"] = scan
        return result

    if task in ("live_transcript", "live_evidence"):

        async def script(session: mcp_stdio.Session) -> dict[str, Any]:
            return normalize_transcript(await _fetch_timed(session, video, lang), lang)

        result, scan = _session(script)
        segments = result.pop("segments")
        result.pop("_requested_lang", None)
        if task == "live_transcript":
            result["_scan"] = scan
            return result
        return {
            "ok": result["ok"],
            "exception": None,
            "provider": None,
            "error_message": result.get("error_message"),
            "hits": evidence.locate(segments, inputs["query"]),
            "_scan": scan,
        }

    raise ValueError(f"unsupported task for {NAME}: {task}")
