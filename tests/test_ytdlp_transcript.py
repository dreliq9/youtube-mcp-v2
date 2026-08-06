from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from youtube_mcp_v2.adapters import ytdlp_transcript


VIDEO_ID = "jNQXAC9IVRw"


def _clear_env(monkeypatch) -> None:
    for name in (
        "YOUTUBE_YTDLP_PROXY",
        "YOUTUBE_COOKIES_FILE",
        "YOUTUBE_COOKIES_FROM_BROWSER",
    ):
        monkeypatch.delenv(name, raising=False)


def test_settings_summary_never_contains_proxy_or_cookie_values(monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(
        "YOUTUBE_YTDLP_PROXY",
        "socks5://secret-user:secret-pass@proxy.example:1080",
    )
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", "/secret/path/cookies.txt")

    settings = ytdlp_transcript.settings_from_env()
    assert settings.summary == {"proxy_configured": True, "cookies_mode": "file"}
    rendered = json.dumps(settings.summary)
    assert "secret-user" not in rendered
    assert "secret-pass" not in rendered
    assert "cookies.txt" not in rendered


def test_cookie_file_and_browser_modes_are_mutually_exclusive(monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", "/tmp/cookies")
    monkeypatch.setenv("YOUTUBE_COOKIES_FROM_BROWSER", "firefox:profile")
    with pytest.raises(ValueError, match="either YOUTUBE_COOKIES_FILE"):
        ytdlp_transcript.settings_from_env()


def test_redact_removes_exact_secret_values_and_proxy_userinfo() -> None:
    settings = ytdlp_transcript.YtDlpSettings(
        proxy="http://alice:password@proxy.example:8080",
        cookies_file="/home/alice/private-cookies.txt",
        cookies_from_browser=None,
    )
    text = (
        "proxy http://alice:password@proxy.example:8080 "
        "cookies /home/alice/private-cookies.txt "
        "normalized socks5://bob:hunter2@other.example:1080"
    )
    redacted = ytdlp_transcript._redact(text, settings)
    assert "password" not in redacted
    assert "private-cookies" not in redacted
    assert "hunter2" not in redacted
    assert "<redacted>" in redacted


def test_requested_manual_track_beats_requested_generated_track() -> None:
    info = {
        "subtitles": {"en": [{"ext": "json3"}]},
        "automatic_captions": {"en": [{"ext": "json3"}]},
    }
    assert ytdlp_transcript._select_track(info, "en") == ("en", False)


def test_requested_generated_beats_unrequested_manual_track() -> None:
    info = {
        "subtitles": {"de": [{"ext": "json3"}]},
        "automatic_captions": {"fr-FR": [{"ext": "json3"}]},
    }
    assert ytdlp_transcript._select_track(info, "fr") == ("fr-FR", True)


def test_english_fallback_precedes_arbitrary_track() -> None:
    info = {
        "subtitles": {"de": [{"ext": "json3"}], "en-US": [{"ext": "json3"}]},
        "automatic_captions": {},
    }
    assert ytdlp_transcript._select_track(info, "fr") == ("en-US", False)


def test_any_track_fallback_is_deterministic_and_prefers_manual() -> None:
    info = {
        "subtitles": {"ja": [{"ext": "json3"}], "de": [{"ext": "json3"}]},
        "automatic_captions": {"en": [{"ext": "json3"}]},
    }
    # English generated wins because English is the documented second preference.
    assert ytdlp_transcript._select_track(info, "fr") == ("en", True)

    no_english = {
        "subtitles": {"ja": [{"ext": "json3"}], "de": [{"ext": "json3"}]},
        "automatic_captions": {"es": [{"ext": "json3"}]},
    }
    assert ytdlp_transcript._select_track(no_english, "fr") == ("de", False)


def test_no_tracks_raises_explicit_no_transcript() -> None:
    with pytest.raises(ytdlp_transcript.YtDlpNoTranscript):
        ytdlp_transcript._select_track(
            {"subtitles": {}, "automatic_captions": {"live_chat": [{}]}},
            "en",
        )


def test_json3_parser_builds_timestamped_segments(tmp_path: Path) -> None:
    path = tmp_path / "captions.en.json3"
    path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "tStartMs": 1000,
                        "dDurationMs": 2500,
                        "segs": [{"utf8": "hello "}, {"utf8": "world"}],
                    },
                    {"tStartMs": 4000, "dDurationMs": 1000, "segs": [{"utf8": "next"}]},
                ]
            }
        ),
        encoding="utf-8",
    )
    segments = ytdlp_transcript._parse_json3(path)
    assert [(s.start_s, s.duration_s, s.text) for s in segments] == [
        (1.0, 2.5, "hello world"),
        (4.0, 1.0, "next"),
    ]


def test_vtt_parser_strips_markup_and_decodes_entities(tmp_path: Path) -> None:
    path = tmp_path / "captions.en.vtt"
    path.write_text(
        """WEBVTT\n\n00:00:01.000 --> 00:00:03.500\n<c>Hello &amp; welcome</c>\n\n00:00:04.000 --> 00:00:05.000\nNext line\n\n""",
        encoding="utf-8",
    )
    segments = ytdlp_transcript._parse_vtt(path)
    assert [(s.start_s, s.duration_s, s.text) for s in segments] == [
        (1.0, 2.5, "Hello & welcome"),
        (4.0, 1.0, "Next line"),
    ]


def test_fetch_transcript_uses_selected_track_and_temporary_file(monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setattr(
        ytdlp_transcript,
        "_dump_info",
        lambda *_a, **_k: {
            "subtitles": {},
            "automatic_captions": {"en": [{"ext": "json3"}]},
        },
    )

    def fake_download(
        video_id: str,
        lang: str,
        *,
        generated: bool,
        settings,
        dest_dir: Path,
    ) -> Path:
        assert video_id == VIDEO_ID
        assert lang == "en"
        assert generated is True
        path = dest_dir / f"{video_id}.en.json3"
        path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "tStartMs": 0,
                            "dDurationMs": 1000,
                            "segs": [{"utf8": "fallback works"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return path

    monkeypatch.setattr(ytdlp_transcript, "_download_track", fake_download)
    result = ytdlp_transcript.fetch_transcript(VIDEO_ID, "en")
    assert result.lang == "en"
    assert result.is_generated is True
    assert result.segments[0].text == "fallback works"


def test_missing_executable_becomes_provider_unavailable(monkeypatch) -> None:
    _clear_env(monkeypatch)

    def missing(*_a, **_k):
        raise FileNotFoundError("no binary")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ytdlp_transcript.YtDlpUnavailable, match="not found"):
        ytdlp_transcript._run(
            ["yt-dlp", "--version"],
            timeout_s=1,
            settings=ytdlp_transcript.settings_from_env(),
        )
