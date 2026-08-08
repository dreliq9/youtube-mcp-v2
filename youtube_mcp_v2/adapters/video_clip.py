"""Materialize editor-ready MP4 clips from immutable clip-plan source ranges.

This is an explicit heavy/network boundary.  Retrieval/planning never downloads
video; callers opt into media acquisition by invoking media.materialize.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..paths import EDIT_PLAN_DIR as _DEFAULT_EDIT_PLAN_DIR
from ..paths import EDITOR_CLIP_DIR as _DEFAULT_EDITOR_CLIP_DIR

EDIT_PLAN_DIR = _DEFAULT_EDIT_PLAN_DIR
EDITOR_CLIP_DIR = _DEFAULT_EDITOR_CLIP_DIR
YT_DLP = shutil.which("yt-dlp") or "yt-dlp"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
DOWNLOAD_TIMEOUT_S = 300
FFMPEG_TIMEOUT_S = 240
MAX_CLIPS_PER_CALL = 12
MAX_UNIQUE_VIDEOS_PER_CALL = 8
MAX_CLIP_DURATION_S = 180.0
MAX_TOTAL_DURATION_S = 900.0
ALLOWED_HEIGHTS = {360, 480, 720, 1080}
MATERIALIZED_SCHEMA = "youtube-mcp.materialized-clip-plan/v1"


class VideoClipError(RuntimeError):
    pass


@dataclass(frozen=True)
class MaterializedClip:
    clip_id: str
    video_id: str
    title: str | None
    channel: str | None
    source_url: str
    source_start_s: float
    source_end_s: float
    duration_s: float
    path: str
    sha256: str
    size_bytes: int
    mime_type: str
    cached: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "video_id": self.video_id,
            "title": self.title,
            "channel": self.channel,
            "source_url": self.source_url,
            "source_start_s": self.source_start_s,
            "source_end_s": self.source_end_s,
            "duration_s": self.duration_s,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
            "cached": self.cached,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(cmd: list[str], *, timeout_s: int, label: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoClipError(f"{label} timed out after {timeout_s}s") from exc
    except FileNotFoundError as exc:
        raise VideoClipError(f"required binary not found: {cmd[0]}") from exc


def _yt_dlp_auth_args() -> list[str]:
    args: list[str] = []
    proxy = os.environ.get("YOUTUBE_YTDLP_PROXY")
    cookies_file = os.environ.get("YOUTUBE_COOKIES_FILE")
    cookies_browser = os.environ.get("YOUTUBE_COOKIES_FROM_BROWSER")
    if proxy:
        args += ["--proxy", proxy]
    if cookies_file:
        args += ["--cookies", cookies_file]
    if cookies_browser:
        args += ["--cookies-from-browser", cookies_browser]
    return args


def _download_video(video_id: str, dest_dir: Path, max_height: int) -> Path:
    out_template = str(dest_dir / "%(id)s.%(ext)s")
    selector = (
        f"bv*[height<={max_height}]+ba/"
        f"b[height<={max_height}]/best"
    )
    cmd = [
        YT_DLP,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "-f",
        selector,
        "--merge-output-format",
        "mp4",
        "-o",
        out_template,
        *_yt_dlp_auth_args(),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    cp = _run(cmd, timeout_s=DOWNLOAD_TIMEOUT_S, label="yt-dlp source acquisition")
    if cp.returncode != 0:
        # Do not forward stderr: cookies, proxy details, or local paths can appear.
        raise VideoClipError(f"yt-dlp source acquisition failed (rc={cp.returncode})")
    candidates = [p for p in dest_dir.glob(f"{video_id}.*") if p.is_file()]
    if not candidates:
        raise VideoClipError("yt-dlp completed but no source video was produced")
    return max(candidates, key=lambda path: path.stat().st_size)


def _clip_cache_path(video_id: str, start_s: float, end_s: float, max_height: int) -> Path:
    directory = EDITOR_CLIP_DIR / video_id
    directory.mkdir(parents=True, exist_ok=True)
    start_ms = int(round(start_s * 1000))
    end_ms = int(round(end_s * 1000))
    return directory / f"clip_{start_ms}ms_{end_ms}ms_{max_height}p.mp4"


def _extract_clip(source: Path, output: Path, start_s: float, end_s: float) -> None:
    duration = end_s - start_s
    cmd = [
        FFMPEG,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-ss",
        f"{start_s:.3f}",
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    cp = _run(cmd, timeout_s=FFMPEG_TIMEOUT_S, label="ffmpeg clip extraction")
    if cp.returncode != 0 or not output.exists() or output.stat().st_size <= 0:
        raise VideoClipError(f"ffmpeg clip extraction failed (rc={cp.returncode})")


def _validate_selection(clips: list[dict[str, Any]], max_height: int) -> None:
    if max_height not in ALLOWED_HEIGHTS:
        raise VideoClipError(
            f"max_height must be one of {sorted(ALLOWED_HEIGHTS)}, got {max_height}"
        )
    if not clips:
        raise VideoClipError("clip selection is empty")
    if len(clips) > MAX_CLIPS_PER_CALL:
        raise VideoClipError(
            f"at most {MAX_CLIPS_PER_CALL} clips may be materialized per call"
        )
    video_ids = {str(clip.get("video_id") or "") for clip in clips}
    video_ids.discard("")
    if len(video_ids) > MAX_UNIQUE_VIDEOS_PER_CALL:
        raise VideoClipError(
            f"at most {MAX_UNIQUE_VIDEOS_PER_CALL} unique videos may be materialized per call"
        )

    total = 0.0
    for clip in clips:
        video_id = str(clip.get("video_id") or "")
        clip_id = str(clip.get("clip_id") or "")
        try:
            start_s = float(clip.get("start_s"))
            end_s = float(clip.get("end_s"))
        except (TypeError, ValueError) as exc:
            raise VideoClipError(f"clip {clip_id or '<unknown>'} has invalid timestamps") from exc
        if not video_id or len(video_id) != 11:
            raise VideoClipError(f"clip {clip_id or '<unknown>'} has invalid video_id")
        if start_s < 0 or end_s <= start_s:
            raise VideoClipError(f"clip {clip_id or '<unknown>'} has invalid source range")
        duration = end_s - start_s
        if duration > MAX_CLIP_DURATION_S:
            raise VideoClipError(
                f"clip {clip_id or '<unknown>'} exceeds {MAX_CLIP_DURATION_S:g}s maximum"
            )
        total += duration
    if total > MAX_TOTAL_DURATION_S:
        raise VideoClipError(
            f"selected clips exceed {MAX_TOTAL_DURATION_S:g}s total materialization budget"
        )


def materialize(
    plan: dict[str, Any],
    *,
    max_height: int = 720,
    clip_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Materialize selected plan ranges, downloading each source at most once."""
    all_clips = [clip for clip in (plan.get("clips") or []) if isinstance(clip, dict)]
    if clip_ids is None:
        clips = all_clips
    else:
        requested = list(dict.fromkeys(str(value) for value in clip_ids))
        by_id = {str(clip.get("clip_id")): clip for clip in all_clips}
        unknown = [clip_id for clip_id in requested if clip_id not in by_id]
        if unknown:
            raise VideoClipError(f"unknown clip_ids: {unknown}")
        clips = [by_id[clip_id] for clip_id in requested]

    _validate_selection(clips, max_height)
    outputs: dict[str, MaterializedClip] = {}
    missing_by_video: dict[str, list[dict[str, Any]]] = {}

    for clip in clips:
        video_id = str(clip["video_id"])
        start_s = float(clip["start_s"])
        end_s = float(clip["end_s"])
        path = _clip_cache_path(video_id, start_s, end_s, max_height)
        if path.exists() and path.stat().st_size > 0:
            outputs[str(clip["clip_id"])] = MaterializedClip(
                clip_id=str(clip["clip_id"]),
                video_id=video_id,
                title=clip.get("title"),
                channel=clip.get("channel"),
                source_url=str(clip.get("source_url") or f"https://www.youtube.com/watch?v={video_id}"),
                source_start_s=start_s,
                source_end_s=end_s,
                duration_s=round(end_s - start_s, 3),
                path=str(path),
                sha256=_sha256(path),
                size_bytes=path.stat().st_size,
                mime_type="video/mp4",
                cached=True,
            )
        else:
            missing_by_video.setdefault(video_id, []).append(clip)

    for video_id, video_clips in missing_by_video.items():
        with tempfile.TemporaryDirectory(prefix="ytmcp-editor-source-") as temp:
            source = _download_video(video_id, Path(temp), max_height)
            for clip in video_clips:
                start_s = float(clip["start_s"])
                end_s = float(clip["end_s"])
                path = _clip_cache_path(video_id, start_s, end_s, max_height)
                _extract_clip(source, path, start_s, end_s)
                outputs[str(clip["clip_id"])] = MaterializedClip(
                    clip_id=str(clip["clip_id"]),
                    video_id=video_id,
                    title=clip.get("title"),
                    channel=clip.get("channel"),
                    source_url=str(clip.get("source_url") or f"https://www.youtube.com/watch?v={video_id}"),
                    source_start_s=start_s,
                    source_end_s=end_s,
                    duration_s=round(end_s - start_s, 3),
                    path=str(path),
                    sha256=_sha256(path),
                    size_bytes=path.stat().st_size,
                    mime_type="video/mp4",
                    cached=False,
                )

    assets = [outputs[str(clip["clip_id"])].as_dict() for clip in clips]
    identity = {
        "schema": MATERIALIZED_SCHEMA,
        "plan_revision": plan.get("plan_revision"),
        "max_height": max_height,
        "assets": [
            {
                "clip_id": asset["clip_id"],
                "sha256": asset["sha256"],
                "source_start_s": asset["source_start_s"],
                "source_end_s": asset["source_end_s"],
            }
            for asset in assets
        ],
    }
    materialization_revision = "cm-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    manifest = {
        **identity,
        "materialization_revision": materialization_revision,
        "materialized_at": _now_iso(),
        "complete": len(assets) == len(all_clips),
        "plan_clip_count": len(all_clips),
        "materialized_clip_count": len(assets),
        "editor_contract": {
            "timeline_order": "assets are emitted in clip-plan order",
            "asset_semantics": "each path is already trimmed to its source range; editor trim-in is 0 and trim-out is duration_s",
            "provenance_fields": ["video_id", "source_url", "source_start_s", "source_end_s", "sha256"],
        },
        "assets": assets,
    }
    manifest_dir = EDIT_PLAN_DIR / "materialized"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{materialization_revision}.json"
    if not manifest_path.exists():
        with manifest_path.open("x", encoding="utf-8") as file:
            json.dump(manifest, file, indent=2, ensure_ascii=False)
    manifest["manifest_path"] = str(manifest_path)
    manifest["manifest_portable"] = False
    return manifest
