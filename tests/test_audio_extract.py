from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from youtube_mcp_v2.tools.audio import audio_get
from youtube_mcp_v2.adapters import audio_extract


VIDEO_ID = "jNQXAC9IVRw"


def test_audio_get_bad_url() -> None:
    env = audio_get("https://example.com/not-youtube")
    assert env["error"] is not None
    assert env["error"]["code"] == "bad_url"


def test_audio_get_extracts_mono_wav(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(audio_extract, "AUDIO_DIR", tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], timeout_s: int):
        calls.append(cmd)
        if "yt-dlp" in cmd[0]:
            template = Path(cmd[cmd.index("-o") + 1])
            out_path = Path(
                str(template)
                .replace("%(id)s", VIDEO_ID)
                .replace("%(ext)s", "webm")
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"video")
        if "ffmpeg" in cmd[0]:
            out_path = Path(cmd[-1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"RIFF....WAVE")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(audio_extract, "_run", fake_run)
    monkeypatch.setattr(audio_extract, "YT_DLP", "yt-dlp")
    monkeypatch.setattr(audio_extract, "FFMPEG", "ffmpeg")

    env = audio_get(
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        fmt="wav",
        sample_rate=22050,
    )

    assert env["error"] is None
    assert env["data"]["id"] == VIDEO_ID
    assert env["data"]["format"] == "wav"
    assert env["data"]["sample_rate"] == 22050
    assert env["data"]["mono"] is True
    assert Path(env["data"]["path"]).exists()
    assert any("yt-dlp" in cmd[0] for cmd in calls)
    assert any("ffmpeg" in cmd[0] for cmd in calls)


def test_audio_download_reuses_ytdlp_proxy_and_cookie_file(monkeypatch, tmp_path) -> None:
    proxy = "socks5://user:password@proxy.example:1080"
    cookies = str(tmp_path / "private-cookies.txt")
    monkeypatch.setenv("YOUTUBE_YTDLP_PROXY", proxy)
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", cookies)
    monkeypatch.delenv("YOUTUBE_COOKIES_FROM_BROWSER", raising=False)
    monkeypatch.setattr(audio_extract, "YT_DLP", "yt-dlp")

    seen: list[str] = []

    def fake_run(cmd: list[str], _timeout_s: int):
        seen.extend(cmd)
        template = Path(cmd[cmd.index("-o") + 1])
        output = Path(
            str(template).replace("%(id)s", VIDEO_ID).replace("%(ext)s", "webm")
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"audio-source")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(audio_extract, "_run", fake_run)
    result = audio_extract._download_audio(VIDEO_ID, tmp_path / "download")
    assert result.is_file()
    assert seen[seen.index("--proxy") + 1] == proxy
    assert seen[seen.index("--cookies") + 1] == cookies


def test_audio_download_redacts_ytdlp_credentials_from_error(monkeypatch, tmp_path) -> None:
    proxy = "socks5://alice:secret@proxy.example:1080"
    cookies = str(tmp_path / "private-cookies.txt")
    monkeypatch.setenv("YOUTUBE_YTDLP_PROXY", proxy)
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", cookies)
    monkeypatch.delenv("YOUTUBE_COOKIES_FROM_BROWSER", raising=False)
    monkeypatch.setattr(audio_extract, "YT_DLP", "yt-dlp")

    monkeypatch.setattr(
        audio_extract,
        "_run",
        lambda _cmd, _timeout_s: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"failed through {proxy} using {cookies}",
        ),
    )

    with pytest.raises(audio_extract.AudioExtractError) as exc_info:
        audio_extract._download_audio(VIDEO_ID, tmp_path / "download")
    message = str(exc_info.value)
    assert proxy not in message
    assert cookies not in message
    assert "alice:secret" not in message
    assert "<redacted>" in message
