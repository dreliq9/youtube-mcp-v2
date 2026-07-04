from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from youtube_mcp_v2.tools.audio import audio_get
from youtube_mcp_v2.adapters import audio_extract


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
            out_path = Path(str(template).replace("%(id)s", "jNQXAC9IVRw").replace("%(ext)s", "webm"))
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

    env = audio_get("https://www.youtube.com/watch?v=jNQXAC9IVRw", fmt="wav", sample_rate=22050)

    assert env["error"] is None
    assert env["data"]["id"] == "jNQXAC9IVRw"
    assert env["data"]["format"] == "wav"
    assert env["data"]["sample_rate"] == 22050
    assert env["data"]["mono"] is True
    assert Path(env["data"]["path"]).exists()
    assert any("yt-dlp" in cmd[0] for cmd in calls)
    assert any("ffmpeg" in cmd[0] for cmd in calls)
