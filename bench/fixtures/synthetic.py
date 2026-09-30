"""Benchmark-owned synthetic transcripts and watch-page metadata.

Every string here was written for the benchmark. Real YouTube videos are
referenced only by ID + annotations in live manifests, never by payload.

Synthetic video IDs are 11 characters (the YouTube ID shape) and prefixed
`bnch` so they are recognisable in reports.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# Word bank for filler speech. Deliberately excludes every evidence phrase.
_FILLER = (
    "the speaker returns to the main idea and adds a little more context",
    "another short remark follows while the camera stays on the table",
    "here the presenter pauses and summarises what came before",
    "a brief aside explains why this step matters for the rest",
    "the discussion continues with a careful and measured tone",
    "now the narrator compares two earlier examples side by side",
)


@dataclass(frozen=True)
class SyntheticSegment:
    start_s: float
    duration_s: float
    text: str


@dataclass(frozen=True)
class SyntheticTranscript:
    key: str
    domain: str
    segments: tuple[SyntheticSegment, ...]
    duration_s: int

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments).strip()

    @property
    def text_sha256(self) -> str:
        return sha256_text(self.text)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build(
    key: str,
    domain: str,
    *,
    count: int,
    spacing_s: float,
    offset_s: float = 0.0,
    duration_s: int,
    evidence: dict[int, str] | None = None,
) -> SyntheticTranscript:
    evidence = evidence or {}
    segments = []
    for i in range(count):
        text = evidence.get(i) or f"{_FILLER[i % len(_FILLER)]} part {i}"
        segments.append(
            SyntheticSegment(
                start_s=round(offset_s + i * spacing_s, 3),
                duration_s=round(spacing_s * 0.9, 3),
                text=text,
            )
        )
    return SyntheticTranscript(key, domain, tuple(segments), duration_s)


TRANSCRIPTS: dict[str, SyntheticTranscript] = {
    t.key: t
    for t in (
        _build(
            "hist-treaty",
            "history-documentary",
            count=10,
            spacing_s=6.0,
            duration_s=60,
            evidence={4: "the peace treaty was signed in 1648 after long negotiation"},
        ),
        _build(
            "cook-sauce",
            "cooking-craft",
            count=10,
            spacing_s=6.0,
            duration_s=60,
            evidence={7: "hold the custard at sixty-two degrees until it coats the spoon"},
        ),
        _build(
            "sci-avogadro",
            "science-math-technology",
            count=30,
            spacing_s=5.0,
            duration_s=150,
            evidence={25: "one mole contains the Avogadro constant of particles"},
        ),
        _build(
            "sport-final",
            "sports-games-performance",
            count=630,
            spacing_s=6.0,
            offset_s=0.5,
            duration_s=3780,
            evidence={620: "and the final score is three to two for the visitors"},
        ),
        _build(
            "politics-bill",
            "politics-current-affairs",
            count=200,
            spacing_s=4.0,
            offset_s=0.3,
            duration_s=800,
            evidence={188: "the committee advanced H.R. 4521 by a narrow vote"},
        ),
        _build(
            "podcast-long",
            "long-form-interview",
            count=1200,
            spacing_s=6.0,
            duration_s=7200,
        ),
    )
}


# Compact watch-page descriptions; `watch_page_html` renders them into the
# `ytInitialPlayerResponse` shape the real page adapter parses.
WATCH_PAGES: dict[str, dict] = {
    "manual-en": {
        "title": "Bench Synthetic Treaty Lecture",
        "author": "Bench Archive Channel",
        "channelId": "UCbenchArchive000000000",
        "lengthSeconds": "60",
        "captions": [{"lang": "en", "kind": None}],
    },
    "generated-only": {
        "title": "Bench Synthetic Custard Demo",
        "author": "Bench Kitchen",
        "channelId": "UCbenchKitchen000000000",
        "lengthSeconds": "60",
        "captions": [{"lang": "en", "kind": "asr"}],
    },
    "multilingual": {
        "title": "Bench Synthetic Chemistry Primer",
        "author": "Bench Science",
        "channelId": "UCbenchScience000000000",
        "lengthSeconds": "150",
        "captions": [
            {"lang": "en", "kind": None},
            {"lang": "fr", "kind": None},
            {"lang": "de", "kind": "asr"},
        ],
    },
    "no-captions": {
        "title": "Bench Synthetic Silent Timelapse",
        "author": "Bench Studio",
        "channelId": "UCbenchStudio0000000000",
        "lengthSeconds": "95",
        "captions": [],
    },
    "livestream-archive": {
        "title": "Bench Synthetic Match Stream",
        "author": "Bench Sports",
        "channelId": "UCbenchSports0000000000",
        "lengthSeconds": "3780",
        "isLiveContent": True,
        "captions": [{"lang": "en", "kind": "asr"}],
    },
    "age-gated": {
        "title": "Bench Synthetic Restricted Clip",
        "author": "Bench Mature",
        "channelId": "UCbenchMature0000000000",
        "lengthSeconds": "300",
        "captions": [{"lang": "en", "kind": None}],
        "playability": {"status": "LOGIN_REQUIRED", "playableInEmbed": False},
    },
}


def player_response(video_id: str, spec: dict) -> dict:
    tracks = [
        {
            "baseUrl": f"https://bench.invalid/timedtext?v={video_id}&lang={c['lang']}",
            "languageCode": c["lang"],
            **({"kind": c["kind"]} if c.get("kind") else {}),
        }
        for c in spec.get("captions", [])
    ]
    player: dict = {
        "videoDetails": {
            "videoId": video_id,
            "title": spec["title"],
            "author": spec["author"],
            "channelId": spec["channelId"],
            "lengthSeconds": spec["lengthSeconds"],
            "isLiveContent": bool(spec.get("isLiveContent", False)),
            "viewCount": "1234",
        },
        "playabilityStatus": spec.get("playability", {"status": "OK", "playableInEmbed": True}),
    }
    if tracks:
        player["captions"] = {"playerCaptionsTracklistRenderer": {"captionTracks": tracks}}
    return player


def watch_page_html(video_id: str, spec: dict) -> str:
    import json

    return (
        "<!DOCTYPE html><html><head>"
        f"<title>{spec['title']} - YouTube</title></head><body>"
        "<script>var ytInitialPlayerResponse = "
        f"{json.dumps(player_response(video_id, spec))};</script>"
        "</body></html>"
    )
