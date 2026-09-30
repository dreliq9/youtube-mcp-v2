"""Scripted upstream behaviour for deterministic youtube-mcp-v2 cases.

A case's `fixture` object selects behaviour per upstream boundary:

```json
{
  "native":     {"tracks": [{"lang": "en", "generated": false, "transcript": "hist-treaty"}]}
              | {"raise": "IpBlocked" | "TranscriptsDisabled" | "NoTranscriptFound"
                         | "ReadTimeout" | "RuntimeError" | "LeakyRuntimeError"},
  "native_malformed": true,
  "ytdlp":      {"manual": {"en": "sport-final"}, "auto": {}, "format": "vtt" | "json3"}
              | {"raise": "timeout" | "missing_binary"}
              | {"stderr": "text with {ytdlp_proxy} / {cookies_file} placeholders"},
  "watch_page": {"page": "manual-en"} | {"status": 429} | {"html": "..."},
  "data_api":   {"http_status": 403, "reason": "quotaExceeded"},
  "cache":      {"video_meta": {"duration_s": 60}, "corrupt_transcript": {"lang": "en"}}
}
```

Unspecified boundaries are left alone; the runner's socket guard turns any
real network use into a recorded case failure.
"""

from __future__ import annotations

import json
import subprocess as _real_subprocess
import sys
import types
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

from . import synthetic
from ..isolation import CANARY_AUTH_HEADER, CANARY_ENV


@dataclass
class FixtureCalls:
    counts: dict[str, int] = field(default_factory=dict)

    def hit(self, boundary: str) -> None:
        self.counts[boundary] = self.counts.get(boundary, 0) + 1


def _transcript(key: str) -> synthetic.SyntheticTranscript:
    try:
        return synthetic.TRANSCRIPTS[key]
    except KeyError as exc:
        raise ValueError(f"unknown synthetic transcript {key!r}") from exc


# ---------------------------------------------------------------------------
# youtube-transcript-api client replacement
# ---------------------------------------------------------------------------


class _Snippet:
    def __init__(self, start: Any, duration: Any, text: Any) -> None:
        self.start = start
        self.duration = duration
        self.text = text


class _Fetched:
    def __init__(self, snippets: list[_Snippet], language_code: str, is_generated: bool) -> None:
        self.snippets = snippets
        self.language_code = language_code
        self.is_generated = is_generated


def _native_error(name: str, video_id: str) -> Exception:
    from requests.exceptions import ReadTimeout
    from youtube_transcript_api import _errors

    if name == "ReadTimeout":
        return ReadTimeout("read timed out (bench fixture)")
    if name == "RuntimeError":
        return RuntimeError("bench fixture: upstream provider failure")
    if name == "LeakyRuntimeError":
        return RuntimeError(
            "bench fixture: failed via "
            f"{CANARY_ENV['YOUTUBE_TRANSCRIPT_PROXY']} with {CANARY_AUTH_HEADER}"
        )
    if name == "NoTranscriptFound":
        return _errors.NoTranscriptFound(video_id, ["en"], None)
    cls = getattr(_errors, name, None)
    if cls is None:
        raise ValueError(f"unknown native error fixture {name!r}")
    return cls(video_id)


def _make_native_api(spec: dict[str, Any], calls: FixtureCalls, malformed: bool):
    tracks = spec.get("tracks", [])

    def fetched_for(track: dict[str, Any]) -> _Fetched:
        transcript = _transcript(track["transcript"])
        snippets = [
            _Snippet(
                "not-a-number" if malformed and i == 0 else s.start_s,
                s.duration_s,
                s.text,
            )
            for i, s in enumerate(transcript.segments)
        ]
        return _Fetched(snippets, track["lang"], bool(track.get("generated", False)))

    class _Track:
        def __init__(self, track: dict[str, Any]) -> None:
            self._track = track
            self.language_code = track["lang"]
            self.is_generated = bool(track.get("generated", False))

        def fetch(self) -> _Fetched:
            return fetched_for(self._track)

    class FakeYouTubeTranscriptApi:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def _maybe_raise(self, video_id: str) -> None:
            calls.hit("native")
            if "raise" in spec:
                raise _native_error(spec["raise"], video_id)

        def fetch(self, video_id: str, languages: Any = ("en",)) -> _Fetched:
            self._maybe_raise(video_id)
            # Mirror the library: first requested language wins; manual
            # tracks are preferred over generated ones for that language.
            for lang in languages:
                matching = [t for t in tracks if t["lang"] == lang]
                matching.sort(key=lambda t: bool(t.get("generated", False)))
                if matching:
                    return fetched_for(matching[0])
            raise _native_error("NoTranscriptFound", video_id)

        def list(self, video_id: str) -> list[_Track]:
            calls.hit("native_list")
            return [_Track(t) for t in tracks]

    return FakeYouTubeTranscriptApi


# ---------------------------------------------------------------------------
# yt-dlp subprocess replacement
# ---------------------------------------------------------------------------


def _vtt_time(seconds: float) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


def _render_vtt(transcript: synthetic.SyntheticTranscript) -> str:
    lines = ["WEBVTT", ""]
    for seg in transcript.segments:
        lines.append(f"{_vtt_time(seg.start_s)} --> {_vtt_time(seg.start_s + seg.duration_s)}")
        lines.append(seg.text)
        lines.append("")
    return "\n".join(lines)


def _render_json3(transcript: synthetic.SyntheticTranscript) -> str:
    events = [
        {
            "tStartMs": int(round(seg.start_s * 1000)),
            "dDurationMs": int(round(seg.duration_s * 1000)),
            "segs": [{"utf8": seg.text}],
        }
        for seg in transcript.segments
    ]
    return json.dumps({"events": events})


def _make_ytdlp_subprocess(spec: dict[str, Any], calls: FixtureCalls):
    manual = spec.get("manual", {})
    auto = spec.get("auto", {})
    fmt = spec.get("format", "json3")

    def run(cmd: list[str], **kwargs: Any) -> _real_subprocess.CompletedProcess:
        calls.hit("ytdlp")
        if spec.get("raise") == "missing_binary":
            raise FileNotFoundError(cmd[0])
        if spec.get("raise") == "timeout":
            raise _real_subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 1)
        if "stderr" in spec:
            stderr = spec["stderr"].format(
                ytdlp_proxy=CANARY_ENV["YOUTUBE_YTDLP_PROXY"],
                cookies_file=CANARY_ENV["YOUTUBE_COOKIES_FILE"],
                auth_header=CANARY_AUTH_HEADER,
            )
            return _real_subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stderr)

        url = cmd[-1]
        video_id = url.rsplit("v=", 1)[-1]
        if "--dump-single-json" in cmd:
            info = {
                "id": video_id,
                "subtitles": {lang: [{"ext": "json3"}, {"ext": "vtt"}] for lang in manual},
                "automatic_captions": {lang: [{"ext": "json3"}, {"ext": "vtt"}] for lang in auto},
            }
            return _real_subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(info), stderr="")

        lang = cmd[cmd.index("--sub-langs") + 1]
        pool = auto if "--write-auto-subs" in cmd else manual
        key = pool.get(lang)
        if key is None:
            return _real_subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        dest = Path(cmd[cmd.index("-o") + 1]).parent
        transcript = _transcript(key)
        if fmt == "vtt":
            (dest / f"{video_id}.{lang}.vtt").write_text(_render_vtt(transcript), encoding="utf-8")
        else:
            (dest / f"{video_id}.{lang}.json3").write_text(_render_json3(transcript), encoding="utf-8")
        return _real_subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return types.SimpleNamespace(
        run=run,
        TimeoutExpired=_real_subprocess.TimeoutExpired,
        CompletedProcess=_real_subprocess.CompletedProcess,
        PIPE=_real_subprocess.PIPE,
    )


# ---------------------------------------------------------------------------
# Watch page (httpx) replacement
# ---------------------------------------------------------------------------


def _make_page_client(spec: dict[str, Any], calls: FixtureCalls):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        calls.hit("watch_page")
        if "status" in spec and spec["status"] != 200:
            return httpx.Response(spec["status"], text="bench fixture upstream refusal")
        if "html" in spec:
            return httpx.Response(200, text=spec["html"])
        video_id = request.url.params.get("v", "")
        page = synthetic.WATCH_PAGES[spec["page"]]
        return httpx.Response(200, text=synthetic.watch_page_html(video_id, page))

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return lambda: client


# ---------------------------------------------------------------------------
# Data API replacement
# ---------------------------------------------------------------------------


def _data_api_patches(stack: ExitStack, spec: dict[str, Any], calls: FixtureCalls) -> None:
    from youtube_mcp_v2.adapters import data_api

    key = CANARY_ENV["YOUTUBE_API_KEY"]
    status = int(spec.get("http_status", 403))
    reason = spec.get("reason", "quotaExceeded")
    body = json.dumps(
        {"error": {"code": status, "errors": [{"reason": reason}], "message": f"bench {reason}"}}
    ).encode()

    try:
        from googleapiclient.errors import HttpError  # type: ignore[import-not-found]

        class _Resp(dict):
            def __init__(self, code: int) -> None:
                super().__init__(status=str(code))
                self.status = code
                self.reason = reason

        def make_error() -> Exception:
            return HttpError(
                _Resp(status), body, uri=f"https://youtube.googleapis.com/youtube/v3/search?key={key}"
            )

    except ImportError:
        class HttpError(Exception):  # type: ignore[no-redef]
            def __init__(self, resp: Any, content: bytes, uri: str) -> None:
                super().__init__(f"<HttpError {resp.status} when requesting {uri}>")
                self.resp = resp
                self.content = content
                self.uri = uri

        errors_mod = types.ModuleType("googleapiclient.errors")
        errors_mod.HttpError = HttpError  # type: ignore[attr-defined]
        pkg = types.ModuleType("googleapiclient")
        pkg.errors = errors_mod  # type: ignore[attr-defined]
        stack.enter_context(
            mock.patch.dict(sys.modules, {"googleapiclient": pkg, "googleapiclient.errors": errors_mod})
        )

        def make_error() -> Exception:
            return HttpError(
                types.SimpleNamespace(status=status),
                body,
                uri=f"https://youtube.googleapis.com/youtube/v3/search?key={key}",
            )

    class _Request:
        def execute(self) -> Any:
            calls.hit("data_api")
            raise make_error()

    class _Resource:
        def list(self, **_kwargs: Any) -> _Request:
            return _Request()

    class _Client:
        def __getattr__(self, _name: str):
            return lambda: _Resource()

    stack.enter_context(mock.patch.object(data_api, "_get_client", lambda: _Client()))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def apply(stack: ExitStack, fixture: dict[str, Any] | None, *, video_id: str | None) -> FixtureCalls:
    """Install `fixture` patches on `stack` and return a call counter."""
    from youtube_mcp_v2 import cache
    from youtube_mcp_v2.adapters import page, transcript_api, ytdlp_transcript

    calls = FixtureCalls()
    fixture = fixture or {}

    if "native" in fixture:
        api = _make_native_api(fixture["native"], calls, bool(fixture.get("native_malformed")))
        stack.enter_context(mock.patch.object(transcript_api, "YouTubeTranscriptApi", api))
    if "ytdlp" in fixture:
        fake = _make_ytdlp_subprocess(fixture["ytdlp"], calls)
        stack.enter_context(mock.patch.object(ytdlp_transcript, "subprocess", fake))
    if "watch_page" in fixture:
        stack.enter_context(
            mock.patch.object(page, "_client_get", _make_page_client(fixture["watch_page"], calls))
        )
    if "data_api" in fixture:
        _data_api_patches(stack, fixture["data_api"], calls)

    cache_spec = fixture.get("cache", {})
    if "video_meta" in cache_spec and video_id:
        cache.put_video_meta(video_id, dict(cache_spec["video_meta"]))
    if "corrupt_transcript" in cache_spec and video_id:
        lang = cache_spec["corrupt_transcript"].get("lang", "en")
        cache.put_transcript(
            video_id=video_id,
            lang=lang,
            text="corrupt",
            segments=None,
            word_count=1,
            validated=True,
            warnings=[],
        )
        with cache.connect() as conn:
            conn.execute(
                "UPDATE transcripts SET segments_json = '{broken' WHERE video_id = ?",
                (video_id,),
            )
    return calls
