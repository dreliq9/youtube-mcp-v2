"""Audio extraction via yt-dlp + ffmpeg.

Outputs are cached at ~/.cache/youtube-mcp/audio/<videoId>/ and are intended
for downstream transcription and audio-analysis tools.

The yt-dlp download path reuses the same proxy/cookie environment configuration
as the independent caption fallback so local STT can reach authenticated or
routed videos without creating a second credential surface.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import ytdlp_transcript

CACHE_DIR = Path.home() / ".cache" / "youtube-mcp"
AUDIO_DIR = CACHE_DIR / "audio"

YT_DLP = shutil.which("yt-dlp") or "yt-dlp"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

DOWNLOAD_TIMEOUT_S = 180
FFMPEG_TIMEOUT_S = 180


class AudioExtractError(RuntimeError):
    """Raised when audio extraction fails. Caller wraps in error envelope."""


@dataclass
class AudioResult:
    path: str
    format: str
    sample_rate: int
    mono: bool
    start_s: float | None
    end_s: float | None
    cached: bool


def _audio_cache_dir(video_id: str) -> Path:
    d = AUDIO_DIR / video_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run(cmd: list[str], timeout_s: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise AudioExtractError(
            f"timeout after {timeout_s}s: {' '.join(cmd[:3])}..."
        ) from e
    except FileNotFoundError as e:
        raise AudioExtractError(f"binary not found: {cmd[0]}") from e


def _download_audio(video_id: str, dest_dir: Path) -> Path:
    out_template = str(dest_dir / "%(id)s.%(ext)s")
    try:
        settings = ytdlp_transcript.settings_from_env()
    except ValueError as exc:
        # Configuration error wording contains variable names, never values.
        raise AudioExtractError(str(exc)) from exc

    cmd = [
        YT_DLP,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "-f", "ba/bestaudio/best",
        "-o", out_template,
        *ytdlp_transcript._auth_network_args(settings),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    cp = _run(cmd, DOWNLOAD_TIMEOUT_S)
    if cp.returncode != 0:
        diagnostic = ytdlp_transcript._redact(
            (cp.stderr or cp.stdout or "").strip(), settings
        )
        if len(diagnostic) > 400:
            diagnostic = diagnostic[:400] + "…"
        raise AudioExtractError(
            f"yt-dlp failed (rc={cp.returncode})"
            + (f": {diagnostic}" if diagnostic else "")
        )

    candidates = list(dest_dir.glob(f"{video_id}.*"))
    if not candidates:
        raise AudioExtractError("yt-dlp completed but no output file found")
    return candidates[0]


def _cache_path(
    video_id: str,
    fmt: str,
    sample_rate: int,
    start_s: float | None,
    end_s: float | None,
) -> Path:
    if start_s is None and end_s is None:
        clip = "full"
    else:
        start = 0 if start_s is None else int(start_s * 1000)
        end = "end" if end_s is None else int(end_s * 1000)
        clip = f"{start}ms_{end}ms"
    return _audio_cache_dir(video_id) / f"audio_{clip}_{sample_rate}hz_mono.{fmt}"


def extract_audio(
    video_id: str,
    fmt: str = "wav",
    sample_rate: int = 22050,
    start_s: float | None = None,
    end_s: float | None = None,
) -> AudioResult:
    """Extract audio to a mono file. Defaults to Basic Pitch-friendly WAV."""
    fmt = fmt.lower()
    if fmt not in {"wav", "m4a", "mp3", "flac", "ogg"}:
        raise AudioExtractError(f"unsupported fmt {fmt!r}")
    if sample_rate < 8000:
        raise AudioExtractError("sample_rate must be >= 8000")
    if start_s is not None and start_s < 0:
        raise AudioExtractError("start_s must be >= 0")
    if end_s is not None and end_s <= 0:
        raise AudioExtractError("end_s must be > 0")
    if start_s is not None and end_s is not None and end_s <= start_s:
        raise AudioExtractError("end_s must be greater than start_s")

    out_path = _cache_path(video_id, fmt, sample_rate, start_s, end_s)
    if out_path.exists() and out_path.stat().st_size > 0:
        return AudioResult(
            path=str(out_path),
            format=fmt,
            sample_rate=sample_rate,
            mono=True,
            start_s=start_s,
            end_s=end_s,
            cached=True,
        )

    with tempfile.TemporaryDirectory(prefix="ytmcp-audio-") as tmp:
        source = _download_audio(video_id, Path(tmp))
        cmd = [FFMPEG, "-y", "-loglevel", "error"]
        if start_s is not None:
            cmd += ["-ss", f"{start_s:.3f}"]
        cmd += ["-i", str(source)]
        if end_s is not None:
            duration = end_s - (start_s or 0)
            cmd += ["-t", f"{duration:.3f}"]
        cmd += ["-vn", "-ac", "1", "-ar", str(sample_rate)]
        if fmt == "m4a":
            cmd += ["-c:a", "aac"]
        cmd.append(str(out_path))

        cp = _run(cmd, FFMPEG_TIMEOUT_S)
        if cp.returncode != 0:
            raise AudioExtractError(f"ffmpeg failed: {cp.stderr.strip()[:400]}")

    return AudioResult(
        path=str(out_path),
        format=fmt,
        sample_rate=sample_rate,
        mono=True,
        start_s=start_s,
        end_s=end_s,
        cached=False,
    )
