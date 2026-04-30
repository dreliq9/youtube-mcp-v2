"""frame.get — single-frame or contact-sheet extraction via yt-dlp + ffmpeg.

Two modes merged into one tool for AI-parseability (per SPEC §3 mode-merge):
- mode='single' takes timestamp_s and returns one frame.
- mode='sheet'  takes n + layout and returns a tiled contact sheet plus the
                individual frame paths (useful for LitRPG-style review).

Cached at ~/.cache/youtube-mcp/frames/<videoId>/. Repeat calls with identical
parameters return the existing file in milliseconds.
"""

from __future__ import annotations

from typing import Any, Literal

from .. import envelope
from ..adapters import frame_extract
from ..adapters.frame_extract import FrameExtractError
from ..adapters.url import parse_video_id


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
                            required). Useful for in-text references, captions,
                            or feeding to a vision model.
    USE WHEN mode='sheet':  you need an at-a-glance view of a video (LitRPG
                            review pipeline, scene-skimming, content audit).
                            Returns the tiled sheet AND the individual frames.
    DO NOT USE WHEN: you only need text — call transcript.get instead.
                     For full video download, this is not the right tool.
    OUTPUT SHAPE: envelope wrapping
        single → {path, timestamp_s, cached}
        sheet  → {path, layout, frame_timestamps, frame_paths, cached}
    """
    try:
        video_id = parse_video_id(url_or_id)
    except ValueError as e:
        return envelope.fail("bad_url", str(e), recoverable=False)

    if mode == "single":
        if timestamp_s is None:
            return envelope.fail(
                "missing_timestamp",
                "timestamp_s is required when mode='single'",
                recoverable=False,
            )
        try:
            res = frame_extract.extract_single_frame(
                video_id, float(timestamp_s), fmt=fmt, size=size,
            )
        except FrameExtractError as e:
            return envelope.fail("frame_extract_failed", str(e))
        return envelope.ok(
            {
                "path": res.path,
                "timestamp_s": res.timestamp_s,
                "cached": res.cached,
            },
            source="cache" if res.cached else "scrape",
        )

    if mode == "sheet":
        try:
            res = frame_extract.extract_contact_sheet(
                video_id, n=n, layout=layout, fmt=fmt, size=size,
            )
        except FrameExtractError as e:
            return envelope.fail("frame_extract_failed", str(e))
        return envelope.ok(
            {
                "path": res.path,
                "layout": res.layout,
                "frame_timestamps": res.frame_timestamps,
                "frame_paths": res.frame_paths,
                "cached": res.cached,
            },
            source="cache" if res.cached else "scrape",
        )

    return envelope.fail(
        "bad_mode",
        f"mode must be 'single' or 'sheet', got {mode!r}",
        recoverable=False,
    )
