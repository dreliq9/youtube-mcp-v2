"""frame.get — single-frame or contact-sheet extraction via yt-dlp + ffmpeg.

Same-host callers keep the local `path` fields. Remote-safe callers can consume
`resource_uri` fields through MCP binary resources when the artifact is within the
configured resource-size bound.
"""

from __future__ import annotations

from typing import Any, Literal

from .. import artifacts, envelope
from ..adapters import frame_extract
from ..adapters.frame_extract import FrameExtractError
from ..adapters.url import parse_video_id


def _reference(path: str, video_id: str) -> tuple[dict[str, object], list[str]]:
    try:
        ref = artifacts.reference_for_path(path, kind="frame", video_id=video_id)
    except artifacts.ArtifactError as exc:
        return {
            "resource_uri": None,
            "resource_mime_type": None,
            "resource_size_bytes": None,
            "resource_portable": False,
        }, [f"frame resource reference unavailable: {exc}"]
    warnings: list[str] = []
    if not ref.portable:
        warnings.append(
            "frame artifact exceeds portable MCP resource limit; local path remains available"
        )
    return ref.as_dict(), warnings


def frame_get(
    url_or_id: str,
    mode: Literal["single", "sheet"] = "single",
    timestamp_s: float | None = None,
    n: int = 12,
    layout: str = "4x3",
    size: str = "1280x720",
    fmt: Literal["png", "jpg"] = "png",
) -> dict[str, Any]:
    """Extract one frame or a contact sheet from a YouTube video.

    USE WHEN mode='single': you need a specific moment as an image (timestamp_s
                            required). Useful for captions or feeding a vision model.
    USE WHEN mode='sheet':  you need an at-a-glance view of a video. Returns the
                            tiled sheet and individual frames.
    DO NOT USE WHEN: you only need text — call transcript.get instead.
    OUTPUT SHAPE: local path fields plus portable MCP resource URI metadata.
    """
    try:
        video_id = parse_video_id(url_or_id)
    except ValueError as exc:
        return envelope.fail("bad_url", str(exc), recoverable=False)

    if mode == "single":
        if timestamp_s is None:
            return envelope.fail(
                "missing_timestamp",
                "timestamp_s is required when mode='single'",
                recoverable=False,
            )
        try:
            result = frame_extract.extract_single_frame(
                video_id, float(timestamp_s), fmt=fmt, size=size,
            )
        except FrameExtractError as exc:
            return envelope.fail("frame_extract_failed", str(exc))

        ref, warnings = _reference(result.path, video_id)
        return envelope.ok(
            {
                "path": result.path,
                "timestamp_s": result.timestamp_s,
                "cached": result.cached,
                **ref,
            },
            source="cache" if result.cached else "scrape",
            warnings=warnings,
        )

    if mode == "sheet":
        try:
            result = frame_extract.extract_contact_sheet(
                video_id, n=n, layout=layout, fmt=fmt, size=size,
            )
        except FrameExtractError as exc:
            return envelope.fail("frame_extract_failed", str(exc))

        sheet_ref, warnings = _reference(result.path, video_id)
        frame_refs: list[dict[str, object]] = []
        for path in result.frame_paths:
            ref, ref_warnings = _reference(path, video_id)
            frame_refs.append(ref)
            warnings.extend(ref_warnings)

        return envelope.ok(
            {
                "path": result.path,
                "layout": result.layout,
                "frame_timestamps": result.frame_timestamps,
                "frame_paths": result.frame_paths,
                "cached": result.cached,
                **sheet_ref,
                "frame_resources": frame_refs,
            },
            source="cache" if result.cached else "scrape",
            warnings=list(dict.fromkeys(warnings)),
        )

    return envelope.fail(
        "bad_mode",
        f"mode must be 'single' or 'sheet', got {mode!r}",
        recoverable=False,
    )
