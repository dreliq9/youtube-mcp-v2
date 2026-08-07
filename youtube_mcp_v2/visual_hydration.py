"""Bounded, resumable acquisition of timestamped frame evidence for a corpus.

A single video can require a full yt-dlp download plus multiple ffmpeg operations,
so visual hydration is intentionally more conservative than transcript hydration.
The durable output is the managed frame cache; the tool result is compact progress
metadata, not image bytes or local frame paths.
"""

from __future__ import annotations

import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

from . import skeleton, visual_index
from .adapters import frame_extract

DEFAULT_BATCH_SIZE = 2
MAX_BATCH_SIZE = 6
DEFAULT_MAX_WORKERS = 1
MAX_WORKERS = 2
DEFAULT_FRAME_COUNT = 12
MAX_FRAME_COUNT = 24
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
VisualHydrationPolicy = Literal["missing"]


class VisualHydrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    position: int
    video_id: str
    title: str | None
    cached_frame_count: int


def _parse_cursor(cursor: str | None, total: int) -> int:
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except (TypeError, ValueError) as exc:
        raise VisualHydrationError("cursor must be a non-negative integer string") from exc
    if value < 0:
        raise VisualHydrationError("cursor must be >= 0")
    if value > total:
        raise VisualHydrationError(f"cursor {value} exceeds corpus size {total}")
    return value


def _parse_layout(layout: str) -> tuple[int, int]:
    if "x" not in layout:
        raise VisualHydrationError("layout must be formatted like '4x3'")
    try:
        cols, rows = (int(part) for part in layout.split("x", 1))
    except ValueError as exc:
        raise VisualHydrationError("layout must contain integer dimensions") from exc
    if cols < 1 or rows < 1 or cols > 8 or rows > 8:
        raise VisualHydrationError("layout dimensions must each be between 1 and 8")
    return cols, rows


def _validate_request(
    *,
    batch_size: int,
    max_workers: int,
    n: int,
    layout: str,
    size: str,
    fmt: str,
    policy: str,
) -> None:
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise VisualHydrationError(
            f"batch_size must be between 1 and {MAX_BATCH_SIZE}"
        )
    if max_workers < 1 or max_workers > MAX_WORKERS:
        raise VisualHydrationError(
            f"max_workers must be between 1 and {MAX_WORKERS}"
        )
    if n < 1 or n > MAX_FRAME_COUNT:
        raise VisualHydrationError(f"n must be between 1 and {MAX_FRAME_COUNT}")
    cols, rows = _parse_layout(layout)
    if n > cols * rows:
        raise VisualHydrationError(
            f"n={n} exceeds layout capacity {cols * rows}; enlarge layout or reduce n"
        )
    if not re.fullmatch(r"\d+x\d+", size):
        raise VisualHydrationError("size must be formatted like '1280x720'")
    width, height = (int(part) for part in size.split("x", 1))
    if width < 64 or height < 64 or width > 4096 or height > 4096:
        raise VisualHydrationError("size dimensions must be between 64 and 4096")
    if fmt not in {"png", "jpg"}:
        raise VisualHydrationError("fmt must be 'png' or 'jpg'")
    if policy != "missing":
        raise VisualHydrationError("policy must currently be 'missing'")


def _load_membership(handle: str) -> list[dict[str, Any]]:
    payload = skeleton.load_skeleton(handle)
    videos = payload.get("videos")
    if not isinstance(videos, list):
        raise VisualHydrationError("corpus revision has no valid videos list")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for position, video in enumerate(videos):
        if not isinstance(video, dict):
            raise VisualHydrationError(
                f"corpus video at position {position} is not an object"
            )
        video_id = str(video.get("id") or "")
        if not _VIDEO_ID_RE.fullmatch(video_id):
            raise VisualHydrationError(
                f"invalid video id at corpus position {position}: {video_id!r}"
            )
        if video_id in seen:
            raise VisualHydrationError(f"duplicate video id in corpus: {video_id}")
        seen.add(video_id)
        normalized.append(video)
    return normalized


def cached_timestamped_frame_count(video_id: str) -> int:
    """Count distinct trustworthy timestamp/path pairs already in managed cache."""
    video_dir = visual_index.FRAMES_DIR / video_id
    if not video_dir.is_dir():
        return 0
    candidates = [
        *visual_index._manifest_frames(video_dir),
        *visual_index._single_frames(video_dir),
    ]
    return len({(float(timestamp), str(path.resolve())) for timestamp, path, _kind in candidates})


def _scan_batch(
    videos: list[dict[str, Any]],
    *,
    start: int,
    batch_size: int,
    required_frames: int,
) -> tuple[list[Candidate], int, int]:
    candidates: list[Candidate] = []
    skipped_cached = 0
    scan_end = start
    for position in range(start, len(videos)):
        video = videos[position]
        video_id = str(video["id"])
        cached_count = cached_timestamped_frame_count(video_id)
        scan_end = position + 1
        if cached_count >= required_frames:
            skipped_cached += 1
            continue
        candidates.append(
            Candidate(
                position=position,
                video_id=video_id,
                title=(str(video["title"]) if video.get("title") else None),
                cached_frame_count=cached_count,
            )
        )
        if len(candidates) >= batch_size:
            break
    return candidates, scan_end, skipped_cached


def _extract(
    candidate: Candidate,
    *,
    n: int,
    layout: str,
    size: str,
    fmt: str,
) -> frame_extract.ContactSheetResult:
    return frame_extract.extract_contact_sheet(
        candidate.video_id,
        n=n,
        layout=layout,
        size=size,
        fmt=fmt,
    )


def _success(candidate: Candidate, result: frame_extract.ContactSheetResult) -> dict[str, Any]:
    timestamps = [float(value) for value in result.frame_timestamps]
    return {
        "position": candidate.position,
        "video_id": candidate.video_id,
        "title": candidate.title,
        "status": "ready",
        "cached_before": candidate.cached_frame_count,
        "cached_extraction": bool(result.cached),
        "frame_count": len(timestamps),
        "first_timestamp_s": min(timestamps) if timestamps else None,
        "last_timestamp_s": max(timestamps) if timestamps else None,
        "error": None,
    }


def _failure(candidate: Candidate, exc: BaseException) -> dict[str, Any]:
    # Do not forward raw yt-dlp/ffmpeg diagnostics here. Future integrated auth
    # routes may contain local cookie/proxy configuration in subprocess messages.
    code = "frame_extract_failed"
    if isinstance(exc, frame_extract.FrameExtractError):
        lowered = str(exc).lower()
        if "timeout" in lowered:
            code = "frame_extract_timeout"
        elif "binary not found" in lowered:
            code = "media_dependency_missing"
    return {
        "position": candidate.position,
        "video_id": candidate.video_id,
        "title": candidate.title,
        "status": "failed",
        "cached_before": candidate.cached_frame_count,
        "cached_extraction": None,
        "frame_count": 0,
        "first_timestamp_s": None,
        "last_timestamp_s": None,
        "error": {
            "code": code,
            "message": f"visual hydration failed with {type(exc).__name__}",
            "recoverable": True,
        },
    }


def hydrate_visuals(
    handle: str,
    *,
    cursor: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_workers: int = DEFAULT_MAX_WORKERS,
    n: int = DEFAULT_FRAME_COUNT,
    layout: str = "4x3",
    size: str = "1280x720",
    fmt: Literal["png", "jpg"] = "png",
    policy: VisualHydrationPolicy = "missing",
) -> dict[str, Any]:
    """Acquire at most one bounded batch of timestamped contact-sheet frames."""
    _validate_request(
        batch_size=batch_size,
        max_workers=max_workers,
        n=n,
        layout=layout,
        size=size,
        fmt=fmt,
        policy=policy,
    )
    videos = _load_membership(handle)
    total = len(videos)
    start = _parse_cursor(cursor, total)
    candidates, scan_end, skipped_cached = _scan_batch(
        videos,
        start=start,
        batch_size=batch_size,
        required_frames=n,
    )

    results: list[dict[str, Any]] = []
    if candidates:
        workers = min(max_workers, len(candidates))
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="ytmcp-visual-hydrate",
        ) as pool:
            futures: list[
                tuple[Candidate, Future[frame_extract.ContactSheetResult]]
            ] = [
                (
                    candidate,
                    pool.submit(
                        _extract,
                        candidate,
                        n=n,
                        layout=layout,
                        size=size,
                        fmt=fmt,
                    ),
                )
                for candidate in candidates
            ]
            # Resolve in frozen corpus order for deterministic output.
            for candidate, future in futures:
                try:
                    result = future.result()
                except BaseException as exc:
                    results.append(_failure(candidate, exc))
                else:
                    results.append(_success(candidate, result))

    succeeded = sum(result["status"] == "ready" for result in results)
    failed = len(results) - succeeded
    complete = scan_end >= total
    return {
        "corpus_revision": handle,
        "policy": policy,
        "cursor": str(start),
        "next_cursor": None if complete else str(scan_end),
        "complete": complete,
        "total_videos": total,
        "scanned": scan_end - start,
        "skipped_cached": skipped_cached,
        "attempted": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "remaining_members": max(0, total - scan_end),
        "requested_frames_per_video": n,
        "layout": layout,
        "size": size,
        "format": fmt,
        "results": results,
    }
