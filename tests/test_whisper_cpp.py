from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from youtube_mcp_v2.adapters import audio_extract, whisper_cpp


VIDEO_ID = "jNQXAC9IVRw"


def test_settings_are_disabled_without_explicit_model(monkeypatch) -> None:
    monkeypatch.delenv("YOUTUBE_MCP_WHISPER_CPP_MODEL", raising=False)
    monkeypatch.delenv("YOUTUBE_MCP_WHISPER_CPP_BIN", raising=False)
    assert whisper_cpp.settings_from_env() is None


def test_configured_missing_model_fails_without_echoing_path(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "private" / "secret-model.bin"
    monkeypatch.setenv("YOUTUBE_MCP_WHISPER_CPP_MODEL", str(missing))
    monkeypatch.setenv("YOUTUBE_MCP_WHISPER_CPP_BIN", "whisper-cli")

    with pytest.raises(whisper_cpp.LocalSttUnavailable) as exc_info:
        whisper_cpp.settings_from_env()
    message = str(exc_info.value)
    assert "does not exist" in message
    assert str(tmp_path) not in message


def test_parse_json_uses_millisecond_offsets_and_detected_language() -> None:
    fetch = whisper_cpp._parse_output(
        {
            "result": {"language": "es"},
            "transcription": [
                {
                    "timestamps": {"from": "00:00:01,000", "to": "00:00:03,500"},
                    "offsets": {"from": 1000, "to": 3500},
                    "text": " hola mundo ",
                },
                {
                    "offsets": {"from": 3500, "to": 5000},
                    "text": "segunda frase",
                },
            ],
        }
    )

    assert fetch.lang == "es"
    assert fetch.is_generated is True
    assert len(fetch.segments) == 2
    assert fetch.segments[0].start_s == pytest.approx(1.0)
    assert fetch.segments[0].duration_s == pytest.approx(2.5)
    assert fetch.segments[0].text == "hola mundo"
    assert fetch.segments[1].end_s == pytest.approx(5.0)


def test_transcribe_video_records_hash_provenance_without_local_paths(
    tmp_path, monkeypatch
) -> None:
    model = tmp_path / "models" / "ggml-base.en.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"fixture-model-weights")
    audio = tmp_path / "audio" / "source.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"RIFF-fixture-audio")

    monkeypatch.setattr(
        audio_extract,
        "extract_audio",
        lambda *_a, **_k: audio_extract.AudioResult(
            path=str(audio),
            format="wav",
            sample_rate=16000,
            mono=True,
            start_s=None,
            end_s=None,
            cached=False,
        ),
    )
    monkeypatch.setattr(whisper_cpp, "_engine_version", lambda _binary: "whisper.cpp fixture")

    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        output_base = Path(command[command.index("-of") + 1])
        output_base.with_suffix(".json").write_text(
            json.dumps(
                {
                    "result": {"language": "en"},
                    "transcription": [
                        {
                            "offsets": {"from": 250, "to": 2250},
                            "text": "locally transcribed evidence",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    settings = whisper_cpp.WhisperCppSettings(
        binary="/private/tools/whisper-cli",
        model=model,
        timeout_s=123,
    )

    result = whisper_cpp.transcribe_video(VIDEO_ID, settings=settings)
    assert result.fetch.lang == "en"
    assert result.fetch.segments[0].start_s == pytest.approx(0.25)
    assert result.fetch.segments[0].end_s == pytest.approx(2.25)
    assert result.provenance["engine"] == "whisper.cpp"
    assert result.provenance["engine_version"] == "whisper.cpp fixture"
    assert result.provenance["model"]["name"] == model.name
    assert result.provenance["model"]["sha256"] == hashlib.sha256(
        model.read_bytes()
    ).hexdigest()
    assert result.provenance["audio"]["sha256"] == hashlib.sha256(
        audio.read_bytes()
    ).hexdigest()
    assert result.provenance["audio"]["sample_rate"] == 16000

    serialized = json.dumps(result.provenance)
    assert str(tmp_path) not in serialized
    assert "/private/tools" not in serialized

    command = commands[0]
    assert command[:2] == ["/private/tools/whisper-cli", "-m"]
    assert "-oj" in command
    assert "-np" in command
    assert command[command.index("-l") + 1] == "auto"


def test_whisper_timeout_is_structured_and_path_safe(tmp_path, monkeypatch) -> None:
    model = tmp_path / "secret-model.bin"
    model.write_bytes(b"model")
    audio = tmp_path / "secret-audio.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(
        audio_extract,
        "extract_audio",
        lambda *_a, **_k: audio_extract.AudioResult(
            path=str(audio),
            format="wav",
            sample_rate=16000,
            mono=True,
            start_s=None,
            end_s=None,
            cached=True,
        ),
    )

    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, timeout=2)

    monkeypatch.setattr(subprocess, "run", timeout)
    settings = whisper_cpp.WhisperCppSettings(
        binary="whisper-cli", model=model, timeout_s=2
    )
    with pytest.raises(whisper_cpp.LocalSttTimeout) as exc_info:
        whisper_cpp.transcribe_video(VIDEO_ID, settings=settings)
    assert "2s" in str(exc_info.value)
    assert str(tmp_path) not in str(exc_info.value)


def test_nonzero_exit_never_echoes_whisper_stderr(tmp_path, monkeypatch) -> None:
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(
        audio_extract,
        "extract_audio",
        lambda *_a, **_k: audio_extract.AudioResult(
            path=str(audio),
            format="wav",
            sample_rate=16000,
            mono=True,
            start_s=None,
            end_s=None,
            cached=True,
        ),
    )
    secret = "PRIVATE /home/alice/models/secret.bin"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 7, stdout="", stderr=secret
        ),
    )

    with pytest.raises(whisper_cpp.LocalSttError) as exc_info:
        whisper_cpp.transcribe_video(
            VIDEO_ID,
            settings=whisper_cpp.WhisperCppSettings(
                binary="whisper-cli", model=model, timeout_s=30
            ),
        )
    assert "status 7" in str(exc_info.value)
    assert secret not in str(exc_info.value)
