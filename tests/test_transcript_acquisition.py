from __future__ import annotations

import json

import pytest

from youtube_mcp_v2 import cache, transcript_acquisition
from youtube_mcp_v2.adapters import transcript_api, whisper_cpp, ytdlp_transcript
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
    monkeypatch.setattr(
        whisper_cpp,
        "settings_from_env",
        lambda: pytest.fail("local STT inspected after primary success"),
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
    monkeypatch.setattr(
        whisper_cpp,
        "settings_from_env",
        lambda: pytest.fail("local STT inspected after yt-dlp success"),
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


def test_both_caption_providers_fail_without_local_model_preserves_two_attempts(
    monkeypatch,
) -> None:
    def primary_fail(*_a, **_k):
        raise RuntimeError("primary error")

    def fallback_fail(*_a, **_k):
        raise ytdlp_transcript.YtDlpNoTranscript("no captions")

    monkeypatch.setattr(transcript_api, "fetch_transcript", primary_fail)
    monkeypatch.setattr(ytdlp_transcript, "fetch_transcript", fallback_fail)
    monkeypatch.setattr(whisper_cpp, "settings_from_env", lambda: None)

    with pytest.raises(transcript_acquisition.TranscriptAcquisitionFailed) as exc_info:
        transcript_acquisition.acquire_transcript(VIDEO_ID)

    exc = exc_info.value
    assert [a.outcome for a in exc.attempts] == ["error", "unavailable"]
    assert exc.attempts[1].detail_code == "no_transcript"
    assert exc.local_stt_error is None
    assert exc.provenance["acquisition"]["provider"] is None


def test_membership_required_stops_before_local_stt(monkeypatch) -> None:
    monkeypatch.setattr(
        transcript_api,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(
            transcript_api.NoTranscriptFound(VIDEO_ID, ["en"], None)
        ),
    )
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(
            ytdlp_transcript.YtDlpTranscriptError(
                "Join this channel to get access to members-only content"
            )
        ),
    )
    monkeypatch.setattr(
        whisper_cpp,
        "settings_from_env",
        lambda: pytest.fail("local STT inspected after members-only response"),
    )
    monkeypatch.setattr(
        whisper_cpp,
        "transcribe_video",
        lambda *_a, **_k: pytest.fail("audio download started after members-only response"),
    )

    with pytest.raises(transcript_acquisition.TranscriptAcquisitionFailed) as exc_info:
        transcript_acquisition.acquire_transcript(VIDEO_ID)

    exc = exc_info.value
    assert [(a.provider, a.outcome, a.detail_code) for a in exc.attempts] == [
        ("youtube-transcript-api", "unavailable", "no_transcript"),
        ("yt-dlp", "unavailable", "membership_required"),
    ]
    assert exc.local_stt_error is None


def test_caption_failures_then_local_stt_success_records_model_details(monkeypatch) -> None:
    monkeypatch.setattr(
        transcript_api,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(
            transcript_api.NoTranscriptFound(VIDEO_ID, ["en"], None)
        ),
    )
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(
            ytdlp_transcript.YtDlpNoTranscript("none")
        ),
    )
    settings = whisper_cpp.WhisperCppSettings(
        binary="whisper-cli",
        model=__import__("pathlib").Path("fixture.bin"),
        timeout_s=30,
    )
    monkeypatch.setattr(whisper_cpp, "settings_from_env", lambda: settings)
    details = {
        "engine": "whisper.cpp",
        "engine_version": "fixture-v1",
        "model": {"name": "fixture.bin", "size_bytes": 10, "sha256": "a" * 64},
        "audio": {"sha256": "b" * 64, "sample_rate": 16000, "format": "wav"},
    }
    monkeypatch.setattr(
        whisper_cpp,
        "transcribe_video",
        lambda *_a, **_k: whisper_cpp.WhisperCppResult(
            fetch=_fetch(lang="en", generated=True),
            provenance=details,
        ),
    )

    result = transcript_acquisition.acquire_transcript(VIDEO_ID)
    assert result.provider == "whisper.cpp"
    assert result.method == "local_stt"
    assert result.details == details
    assert [(a.provider, a.outcome) for a in result.attempts] == [
        ("youtube-transcript-api", "unavailable"),
        ("yt-dlp", "unavailable"),
        ("whisper.cpp", "success"),
    ]
    assert result.provenance["acquisition"]["details"] == details


def test_configured_local_stt_failure_adds_third_attempt(monkeypatch) -> None:
    monkeypatch.setattr(
        transcript_api,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(
            transcript_api.NoTranscriptFound(VIDEO_ID, ["en"], None)
        ),
    )
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(
            ytdlp_transcript.YtDlpNoTranscript("none")
        ),
    )
    settings = whisper_cpp.WhisperCppSettings(
        binary="whisper-cli",
        model=__import__("pathlib").Path("fixture.bin"),
        timeout_s=30,
    )
    monkeypatch.setattr(whisper_cpp, "settings_from_env", lambda: settings)
    monkeypatch.setattr(
        whisper_cpp,
        "transcribe_video",
        lambda *_a, **_k: (_ for _ in ()).throw(
            whisper_cpp.LocalSttTimeout("fixture timeout")
        ),
    )

    with pytest.raises(transcript_acquisition.TranscriptAcquisitionFailed) as exc_info:
        transcript_acquisition.acquire_transcript(VIDEO_ID)

    exc = exc_info.value
    assert isinstance(exc.local_stt_error, whisper_cpp.LocalSttTimeout)
    assert [(a.provider, a.outcome, a.detail_code) for a in exc.attempts] == [
        ("youtube-transcript-api", "unavailable", "no_transcript"),
        ("yt-dlp", "unavailable", "no_transcript"),
        ("whisper.cpp", "timeout", "local_stt_timeout"),
    ]


def test_failed_local_stt_makes_no_caption_result_recoverable(monkeypatch) -> None:
    exc = transcript_acquisition.TranscriptAcquisitionFailed(
        "failed",
        attempts=[
            transcript_acquisition.AcquisitionAttempt(
                provider="youtube-transcript-api",
                method="captions",
                duration_ms=1,
                outcome="unavailable",
                detail_code="no_transcript",
            ),
            transcript_acquisition.AcquisitionAttempt(
                provider="whisper.cpp",
                method="local_stt",
                duration_ms=2,
                outcome="error",
                detail_code="local_stt_failed",
            ),
        ],
        primary_error=transcript_api.NoTranscriptFound(VIDEO_ID, ["en"], None),
        fallback_error=ytdlp_transcript.YtDlpNoTranscript("none"),
        local_stt_error=whisper_cpp.LocalSttError("broken local config"),
    )
    monkeypatch.setattr(
        transcript_acquisition,
        "acquire_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(exc),
    )
    monkeypatch.setattr(cache, "get_transcript", lambda *_a, **_k: None)

    env = transcript_get(VIDEO_ID)
    assert env["error"]["code"] == "transcript_fetch_failed"
    assert env["error"]["recoverable"] is True
    assert env["provenance"]["acquisition"]["attempts"][-1]["provider"] == "whisper.cpp"


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
            "provider": "whisper.cpp",
            "method": "local_stt",
            "duration_ms": 200,
            "outcome": "success",
            "detail_code": None,
        },
    ]
    details = {
        "engine": "whisper.cpp",
        "engine_version": "fixture-v1",
        "model": {"name": "ggml-base.en.bin", "size_bytes": 123, "sha256": "a" * 64},
        "audio": {"sha256": "b" * 64, "sample_rate": 16000, "format": "wav"},
    }
    cache.put_transcript(
        video_id=VIDEO_ID,
        lang="fr",
        actual_lang="en",
        is_generated=True,
        provider="whisper.cpp",
        method="local_stt",
        attempts=attempts,
        details=details,
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
    assert env["provenance"]["acquisition"]["provider"] == "whisper.cpp"
    assert env["provenance"]["acquisition"]["attempts"] == attempts
    assert env["provenance"]["acquisition"]["details"] == details
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

    assert {"provider", "method", "attempts_json", "details_json"}.issubset(columns)
