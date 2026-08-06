"""yt-dlp caption fallback adapter.

This adapter is intentionally independent of the primary youtube-transcript-api
path. It uses yt-dlp's YouTube extractor to discover a caption track, writes only
that subtitle track to a temporary directory, parses it, and removes the temporary
files before returning.

Optional routing/auth environment variables:
    YOUTUBE_YTDLP_PROXY=<proxy URL>
    YOUTUBE_COOKIES_FILE=<Netscape cookies.txt path>
    YOUTUBE_COOKIES_FROM_BROWSER=<yt-dlp browser selector>

Credential-bearing values are never included in returned summaries or error text.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .transcript_api import Segment, TranscriptFetch

YT_DLP = shutil.which("yt-dlp") or "yt-dlp"
INFO_TIMEOUT_S = 60
SUBTITLE_TIMEOUT_S = 90


class YtDlpTranscriptError(RuntimeError):
    """Base error for the alternate caption path."""


class YtDlpUnavailable(YtDlpTranscriptError):
    """yt-dlp executable is not installed or cannot be launched."""


class YtDlpNoTranscript(YtDlpTranscriptError):
    """yt-dlp found the video but no usable subtitle track."""


@dataclass(frozen=True)
class YtDlpSettings:
    proxy: str | None
    cookies_file: str | None
    cookies_from_browser: str | None

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "proxy_configured": self.proxy is not None,
            "cookies_mode": (
                "file"
                if self.cookies_file is not None
                else "browser"
                if self.cookies_from_browser is not None
                else "none"
            ),
        }


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def settings_from_env() -> YtDlpSettings:
    cookies_file = _env("YOUTUBE_COOKIES_FILE")
    cookies_from_browser = _env("YOUTUBE_COOKIES_FROM_BROWSER")
    if cookies_file and cookies_from_browser:
        raise ValueError(
            "configure either YOUTUBE_COOKIES_FILE or "
            "YOUTUBE_COOKIES_FROM_BROWSER, not both"
        )
    return YtDlpSettings(
        proxy=_env("YOUTUBE_YTDLP_PROXY"),
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
    )


def _redact(text: str, settings: YtDlpSettings) -> str:
    """Remove configured secret-bearing values from subprocess diagnostics."""
    redacted = text
    for value in (
        settings.proxy,
        settings.cookies_file,
        settings.cookies_from_browser,
    ):
        if value:
            redacted = redacted.replace(value, "<redacted>")
    # yt-dlp/browser tooling can occasionally echo URL userinfo after normalizing
    # it. Redact obvious credential-bearing proxy URL userinfo as a second guard.
    redacted = re.sub(
        r"(?i)(https?|socks[45]h?)://[^\s/@:]+:[^\s/@]+@",
        r"\1://<redacted>@",
        redacted,
    )
    return redacted


def _auth_network_args(settings: YtDlpSettings) -> list[str]:
    args: list[str] = []
    if settings.proxy:
        args += ["--proxy", settings.proxy]
    if settings.cookies_file:
        args += ["--cookies", settings.cookies_file]
    elif settings.cookies_from_browser:
        args += ["--cookies-from-browser", settings.cookies_from_browser]
    return args


def _run(cmd: list[str], *, timeout_s: int, settings: YtDlpSettings) -> subprocess.CompletedProcess[str]:
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise YtDlpUnavailable("yt-dlp executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"yt-dlp exceeded {timeout_s}s") from exc

    if cp.returncode != 0:
        diagnostic = _redact((cp.stderr or cp.stdout or "").strip(), settings)
        if len(diagnostic) > 500:
            diagnostic = diagnostic[:500] + "…"
        raise YtDlpTranscriptError(
            f"yt-dlp failed (rc={cp.returncode})"
            + (f": {diagnostic}" if diagnostic else "")
        )
    return cp


def _dump_info(video_id: str, settings: YtDlpSettings) -> dict[str, Any]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        YT_DLP,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--skip-download",
        "--dump-single-json",
        *_auth_network_args(settings),
        url,
    ]
    cp = _run(cmd, timeout_s=INFO_TIMEOUT_S, settings=settings)
    try:
        payload = json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        raise YtDlpTranscriptError("yt-dlp returned invalid metadata JSON") from exc
    if not isinstance(payload, dict):
        raise YtDlpTranscriptError("yt-dlp metadata payload was not an object")
    return payload


def _language_matches(candidate: str, requested: str) -> bool:
    candidate_l = candidate.lower()
    requested_l = requested.lower()
    return candidate_l == requested_l or candidate_l.startswith(requested_l + "-")


def _pick_language(pool: dict[str, Any], requested: str) -> str | None:
    languages = [key for key, value in pool.items() if key != "live_chat" and value]
    if not languages:
        return None
    for lang in languages:
        if lang.lower() == requested.lower():
            return lang
    for lang in languages:
        if _language_matches(lang, requested):
            return lang
    return None


def _select_track(info: dict[str, Any], requested_lang: str) -> tuple[str, bool]:
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    if not isinstance(manual, dict):
        manual = {}
    if not isinstance(automatic, dict):
        automatic = {}

    # Prefer the requested language and a human track over an auto track.
    for pool, generated in ((manual, False), (automatic, True)):
        chosen = _pick_language(pool, requested_lang)
        if chosen:
            return chosen, generated

    # Then English, still preferring manual over generated.
    if requested_lang.lower() != "en":
        for pool, generated in ((manual, False), (automatic, True)):
            chosen = _pick_language(pool, "en")
            if chosen:
                return chosen, generated

    # Finally any human track, then any generated track. Deterministic lexical
    # ordering avoids depending on upstream dict order.
    for pool, generated in ((manual, False), (automatic, True)):
        languages = sorted(
            key for key, value in pool.items() if key != "live_chat" and value
        )
        if languages:
            return languages[0], generated

    raise YtDlpNoTranscript("yt-dlp found no subtitle or automatic-caption tracks")


def _download_track(
    video_id: str,
    lang: str,
    *,
    generated: bool,
    settings: YtDlpSettings,
    dest_dir: Path,
) -> Path:
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        YT_DLP,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--skip-download",
        "--write-auto-subs" if generated else "--write-subs",
        "--sub-langs",
        lang,
        "--sub-format",
        "json3/vtt/best",
        "-o",
        str(dest_dir / "%(id)s.%(ext)s"),
        *_auth_network_args(settings),
        url,
    ]
    _run(cmd, timeout_s=SUBTITLE_TIMEOUT_S, settings=settings)

    files = [
        path
        for path in dest_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".json3", ".vtt"}
    ]
    if not files:
        raise YtDlpNoTranscript("yt-dlp completed but did not write a subtitle file")

    # There should be exactly one selected language. Prefer a filename containing
    # the exact language tag, then JSON3 over VTT.
    def score(path: Path) -> tuple[int, int, str]:
        exact_lang = 0 if f".{lang}." in path.name else 1
        format_rank = 0 if path.suffix.lower() == ".json3" else 1
        return exact_lang, format_rank, path.name

    return sorted(files, key=score)[0]


def _parse_json3(path: Path) -> list[Segment]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise YtDlpTranscriptError("could not parse yt-dlp JSON3 subtitles") from exc

    events = payload.get("events", []) if isinstance(payload, dict) else []
    segments: list[Segment] = []
    for event in events:
        if not isinstance(event, dict) or not event.get("segs"):
            continue
        text = "".join(
            str(seg.get("utf8", ""))
            for seg in event.get("segs", [])
            if isinstance(seg, dict)
        ).replace("\n", " ").strip()
        if not text:
            continue
        start_ms = event.get("tStartMs", 0)
        duration_ms = event.get("dDurationMs", 0)
        try:
            start_s = float(start_ms) / 1000.0
            duration_s = max(0.0, float(duration_ms) / 1000.0)
        except (TypeError, ValueError):
            continue
        segments.append(Segment(start_s=start_s, duration_s=duration_s, text=text))
    return segments


_VTT_TS_RE = re.compile(
    r"^(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}\.\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


def _vtt_time(value: str) -> float:
    parts = value.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = "0"
        minutes, seconds = parts
    else:
        raise ValueError(value)
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _parse_vtt(path: Path) -> list[Segment]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise YtDlpTranscriptError("could not read yt-dlp VTT subtitles") from exc

    segments: list[Segment] = []
    i = 0
    while i < len(lines):
        match = _VTT_TS_RE.match(lines[i].strip())
        if match is None:
            i += 1
            continue
        start = _vtt_time(match.group("start"))
        end = _vtt_time(match.group("end"))
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            clean = html.unescape(_TAG_RE.sub("", lines[i])).strip()
            if clean:
                text_lines.append(clean)
            i += 1
        text = " ".join(text_lines).strip()
        if text:
            segments.append(
                Segment(start_s=start, duration_s=max(0.0, end - start), text=text)
            )
        i += 1
    return segments


def _normalize_segments(segments: list[Segment]) -> list[Segment]:
    """Sort and drop exact duplicate neighboring cues produced by some VTT tracks."""
    ordered = sorted(segments, key=lambda s: (s.start_s, s.duration_s, s.text))
    out: list[Segment] = []
    for segment in ordered:
        if out and (
            segment.start_s == out[-1].start_s
            and segment.duration_s == out[-1].duration_s
            and segment.text == out[-1].text
        ):
            continue
        out.append(segment)
    return out


def fetch_transcript(video_id: str, lang: str = "en") -> TranscriptFetch:
    """Fetch a caption track through yt-dlp using a requested→English→any policy."""
    settings = settings_from_env()
    info = _dump_info(video_id, settings)
    selected_lang, generated = _select_track(info, lang)

    with tempfile.TemporaryDirectory(prefix="youtube-mcp-subs-") as tmp:
        path = _download_track(
            video_id,
            selected_lang,
            generated=generated,
            settings=settings,
            dest_dir=Path(tmp),
        )
        if path.suffix.lower() == ".json3":
            segments = _parse_json3(path)
        else:
            segments = _parse_vtt(path)

    segments = _normalize_segments(segments)
    if not segments:
        raise YtDlpNoTranscript("yt-dlp subtitle track contained no text cues")
    return TranscriptFetch(
        segments=segments,
        lang=selected_lang,
        is_generated=generated,
    )
