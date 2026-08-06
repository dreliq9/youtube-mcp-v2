"""Frame extraction via yt-dlp + ffmpeg.

Reliability over speed: download the video locally, then extract frames with
ffmpeg. Persisted frame outputs use the shared XDG-aware youtube-mcp cache root.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..paths import CACHE_DIR as _DEFAULT_CACHE_DIR
from ..paths import FRAMES_DIR as _DEFAULT_FRAMES_DIR

CACHE_DIR = _DEFAULT_CACHE_DIR
FRAMES_DIR = _DEFAULT_FRAMES_DIR

YT_DLP = shutil.which("yt-dlp") or "yt-dlp"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

DOWNLOAD_TIMEOUT_S = 180
FFMPEG_TIMEOUT_S = 60
PROBE_TIMEOUT_S = 15


class FrameExtractError(RuntimeError):
    """Raised when frame extraction fails. Caller wraps in error envelope."""


def _video_cache_dir(video_id: str) -> Path:
    directory = FRAMES_DIR / video_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _run(cmd: list[str], timeout_s: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FrameExtractError(
            f"timeout after {timeout_s}s: {' '.join(cmd[:3])}…"
        ) from exc
    except FileNotFoundError as exc:
        raise FrameExtractError(f"binary not found: {cmd[0]}") from exc


def _download_video(video_id: str, dest_dir: Path) -> Path:
    out_template = str(dest_dir / "%(id)s.%(ext)s")
    cmd = [
        YT_DLP,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "-f",
        "bv*[height<=720]+ba/b[height<=720]/best",
        "--merge-output-format",
        "mp4",
        "-o",
        out_template,
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    cp = _run(cmd, DOWNLOAD_TIMEOUT_S)
    if cp.returncode != 0:
        raise FrameExtractError(
            f"yt-dlp failed (rc={cp.returncode}): {cp.stderr.strip()[:400]}"
        )

    candidates = list(dest_dir.glob(f"{video_id}.*"))
    if not candidates:
        raise FrameExtractError("yt-dlp completed but no output file found")
    return candidates[0]


def _probe_duration(video_path: Path) -> float:
    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    cp = _run(cmd, PROBE_TIMEOUT_S)
    if cp.returncode != 0:
        raise FrameExtractError(f"ffprobe failed: {cp.stderr.strip()[:200]}")
    try:
        data = json.loads(cp.stdout)
        return float(data["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        raise FrameExtractError(f"ffprobe parse failed: {exc}") from exc


def _extract_single(video_path: Path, ts: float, out_path: Path, size: str) -> None:
    cmd = [
        FFMPEG,
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{ts:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={size.replace('x', ':')}",
        "-q:v",
        "2",
        str(out_path),
    ]
    cp = _run(cmd, FFMPEG_TIMEOUT_S)
    if cp.returncode != 0:
        raise FrameExtractError(f"ffmpeg failed: {cp.stderr.strip()[:200]}")


def _parse_layout(layout: str) -> tuple[int, int]:
    if "x" not in layout:
        raise FrameExtractError(f"layout must be like '4x3', got {layout!r}")
    try:
        cols, rows = (int(part) for part in layout.split("x", 1))
    except ValueError as exc:
        raise FrameExtractError(f"invalid layout {layout!r}") from exc
    if cols < 1 or rows < 1:
        raise FrameExtractError(f"layout dims must be >=1, got {layout!r}")
    return cols, rows


@dataclass
class SingleFrameResult:
    path: str
    timestamp_s: float
    cached: bool


@dataclass
class ContactSheetResult:
    path: str
    layout: str
    frame_timestamps: list[float]
    frame_paths: list[str]
    cached: bool


def extract_single_frame(
    video_id: str,
    timestamp_s: float,
    fmt: str = "png",
    size: str = "1280x720",
) -> SingleFrameResult:
    if timestamp_s < 0:
        raise FrameExtractError(f"timestamp_s must be >=0, got {timestamp_s}")

    cache = _video_cache_dir(video_id)
    out_path = cache / f"single_{int(timestamp_s * 1000)}ms_{size}.{fmt}"
    if out_path.exists() and out_path.stat().st_size > 0:
        return SingleFrameResult(
            path=str(out_path), timestamp_s=timestamp_s, cached=True
        )

    with tempfile.TemporaryDirectory(prefix="ytmcp-dl-") as tmp:
        tmp_dir = Path(tmp)
        video_path = _download_video(video_id, tmp_dir)
        duration = _probe_duration(video_path)
        if timestamp_s >= duration:
            raise FrameExtractError(
                f"timestamp {timestamp_s}s exceeds duration {duration:.1f}s"
            )
        _extract_single(video_path, timestamp_s, out_path, size)

    return SingleFrameResult(
        path=str(out_path), timestamp_s=timestamp_s, cached=False
    )


def extract_contact_sheet(
    video_id: str,
    n: int = 12,
    layout: str = "4x3",
    fmt: str = "png",
    size: str = "1280x720",
) -> ContactSheetResult:
    cols, rows = _parse_layout(layout)
    cells = cols * rows
    if n > cells:
        n = cells
    if n < 1:
        raise FrameExtractError("n must be >=1")

    cache = _video_cache_dir(video_id)
    sheet_path = cache / f"sheet_{n}_{layout}_{size}.{fmt}"
    manifest_path = sheet_path.with_suffix(".json")
    if (
        sheet_path.exists()
        and sheet_path.stat().st_size > 0
        and manifest_path.exists()
    ):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return ContactSheetResult(
                path=str(sheet_path),
                layout=layout,
                frame_timestamps=manifest["frame_timestamps"],
                frame_paths=manifest["frame_paths"],
                cached=True,
            )
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    with tempfile.TemporaryDirectory(prefix="ytmcp-dl-") as tmp:
        tmp_dir = Path(tmp)
        video_path = _download_video(video_id, tmp_dir)
        duration = _probe_duration(video_path)

        margin = max(0.5, duration * 0.02)
        usable = max(duration - 2 * margin, 0.5)
        if n == 1:
            timestamps = [margin + usable / 2]
        else:
            timestamps = [margin + (usable * i / (n - 1)) for i in range(n)]

        frame_paths: list[str] = []
        for index, timestamp in enumerate(timestamps):
            frame_path = cache / f"sheet_{n}_{layout}_{size}_{index:02d}.{fmt}"
            _extract_single(video_path, timestamp, frame_path, size)
            frame_paths.append(str(frame_path))

        seq_dir = tmp_dir / "seq"
        seq_dir.mkdir()
        for index, path in enumerate(frame_paths):
            shutil.copy(path, seq_dir / f"f{index:04d}.{fmt}")

        cmd = [
            FFMPEG,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(seq_dir / f"f%04d.{fmt}"),
            "-vf",
            f"tile={cols}x{rows}",
            "-frames:v",
            "1",
            str(sheet_path),
        ]
        cp = _run(cmd, FFMPEG_TIMEOUT_S)
        if cp.returncode != 0:
            raise FrameExtractError(
                f"ffmpeg tile failed: {cp.stderr.strip()[:200]}"
            )

    manifest_path.write_text(
        json.dumps(
            {
                "frame_timestamps": timestamps,
                "frame_paths": frame_paths,
            }
        ),
        encoding="utf-8",
    )

    return ContactSheetResult(
        path=str(sheet_path),
        layout=layout,
        frame_timestamps=timestamps,
        frame_paths=frame_paths,
        cached=False,
    )
