"""Benchmark adapter for the in-repo youtube-mcp-v2 implementation.

* Deterministic cases call the registered tool functions in-process so
  `bench/fixtures/upstream.py` can script the outermost upstream boundary
  while the server's real parsing/fallback/validation/cache code runs.
* Protocol cases launch the real stdio server (`python -m youtube_mcp_v2`).
* Live cases also go through the stdio server, exactly like competitors.

Outputs never contain transcript text — only hashes, counts, and timestamps.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest import mock

from youtube_mcp_v2 import __version__, skeleton, validate
from youtube_mcp_v2.adapters.url import parse_video_id

from .. import evidence, mcp_stdio
from ..isolation import CANARY_ENV
from . import _common

NAME = "youtube_mcp_v2"
ROOT = Path(__file__).resolve().parents[2]

DOCUMENTED_TOOLS = [
    "inspect.video",
    "transcript.get",
    "scrape.search",
    "skeleton.build",
    "skeleton.list",
    "skeleton.get",
    "skeleton.diff",
    "skeleton.expire",
    "skeleton.index",
    "corpus.compose",
    "corpus.hydrate",
    "corpus.prepare",
    "corpus.search",
    "corpus.visual_search",
    "corpus.clip_plan",
    "frame.get",
    "audio.get",
    "media.materialize",
    "api.search",
    "api.channel_stats",
    "api.trending",
    "api.video_categories",
]

DETERMINISTIC_TASKS = {
    "parse_video_id",
    "validate_transcript",
    "transcript_guard",
    "skeleton_handle",
    "install_metadata",
    "protocol_probe",
    "inspect_video",
    "transcript_fetch",
    "transcript_chunked",
    "transcript_timed_pages",
    "transcript_evidence",
    "api_search",
}
LIVE_TASKS = {"protocol_probe", "install_metadata", "live_metadata", "live_transcript", "live_evidence"}

# Opt-in only: names of host env vars forwarded to the live server. Optional
# providers are never enabled silently (BENCHMARK.md competitive rules).
PASSTHROUGH_ENV = "BENCH_PASSTHROUGH_ENV"

_probe_cache: dict[str, tuple[dict[str, Any], list[str]]] = {}


def reset() -> None:
    _probe_cache.clear()


def _passthrough_names() -> list[str]:
    raw = os.environ.get(PASSTHROUGH_ENV, "")
    return sorted({n.strip() for n in raw.split(",") if n.strip().startswith("YOUTUBE_")})


def describe(mode: str = "deterministic") -> dict[str, Any]:
    cookies = (
        "file"
        if os.environ.get("YOUTUBE_COOKIES_FILE")
        else "browser"
        if os.environ.get("YOUTUBE_COOKIES_FROM_BROWSER")
        else "none"
    )
    return {
        "name": NAME,
        "version": __version__,
        "dist_version": _common.dist_version("youtube-mcp-v2"),
        "kind": "in-process tool calls with scripted upstream fixtures; mcp-stdio for protocol and live cases",
        "launch": f"{Path(sys.executable).name} -m youtube_mcp_v2",
        "config": "repository defaults; no optional providers unless forwarded via BENCH_PASSTHROUGH_ENV",
        "dependencies": {
            name: _common.dist_version(name)
            for name in ("mcp", "youtube-transcript-api", "yt-dlp", "httpx")
        },
        # Host availability (booleans only, never values).
        "host_optional_providers": {
            "youtube_data_api_key": bool(os.environ.get("YOUTUBE_API_KEY")),
            "google_api_client_installed": _common.dist_version("google-api-python-client") is not None,
            "whisper_cpp_model": bool(os.environ.get("YOUTUBE_MCP_WHISPER_CPP_MODEL")),
            "transcript_proxy": any(
                os.environ.get(n)
                for n in (
                    "YOUTUBE_TRANSCRIPT_PROXY",
                    "YOUTUBE_TRANSCRIPT_HTTP_PROXY",
                    "YOUTUBE_TRANSCRIPT_HTTPS_PROXY",
                    "YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME",
                )
            ),
            "ytdlp_proxy": bool(os.environ.get("YOUTUBE_YTDLP_PROXY")),
            "cookies_mode": cookies,
            "fastembed_installed": _common.dist_version("fastembed") is not None,
            "yt_dlp_binary": _common.binary_available("yt-dlp"),
            "ffmpeg_binary": _common.binary_available("ffmpeg"),
            "dotenv_file_present": (ROOT / ".env").exists(),
        },
        # What the server under test actually received in this run.
        "enabled_optional_providers": (
            ["synthetic canaries only (deterministic isolation)"]
            if mode == "deterministic"
            else _passthrough_names()
        ),
    }


def supports(task: str, mode: str) -> tuple[bool, str | None]:
    allowed = DETERMINISTIC_TASKS if mode == "deterministic" else LIVE_TASKS
    if task in allowed:
        return True, None
    return False, f"task {task!r} not implemented for {mode} mode"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _warning_kind(message: str) -> str:
    lower = message.lower()
    if "transcript empty" in lower:
        return "empty"
    if "low word-rate" in lower:
        return "low_word_rate"
    if "high word-rate" in lower:
        return "high_word_rate"
    if "lang fallback" in lower:
        return "lang_fallback"
    if "truncation marker" in lower:
        return "truncation_marker"
    if "non-monotonic timestamp" in lower:
        return "non_monotonic_timestamp"
    if "duration unavailable" in lower:
        return "duration_unavailable"
    if "fallback used" in lower:
        return "fallback_used"
    return "other"


def _segments_text(segments: list[dict[str, Any]]) -> str:
    return " ".join(str(s.get("text", "")) for s in segments).strip()


def normalize_transcript_envelope(env: dict[str, Any]) -> dict[str, Any]:
    """Map a transcript.get envelope to the adapter-neutral scoring shape."""
    error = env.get("error") or {}
    data = env.get("data") or {}
    acquisition = (env.get("provenance") or {}).get("acquisition") or {}
    attempts = [
        {
            "provider": a.get("provider"),
            "outcome": a.get("outcome"),
            "detail_code": a.get("detail_code"),
        }
        for a in acquisition.get("attempts") or []
        if isinstance(a, dict)
    ]
    if "text" in data:
        text = data.get("text") or ""
    else:
        text = _segments_text(data.get("segments") or [])
    is_generated = data.get("is_generated")
    ok = env.get("error") is None
    return {
        "ok": ok,
        "exception": None,
        "error_code": error.get("code"),
        "recoverable": error.get("recoverable"),
        "source": env.get("source"),
        "validated": env.get("validated"),
        "warning_kinds": [_warning_kind(w) for w in env.get("warnings") or []],
        "lang": data.get("lang"),
        "requested_lang": data.get("requested_lang"),
        "is_generated": is_generated,
        "provider": acquisition.get("provider"),
        "method": acquisition.get("method"),
        "attempts": attempts,
        "attempt_count": len(attempts),
        "text_sha256": _common.sha256_text(text) if ok else None,
        "segment_count": data.get("total_segments"),
        "provenance": {
            "video_id": data.get("id"),
            "provider": acquisition.get("provider"),
            "method": acquisition.get("method"),
            "fetched_at": env.get("fetched_at") if ok else None,
            "requested_lang": data.get("requested_lang"),
            "actual_lang": data.get("lang"),
            "transcript_type": (
                None if is_generated is None else ("generated" if is_generated else "manual")
            ),
            "validation_state": None if not ok else ("pass" if env.get("validated") else "fail"),
            "attempts": len(attempts) or None,
            # Not exposed by the server on main yet (EVIDENCE_MODEL revision block).
            "content_hash": (env.get("provenance") or {}).get("revision", {}).get("content_sha256")
            if isinstance((env.get("provenance") or {}).get("revision"), dict)
            else None,
        },
    }


def _normalize_inspect(env: dict[str, Any]) -> dict[str, Any]:
    error = env.get("error") or {}
    data = env.get("data") or {}
    fields = (
        "title",
        "channel",
        "channel_id",
        "duration_s",
        "has_transcript",
        "available_caption_langs",
        "lang_default",
        "livestream",
        "age_gated",
        "embed_allowed",
    )
    return {
        "ok": env.get("error") is None,
        "exception": None,
        "error_code": error.get("code"),
        "recoverable": error.get("recoverable"),
        "source": env.get("source"),
        **{f: data.get(f) for f in fields},
    }


def _normalize_error_only(env: dict[str, Any]) -> dict[str, Any]:
    error = env.get("error") or {}
    return {
        "ok": env.get("error") is None,
        "exception": None,
        "error_code": error.get("code"),
        "recoverable": error.get("recoverable"),
        "source": env.get("source"),
    }


def _machine_code(call: mcp_stdio.ToolCall) -> tuple[str | None, bool | None]:
    payload = call.payload()
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return payload["error"].get("code"), payload["error"].get("recoverable")
    return None, None


# ---------------------------------------------------------------------------
# Deterministic, in-process tasks
# ---------------------------------------------------------------------------


def _isolated_cache(stack: ExitStack) -> Path:
    from youtube_mcp_v2 import cache
    from youtube_mcp_v2.adapters import data_api, page

    tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="ytmcp-bench-")))
    cache_dir = tmp / "youtube-mcp"
    stack.enter_context(mock.patch.object(cache, "CACHE_DIR", cache_dir))
    stack.enter_context(mock.patch.object(cache, "CACHE_PATH", cache_dir / "v2.sqlite"))
    stack.enter_context(mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(tmp)}))
    # Module-level HTTP/API clients must not leak between cases.
    stack.enter_context(mock.patch.object(page, "_client", None))
    stack.enter_context(mock.patch.object(data_api, "_client", None))
    return tmp


def _guarded(fn, normalize, raw: list[str] | None = None):
    try:
        env = fn()
    except Exception as exc:  # noqa: BLE001 - survival is what is being measured
        return {"ok": False, "exception": _common.exception_label(exc)}
    if raw is not None:
        # The full envelope (messages, attempt details) is scanned for secret
        # canaries even though only normalised fields are scored/reported.
        raw.append(json.dumps(env, default=str))
    return normalize(env)


def _timed_pages(url_or_id: str, lang: str) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    from youtube_mcp_v2.tools.transcript import transcript_get

    segments: list[dict[str, Any]] = []
    cursor = None
    pages = 0
    first_env: dict[str, Any] = {}
    while True:
        env = transcript_get(url_or_id, mode="timed", lang=lang, cursor=cursor)
        pages += 1
        if pages == 1:
            first_env = env
        if env.get("error") is not None:
            break
        segments.extend(env["data"]["segments"])
        cursor = env["data"].get("next_cursor")
        if cursor is None or pages > 1000:
            break
    return segments, first_env, pages


def _run_deterministic(task: str, inputs: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    from ..fixtures import upstream

    raw: list[str] = []
    with ExitStack() as stack:
        _isolated_cache(stack)
        calls = upstream.apply(stack, case.get("fixture"), video_id=case.get("video_id"))
        result = _run_fixture_task(task, inputs, calls, raw)
    result["_scan"] = raw
    return result


def _run_fixture_task(task: str, inputs: dict[str, Any], calls: Any, raw: list[str]) -> dict[str, Any]:
    from youtube_mcp_v2.tools.api import api_search
    from youtube_mcp_v2.tools.inspect import inspect_video
    from youtube_mcp_v2.tools.transcript import transcript_get

    if task == "inspect_video":
        return _guarded(lambda: inspect_video(inputs["url_or_id"]), _normalize_inspect, raw)

    if task == "api_search":
        return _guarded(lambda: api_search(inputs["query"]), _normalize_error_only, raw)

    if task == "transcript_fetch":
        repeat = int(inputs.get("repeat", 1))
        args = {k: v for k, v in inputs.items() if k != "repeat"}
        results = []
        counts_after_first: dict[str, int] = {}
        for i in range(repeat):
            results.append(
                _guarded(lambda: transcript_get(**args), normalize_transcript_envelope, raw)
            )
            if i == 0:
                counts_after_first = dict(calls.counts)
        final = dict(results[-1])
        final["upstream_calls"] = dict(calls.counts)
        if repeat > 1:
            final["first"] = results[0]
            final["upstream_calls_after_first"] = sum(calls.counts.values()) - sum(
                counts_after_first.values()
            )
        return final

    if task == "transcript_chunked":
        env = transcript_get(
            inputs["url_or_id"],
            mode="chunked",
            chunk_tokens=int(inputs.get("chunk_tokens", 500)),
            chunk_overlap=int(inputs.get("chunk_overlap", 50)),
        )
        if env.get("error") is not None:
            return normalize_transcript_envelope(env)
        chunks = env["data"]["chunks"]
        timed, _, _ = _timed_pages(inputs["url_or_id"], "en")
        chunk_texts = [c["text"] for c in chunks]
        covered = sum(1 for seg in timed if any(seg["text"] in t for t in chunk_texts))
        starts = [c["start_s"] for c in chunks]
        text_tokens = sum(max(1, len(s["text"]) // 4) for s in timed)
        return {
            "ok": True,
            "exception": None,
            "chunk_count": len(chunks),
            "n_consistent": all(c["n"] == len(chunks) for c in chunks),
            "ordered": starts == sorted(starts),
            "segment_coverage": round(covered / len(timed), 4) if timed else 0.0,
            "max_chunk_tokens": max(c["token_estimate"] for c in chunks),
            "duplication_ratio": round(sum(c["token_estimate"] for c in chunks) / text_tokens, 3),
            "response_bytes": len(json.dumps(env).encode("utf-8")),
        }

    if task == "transcript_timed_pages":
        segments, first_env, pages = _timed_pages(inputs["url_or_id"], inputs.get("lang", "en"))
        starts = [s["start_s"] for s in segments]
        total = (first_env.get("data") or {}).get("total_segments")
        return {
            "ok": first_env.get("error") is None,
            "exception": None,
            "pages": pages,
            "total_segments": total,
            "collected_segments": len(segments),
            "unique_starts": len(set(starts)),
            "monotonic": starts == sorted(starts),
            "text_sha256": _common.sha256_text(_segments_text(segments)),
        }

    if task == "transcript_evidence":
        segments, first_env, _ = _timed_pages(inputs["url_or_id"], inputs.get("lang", "en"))
        norm = normalize_transcript_envelope(first_env)
        return {
            "ok": norm["ok"],
            "exception": None,
            "provider": norm["provider"],
            "hits": evidence.locate(segments, inputs["query"]),
        }

    raise ValueError(f"unsupported deterministic task: {task}")


# ---------------------------------------------------------------------------
# Protocol + live (stdio) tasks
# ---------------------------------------------------------------------------


def _server(mode: str, cache_dir: str) -> mcp_stdio.ServerCommand:
    extra = {"XDG_CACHE_HOME": cache_dir, "PYTHONPATH": str(ROOT)}
    if mode == "deterministic":
        extra.update(CANARY_ENV)
    else:
        extra.update({n: os.environ[n] for n in _passthrough_names() if n in os.environ})
    return mcp_stdio.ServerCommand(
        command=sys.executable,
        args=["-m", "youtube_mcp_v2"],
        env=mcp_stdio.base_env(extra),
        cwd=cache_dir,
    )


def _probe(mode: str) -> tuple[dict[str, Any], list[str]]:
    if mode not in _probe_cache:
        with tempfile.TemporaryDirectory(prefix="ytmcp-bench-probe-") as tmp:
            _probe_cache[mode] = _common.protocol_probe(
                _server(mode, tmp),
                documented_tools=DOCUMENTED_TOOLS,
                transcript_tool="transcript.get",
                invalid_args={"url_or_id": _common.INVALID_URL},
                machine_code=_machine_code,
                timeout_s=60,
            )
    probe, scan = _probe_cache[mode]
    return json.loads(json.dumps(probe)), list(scan)


def _install_metadata() -> dict[str, Any]:
    try:
        dist = importlib.metadata.distribution("youtube-mcp-v2")
    except importlib.metadata.PackageNotFoundError:
        return {"import_ok": True, "installed": False, "version": __version__}
    scripts = {ep.name for ep in dist.entry_points if ep.group == "console_scripts"}
    return {
        "import_ok": True,
        "installed": True,
        "version": __version__,
        "version_consistent": dist.version == __version__,
        "console_script": "youtube-mcp-v2" in scripts,
        "requires_python": dist.metadata.get("Requires-Python"),
    }


def _live_session(script) -> tuple[Any, list[str]]:
    with tempfile.TemporaryDirectory(prefix="ytmcp-bench-live-") as tmp:
        result, stderr = mcp_stdio.run_session(_server("live", tmp), script, timeout_s=180)
    return result, [stderr]


def _run_live(task: str, inputs: dict[str, Any]) -> dict[str, Any]:
    video = inputs["video_id"]

    if task == "live_metadata":

        async def script(session: mcp_stdio.Session) -> dict[str, Any]:
            call = await session.call("inspect.video", {"url_or_id": video})
            return _normalize_inspect(call.payload() if isinstance(call.payload(), dict) else {})

        result, scan = _live_session(script)
        result["_scan"] = scan
        return result

    lang = inputs.get("lang", "en")

    async def fetch_timed(session: mcp_stdio.Session) -> dict[str, Any]:
        segments: list[dict[str, Any]] = []
        cursor = None
        first: dict[str, Any] | None = None
        for _ in range(1000):
            args: dict[str, Any] = {"url_or_id": video, "mode": "timed", "lang": lang}
            if cursor:
                args["cursor"] = cursor
            env = (await session.call("transcript.get", args)).payload()
            env = env if isinstance(env, dict) else {}
            first = first or env
            if env.get("error") is not None:
                break
            segments.extend(env["data"]["segments"])
            cursor = env["data"].get("next_cursor")
            if not cursor:
                break
        return {"first": first or {}, "segments": segments}

    fetched, scan = _live_session(fetch_timed)
    norm = normalize_transcript_envelope(fetched["first"])
    if norm["ok"]:
        norm["text_sha256"] = _common.sha256_text(_segments_text(fetched["segments"]))
        norm["segment_count"] = len(fetched["segments"])
    if task == "live_transcript":
        norm["_scan"] = scan
        return norm
    if task == "live_evidence":
        return {
            "ok": norm["ok"],
            "exception": None,
            "provider": norm["provider"],
            "error_code": norm["error_code"],
            "attempts": norm["attempts"],
            "hits": evidence.locate(fetched["segments"], inputs["query"]),
            "_scan": scan,
        }
    raise ValueError(f"unsupported live task: {task}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_case(
    task: str,
    inputs: dict[str, Any],
    *,
    case: dict[str, Any] | None = None,
    mode: str = "deterministic",
) -> dict[str, Any]:
    """Execute one benchmark task and normalise output for scoring."""
    case = case or {}

    if task == "parse_video_id":
        try:
            video_id = parse_video_id(str(inputs.get("value", "")))
        except Exception as exc:  # benchmark records the public failure class
            return {"ok": False, "error_type": type(exc).__name__}
        return {"ok": True, "video_id": video_id}

    if task == "validate_transcript":
        args = dict(inputs)
        word_count = args.pop("word_count", None)
        if word_count is not None:
            args["text"] = " ".join("word" for _ in range(int(word_count)))
        validated, warnings = validate.validate_transcript(**args)
        return {
            "validated": validated,
            "warning_kinds": [_warning_kind(w) for w in warnings],
        }

    if task == "transcript_guard":
        from youtube_mcp_v2.tools.transcript import transcript_get

        env = transcript_get(**inputs)
        error = env.get("error") or {}
        return {
            "error_code": error.get("code"),
            "recoverable": error.get("recoverable"),
        }

    if task == "skeleton_handle":
        return {"valid": skeleton.is_valid_handle(str(inputs.get("handle", "")))}

    if task == "install_metadata":
        return _install_metadata()

    if task == "protocol_probe":
        probe, scan = _probe(mode)
        probe["_scan"] = scan
        return probe

    if mode == "live":
        return _run_live(task, inputs)

    return _run_deterministic(task, inputs, case)
