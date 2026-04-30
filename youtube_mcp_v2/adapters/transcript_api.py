"""Wraps youtube-transcript-api with a v0.2-shaped output.

Returns a list of segments and the resolved language. The caller decides
whether to flatten to text or keep timed segments.
"""

from __future__ import annotations

from dataclasses import dataclass

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (  # noqa: F401  (re-exported)
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)


@dataclass
class Segment:
    start_s: float
    duration_s: float
    text: str

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


@dataclass
class TranscriptFetch:
    segments: list[Segment]
    lang: str
    is_generated: bool


def fetch_transcript(video_id: str, lang: str = "en") -> TranscriptFetch:
    """Fetch a transcript. Tries `lang`, then English, then any available track.

    Raises NoTranscriptFound if absolutely nothing is available, or the upstream
    error if the video is private/disabled/etc.
    """
    ytt = YouTubeTranscriptApi()

    # First attempt: requested language with English fallback.
    try:
        result = ytt.fetch(video_id, languages=[lang, "en"])
        return _to_fetch(result, fallback_lang=lang)
    except NoTranscriptFound:
        pass

    # Last-resort: any track at all.
    transcript_list = ytt.list(video_id)
    available = list(transcript_list)
    if not available:
        raise NoTranscriptFound(video_id, [lang], None)
    track = available[0]
    fetched = track.fetch()
    return _to_fetch(fetched, fallback_lang=track.language_code, is_generated=track.is_generated)


def _to_fetch(result, *, fallback_lang: str, is_generated: bool | None = None) -> TranscriptFetch:
    segs = [
        Segment(start_s=float(s.start), duration_s=float(s.duration), text=s.text)
        for s in result.snippets
    ]
    lang = getattr(result, "language_code", None) or fallback_lang
    gen = is_generated if is_generated is not None else bool(getattr(result, "is_generated", False))
    return TranscriptFetch(segments=segs, lang=lang, is_generated=gen)
