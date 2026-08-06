from __future__ import annotations

import json

import pytest

from youtube_mcp_v2 import cache, transcript_acquisition
from youtube_mcp_v2.adapters import transcript_api, ytdlp_transcript
from youtube_mcp_v2.adapters.transcript_api import Segment, TranscriptFetch
from youtube_mcp_v2.tools.transcript import transcript_get


VIDEO_ID = "jNQXAC9IVRw"


def _fetch(lang: str = "en", generated: bool = False) -> TranscriptFetch:
    return TranscriptFetch(
        segments=[Segment(start_s=0.0, duration_s=2.0, text="hello world")],
        lang=lang,
        is_generated=generated,
    )


def test_primary_success_does_not_call_fallback(monkeypatch) -> None:
    monkeypatch.setattr(transcript_api, "fetch_transcript", lambda *_a, **_k: _fetch())
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        lambda *_a, **_k: pytest.fail("fallback called after primary success"),
    )

    result = transcript_acquisition.acquire_transcript(VIDEO_ID, "en")
    assert result.provider == "youtube-transcript-api"
    assert result.fetch.lang == "en"
    assert [a.outcome for a in result.attempts] == ["success"]


def test_primary_failure_then_ytdlp_success_records_both_attempts(monkeypatch) -> None:
    def primary_fail(*_a, **_k):
        raise RuntimeError("primary unavailable")

    monkeypatch.setattr(transcript_api, "fetch_transcript", primary_fail)
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        lambda *_a, **_k: _fetch(lang="en-US", generated=True),
    )

    result = transcript_acquisition.acquire_transcript(VIDEO_ID, "fr")
    assert result.provider == "yt-dlp"
    assert result.fetch.lang == "en-US"
    assert result.fetch.is_generated is True
    assert [(a.provider, a.outcome) for a in result.attempts] == [
        ("youtube-transcript-api", "error"),
        ("yt-dlp", "success"),
    ]
    assert result.provenance["acquisition"]["attempts"][0]["detail_code"] == "provider_error"


def test_blocked_primary_is_classified_without_exposing_exception_text(monkeypatch) -> None:
    class RequestBlocked(RuntimeError):
        pass

    def primary_fail(*_a, **_k):
        raise RequestBlocked("secret-ish upstream diagnostic")

    monkeypatch.setattr(transcript_api, "fetch_transcript", primary_fail)
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        lambda *_a, **_k: _fetch(),
    )

    result = transcript_acquisition.acquire_transcript(VIDEO_ID)
    attempt = result.provenance["acquisition"]["attempts"][0]
    assert attempt["outcome"] == "blocked"
    assert attempt["detail_code"] == "request_blocked"
    assert "secret-ish" not in json.dumps(result.provenance)


def test_both_providers_fail_with_structured_attempt_history(monkeypatch) -> None:
    def primary_fail(*_a, **_k):
        raise RuntimeError("primary error")

    def fallback_fail(*_a, **_k):
        raise ytdlp_transcript.YtDlpNoTranscript("no captions")

    monkeypatch.setattr(transcript_api, "fetch_transcript", primary_fail)
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", fallback_fail)

    with pytest.raises(transcript_acquisition.TranscriptAcquisitionFailed) as exc_info:
        transcript_acquisition.acquire_transcript(VIDEO_ID)

    exc = exc_info.value
    assert [a.outcome for a in exc.attempts] == ["error", "unavailable"]
    assert exc.attempts[1].detail_code == "no_transcript"
    assert exc.provenance["acquisition"]["provider"] is None


def test_cache_hit_preserves_original_acquisition_provenance(tmp_path, monkeypatch) -> None:
    cache_dir = tmp_path / "youtube-mcp"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_PATH", cache_dir / "v2.sqlite")

    attempts = [
        {
            "provider": "youtube-transcript-api",
            "method": "captions",
            "duration_ms": 100,
            "outcome": "blocked",
            "detail_code": "request_blocked",
        },
        {
            "provider": "yt-dlp",
            "method": "captions",
            "duration_ms": 200,
            "outcome": "success",
            "detail_code": None,
        },
    ]
    cache.put_transcript(
        video_id=VIDEO_ID,
        lang="fr",
        actual_lang="en",
        is_generated=True,
        provider="yt-dlp",
        method="captions",
        attempts=attempts,
        text="hello world",
        segments=[
            {
                "start_s": 0.0,
                "duration_s": 2.0,
                "end_s": 2.0,
                "text": "hello world",
            }
        ],
        word_count=2,
        validated=True,
        warnings=["lang fallback: requested=fr got=en"],
    )

    env = transcript_get(VIDEO_ID, lang="fr")
    assert env["source"] == "cache"
    assert env["provenance"]["acquisition"]["provider"] == "yt-dlp"
    assert env["provenance"]["acquisition"]["attempts"] == attempts
    assert env["data"]["requested_lang"] == "fr"
    assert env["data"]["lang"] == "en"


def test_legacy_cache_migration_adds_acquisition_columns(tmp_path, monkeypatch) -> None:
    import sqlite3

    cache_dir = tmp_path / "youtube-mcp"
    cache_dir.mkdir(parents=True)
    cache_path = cache_dir / "v2.sqlite"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_PATH", cache_path)

    conn = sqlite3.connect(cache_path)
    conn.execute(
        """
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL,
            lang TEXT NOT NULL,
            text TEXT NOT NULL,
            segments_json TEXT,
            word_count INTEGER,
            fetched_at TEXT NOT NULL,
            validated INTEGER NOT NULL,
            warnings_json TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    with cache.connect() as migrated:
        columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(transcripts)").fetchall()
        }

    assert {"provider", "method", "attempts_json"}.issubset(columns)
