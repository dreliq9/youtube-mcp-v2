"""Deterministic benchmark adapter for the in-repo youtube-mcp-v2 implementation."""

from __future__ import annotations

from typing import Any

from youtube_mcp_v2 import skeleton, validate
from youtube_mcp_v2.adapters.url import parse_video_id
from youtube_mcp_v2.tools.transcript import transcript_get


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
    return "other"


def run_case(task: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Execute one benchmark task and normalize output for scoring."""
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
        env = transcript_get(**inputs)
        error = env.get("error") or {}
        return {
            "error_code": error.get("code"),
            "recoverable": error.get("recoverable"),
        }

    if task == "skeleton_handle":
        return {"valid": skeleton.is_valid_handle(str(inputs.get("handle", "")))}

    raise ValueError(f"unsupported benchmark task: {task}")
