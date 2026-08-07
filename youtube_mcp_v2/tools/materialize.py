"""Explicit materialization boundary for editor-ready source clips."""

from __future__ import annotations

from typing import Any

from .. import edit_plan, envelope
from ..adapters import video_clip


def media_materialize(
    plan_revision: str,
    max_height: int = 720,
    clip_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Materialize a bounded set of ranges from an immutable clip plan.

    This is intentionally separate from corpus.clip_plan because it performs live
    network/media acquisition and writes editor-ready MP4 assets to the local cache.
    """
    try:
        plan = edit_plan.load_plan(plan_revision)
    except FileNotFoundError as exc:
        return envelope.fail("clip_plan_not_found", str(exc), recoverable=False)
    except edit_plan.ClipPlanError as exc:
        return envelope.fail("clip_plan_invalid", str(exc), recoverable=False)

    try:
        manifest = video_clip.materialize(
            plan,
            max_height=int(max_height),
            clip_ids=clip_ids,
        )
    except (TypeError, ValueError) as exc:
        return envelope.fail("bad_materialize_request", str(exc), recoverable=False)
    except video_clip.VideoClipError as exc:
        return envelope.fail("media_materialize_failed", str(exc), recoverable=True)
    except OSError as exc:
        return envelope.fail("media_materialize_failed", str(exc), recoverable=True)

    warnings = [
        "materialized asset paths are local to the youtube-mcp host and are intended for local editor MCPs such as Declip or FCP-MCP",
        "source provenance is retained for attribution/rights review; downstream publication remains the caller's responsibility",
    ]
    if not manifest.get("complete"):
        warnings.append(
            "this is a partial materialization; call media.materialize again with remaining clip_ids if the full plan is needed"
        )
    all_cached = all(bool(asset.get("cached")) for asset in manifest.get("assets") or [])
    return envelope.ok(
        manifest,
        source="cache" if all_cached else "scrape",
        validated=True,
        warnings=warnings,
    )
