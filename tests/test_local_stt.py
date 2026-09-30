"""Local STT adapter seam (mlx-whisper / whisper.cpp) and captionless corpus entry.

No model, audio download, or network is used: the engine call is mocked at the
isolation boundary and audio is a tiny generated WAV.
"""

from __future__ import annotations

import json
import os
import sys
import types
import wave
from pathlib import Path

import pytest

from youtube_mcp_v2 import cache, corpus_index, skeleton, transcript_acquisition
from youtube_mcp_v2.adapters import (
    local_stt,
    mlx_stt,
    stt_common,
    transcript_api,
    whisper_cpp,
    ytdlp_transcript,
)
from youtube_mcp_v2.tools.corpus import corpus_hydrate, corpus_search

VIDEO_ID = "jNQXAC9IVRw"
HANDLE = "topic-captionless-stt-20260901-000000"
REPO_COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _clean_stt_env(monkeypatch):
    for name in (
        local_stt.ENV_BACKEND,
        mlx_stt.ENV_MODEL,
        mlx_stt.ENV_TIMEOUT,
        stt_common.ENV_LANGUAGE,
        "YOUTUBE_MCP_WHISPER_CPP_MODEL",
        "YOUTUBE_MCP_WHISPER_CPP_BIN",
    ):
        monkeypatch.delenv(name, raising=False)


def _mlx_model_dir(root: Path) -> Path:
    model_dir = root / "hf" / "models--mlx-community--whisper-tiny" / "snapshots" / REPO_COMMIT
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text('{"n_mels": 80}', encoding="utf-8")
    (model_dir / "weights.npz").write_bytes(b"fixture-weights")
    return model_dir


def _wav(path: Path, seconds: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(stt_common.SAMPLE_RATE)
        writer.writeframes(b"\x00\x00" * int(stt_common.SAMPLE_RATE * seconds))
    return path


def _payload() -> dict:
    words = "the regulator loop uses an LM5176-Q1 buck boost controller for the bench supply"
    return {
        "language": "en",
        "segments": [
            {"start": 0.0, "end": 20.0, "text": f"Welcome back. {words} rail. " * 3,
             "avg_logprob": -0.21, "no_speech_prob": 0.02},
            {"start": 20.0, "end": 40.0, "text": "Next we measure ripple on the output capacitor bank. " * 4,
             "avg_logprob": -0.35, "no_speech_prob": 0.05},
            {"start": 40.0, "end": 60.0, "text": "Finally the thermal camera shows the inductor hot spot. " * 4,
             "avg_logprob": -0.30, "no_speech_prob": 0.01},
        ],
    }


# --------------------------------------------------------------------------- #
# Backend resolution


def test_no_local_backend_configured_resolves_to_none() -> None:
    assert local_stt.resolve_backend() is None


def test_off_disables_even_when_models_are_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(mlx_stt.ENV_MODEL, str(_mlx_model_dir(tmp_path)))
    monkeypatch.setenv(local_stt.ENV_BACKEND, "off")
    assert local_stt.resolve_backend() is None


def test_invalid_backend_choice_is_structured(monkeypatch) -> None:
    monkeypatch.setenv(local_stt.ENV_BACKEND, "cloud-whisper")
    with pytest.raises(local_stt.LocalSttUnavailable) as info:
        local_stt.resolve_backend()
    assert local_stt.ENV_BACKEND in str(info.value)


def test_auto_prefers_mlx_when_runtime_available(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(mlx_stt.ENV_MODEL, str(_mlx_model_dir(tmp_path)))
    monkeypatch.setattr(mlx_stt, "ensure_runtime", lambda: None)
    backend = local_stt.resolve_backend()
    assert backend is not None and backend.name == "mlx-whisper"


def test_auto_falls_back_to_whisper_cpp_off_apple_silicon(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(mlx_stt.ENV_MODEL, str(_mlx_model_dir(tmp_path)))
    monkeypatch.setattr(mlx_stt, "is_supported_platform", lambda: False)
    sentinel = object()
    monkeypatch.setattr(whisper_cpp, "settings_from_env", lambda: sentinel)
    backend = local_stt.resolve_backend()
    assert backend is not None and backend.name == "whisper.cpp"


def test_auto_reports_mlx_unavailable_when_nothing_else_is_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(mlx_stt.ENV_MODEL, str(_mlx_model_dir(tmp_path)))
    monkeypatch.setattr(mlx_stt, "is_supported_platform", lambda: False)
    with pytest.raises(local_stt.LocalSttUnavailable) as info:
        local_stt.resolve_backend()
    assert local_stt.backend_name_for_error(info.value) == "mlx-whisper"
    assert "Apple Silicon" in str(info.value)


def test_explicit_mlx_without_extra_points_at_optional_extra(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(mlx_stt.ENV_MODEL, str(_mlx_model_dir(tmp_path)))
    monkeypatch.setenv(local_stt.ENV_BACKEND, "mlx-whisper")
    monkeypatch.setattr(mlx_stt, "is_supported_platform", lambda: True)
    monkeypatch.setattr(mlx_stt.importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(local_stt.LocalSttUnavailable) as info:
        local_stt.resolve_backend()
    assert "stt-mlx" in str(info.value)


def test_explicit_whisper_cpp_without_model_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv(local_stt.ENV_BACKEND, "whisper.cpp")
    with pytest.raises(local_stt.LocalSttUnavailable):
        local_stt.resolve_backend()


def test_mlx_model_must_be_a_local_directory_never_a_repo_id(monkeypatch) -> None:
    monkeypatch.setenv(mlx_stt.ENV_MODEL, "mlx-community/whisper-large-v3-turbo")
    with pytest.raises(local_stt.LocalSttUnavailable) as info:
        mlx_stt.settings_from_env()
    assert "never downloaded" in str(info.value)
    assert "mlx-community" not in str(info.value)


def test_mlx_model_dir_requires_config_and_weights(tmp_path, monkeypatch) -> None:
    empty = tmp_path / "model"
    empty.mkdir()
    monkeypatch.setenv(mlx_stt.ENV_MODEL, str(empty))
    with pytest.raises(local_stt.LocalSttUnavailable):
        mlx_stt.settings_from_env()


def test_language_hint_is_validated(monkeypatch) -> None:
    monkeypatch.setenv(stt_common.ENV_LANGUAGE, "de")
    assert stt_common.language_hint() == "de"
    monkeypatch.setenv(stt_common.ENV_LANGUAGE, "auto")
    assert stt_common.language_hint() is None
    monkeypatch.setenv(stt_common.ENV_LANGUAGE, "../../etc")
    with pytest.raises(local_stt.LocalSttUnavailable):
        stt_common.language_hint()


# --------------------------------------------------------------------------- #
# MLX adapter provenance


def _patch_mlx(monkeypatch, tmp_path: Path, payload: dict | None = None) -> Path:
    model_dir = _mlx_model_dir(tmp_path)
    audio = _wav(tmp_path / "audio" / f"{VIDEO_ID}.wav", seconds=0.5)
    monkeypatch.setenv(mlx_stt.ENV_MODEL, str(model_dir))
    monkeypatch.setattr(mlx_stt, "ensure_runtime", lambda: None)
    monkeypatch.setattr(stt_common, "acquire_audio", lambda _video_id: audio)
    calls: list[dict] = []

    def fake_engine(audio_path, settings, decode):
        calls.append({"audio": audio_path, "decode": decode})
        return payload if payload is not None else _payload()

    monkeypatch.setattr(mlx_stt, "_run_engine", fake_engine)
    monkeypatch.setattr(mlx_stt, "_engine_version", lambda: "mlx-whisper 0.4.3, mlx 0.29.0")
    return audio


def test_mlx_provenance_records_model_revision_decode_audio_and_segments(
    tmp_path, monkeypatch
) -> None:
    audio = _patch_mlx(monkeypatch, tmp_path)
    monkeypatch.setenv(stt_common.ENV_LANGUAGE, "en")

    result = mlx_stt.transcribe_video(VIDEO_ID)

    prov = result.provenance
    assert prov["schema"] == "youtube-mcp.local-stt/1"
    assert prov["engine"] == "mlx-whisper"
    assert prov["engine_version"] == "mlx-whisper 0.4.3, mlx 0.29.0"
    assert prov["model"]["name"] == "mlx-community/whisper-tiny"
    assert prov["model"]["revision"] == REPO_COMMIT
    assert prov["model"]["format"] == "mlx"
    assert len(prov["model"]["sha256"]) == 64
    assert {f["name"] for f in prov["model"]["files"]} == {"config.json", "weights.npz"}
    assert prov["decode"]["language"] == "en"
    assert prov["decode"]["temperature"] == list(mlx_stt.DEFAULT_DECODE["temperature"])
    assert prov["decode"]["sample_rate"] == 16000
    assert prov["audio"] == {
        "sha256": stt_common.sha256_file(audio),
        "sample_rate": 16000,
        "format": "wav",
        "duration_s": 0.5,
    }
    seg = prov["segments"]
    assert seg["count"] == 3 and seg["monotonic"] is True
    assert seg["first_start_s"] == 0.0 and seg["last_end_s"] == 60.0
    assert seg["max_no_speech_prob"] == pytest.approx(0.05)
    assert result.fetch.lang == "en" and result.fetch.is_generated is True
    # Provenance is hash-based: no local filesystem paths leak into the cache.
    assert str(tmp_path) not in json.dumps(prov)


def test_model_identity_changes_when_weights_change(tmp_path) -> None:
    model_dir = _mlx_model_dir(tmp_path)
    first = mlx_stt.model_identity(model_dir)
    (model_dir / "weights.npz").write_bytes(b"different-weights")
    second = mlx_stt.model_identity(model_dir)
    assert first["sha256"] != second["sha256"]


def test_mlx_output_with_bad_timings_is_rejected(tmp_path, monkeypatch) -> None:
    _patch_mlx(monkeypatch, tmp_path, payload={"segments": [{"start": 5.0, "end": 1.0, "text": "x"}]})
    with pytest.raises(local_stt.LocalSttError):
        mlx_stt.transcribe_video(VIDEO_ID)


def test_mlx_timeout_and_failures_never_echo_paths(tmp_path, monkeypatch) -> None:
    model_dir = _mlx_model_dir(tmp_path)
    settings = mlx_stt.MlxWhisperSettings(model_dir=model_dir, timeout_s=5)

    def timeout(*_a, **_k):
        raise TimeoutError(f"worker for {model_dir} timed out")

    monkeypatch.setattr(mlx_stt.isolation, "run_isolated", timeout)
    with pytest.raises(local_stt.LocalSttTimeout) as info:
        mlx_stt._run_engine(tmp_path / "a.wav", settings, {})
    assert str(tmp_path) not in str(info.value)

    def boom(*_a, **_k):
        raise RuntimeError(f"cannot open {tmp_path}/a.wav")

    monkeypatch.setattr(mlx_stt.isolation, "run_isolated", boom)
    with pytest.raises(local_stt.LocalSttError) as info:
        mlx_stt._run_engine(tmp_path / "a.wav", settings, {})
    assert str(tmp_path) not in str(info.value)


def test_worker_passes_pinned_decode_config_to_mlx_whisper(monkeypatch) -> None:
    seen: dict = {}

    def transcribe(audio, **kwargs):
        seen["audio"] = audio
        seen.update(kwargs)
        return {"language": "en", "segments": [{"start": 0, "end": 1.5, "text": " hi ", "avg_logprob": -0.1}]}

    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=transcribe))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)  # restored after the test
    decode = {**mlx_stt.DEFAULT_DECODE, "language": "auto", "sample_rate": 16000}
    out = mlx_stt.transcribe_worker("/tmp/a.wav", "/models/tiny", decode)

    assert seen["path_or_hf_repo"] == "/models/tiny"
    assert seen["language"] is None  # auto-detect
    assert seen["temperature"] == tuple(mlx_stt.DEFAULT_DECODE["temperature"])
    assert "sample_rate" not in seen
    assert out["segments"][0]["end"] == 1.5
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_waterfall_records_mlx_provider_and_attempts(tmp_path, monkeypatch) -> None:
    _patch_mlx(monkeypatch, tmp_path)
    monkeypatch.setattr(
        transcript_api,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(transcript_api.TranscriptsDisabled(VIDEO_ID)),
    )
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(ytdlp_transcript.YtDlpNoTranscript("none")),
    )
    result = transcript_acquisition.acquire_transcript(VIDEO_ID)
    assert result.provider == "mlx-whisper"
    assert result.method == "local_stt"
    assert [a.provider for a in result.attempts] == ["youtube-transcript-api", "yt-dlp", "mlx-whisper"]
    assert result.details["model"]["revision"] == REPO_COMMIT


def test_waterfall_records_unavailable_mlx_attempt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(mlx_stt.ENV_MODEL, str(_mlx_model_dir(tmp_path)))
    monkeypatch.setattr(mlx_stt, "is_supported_platform", lambda: False)
    monkeypatch.setattr(
        transcript_api,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(transcript_api.TranscriptsDisabled(VIDEO_ID)),
    )
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(ytdlp_transcript.YtDlpNoTranscript("none")),
    )
    with pytest.raises(transcript_acquisition.TranscriptAcquisitionFailed) as info:
        transcript_acquisition.acquire_transcript(VIDEO_ID)
    last = info.value.attempts[-1]
    assert (last.provider, last.method, last.outcome, last.detail_code) == (
        "mlx-whisper", "local_stt", "unavailable", "local_stt_unavailable",
    )


# --------------------------------------------------------------------------- #
# Acceptance: a captionless video enters corpus search via local STT


def _configure_storage(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "youtube-mcp"
    monkeypatch.setattr(cache, "CACHE_DIR", root)
    monkeypatch.setattr(cache, "CACHE_PATH", root / "v2.sqlite")
    monkeypatch.setattr(skeleton, "CACHE_DIR", root)
    monkeypatch.setattr(skeleton, "SKELETON_DIR", root / "skeletons")
    monkeypatch.setattr(skeleton, "VECTOR_DIR", root / "vectors")
    monkeypatch.setattr(skeleton, "MODEL_DIR", root / "models")
    monkeypatch.setattr(corpus_index, "VECTOR_DIR", root / "vectors")
    monkeypatch.setattr(corpus_index, "INDEX_ROOT", root / "vectors" / "corpus")


def test_captionless_video_enters_corpus_search_via_local_stt(tmp_path, monkeypatch) -> None:
    _configure_storage(tmp_path, monkeypatch)
    audio = _patch_mlx(monkeypatch, tmp_path)
    monkeypatch.setattr(
        transcript_api,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(transcript_api.TranscriptsDisabled(VIDEO_ID)),
    )
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(ytdlp_transcript.YtDlpNoTranscript("no captions")),
    )
    cache.put_video_meta(VIDEO_ID, {"id": VIDEO_ID, "duration_s": 60})
    skeleton.save_skeleton(
        {
            "handle": HANDLE,
            "target": "topic",
            "value": "captionless bench",
            "built_at": "2026-09-01T00:00:00Z",
            "expired_at": None,
            "source": "fixture",
            "videos": [{"id": VIDEO_ID, "title": "Bench supply teardown", "channel": "Lab"}],
        }
    )

    hydrated = corpus_hydrate(HANDLE)
    assert hydrated["data"] is not None, hydrated
    assert hydrated["data"]["failed"] == 0

    env = corpus_search(HANDLE, "LM5176-Q1 controller", top_k=3, semantic="off")
    assert env["data"] is not None, env
    hit = env["data"]["hits"][0]
    assert hit["video_id"] == VIDEO_ID
    assert hit["anchor"]["start_s"] == 0.0 and hit["anchor"]["basis"] == "exact"
    assert hit["scores"]["exact_match"] is True
    transcript = hit["transcript"]
    assert transcript["validation_status"] == "pass"
    acquisition = transcript["acquisition"]
    assert acquisition["provider"] == "mlx-whisper"
    assert acquisition["method"] == "local_stt"
    assert acquisition["provider_class"] == "local"
    assert acquisition["details"]["model"]["revision"] == REPO_COMMIT
    assert acquisition["details"]["audio"]["sha256"] == stt_common.sha256_file(audio)
    assert hit["corpus_revision"] == HANDLE
    assert hit["index_revision"].startswith("idx1-")

    only_stt = corpus_search(
        HANDLE, "LM5176-Q1", semantic="off", filters={"acquisition_methods": ["local_stt"]}
    )
    assert [h["video_id"] for h in only_stt["data"]["hits"]] == [VIDEO_ID]
    captions_only = corpus_search(
        HANDLE, "LM5176-Q1", semantic="off", filters={"acquisition_methods": ["captions"]}
    )
    assert captions_only["data"]["hits"] == []
    assert str(tmp_path) not in json.dumps(env)


def test_whisper_cpp_shares_provenance_schema_and_language_hint(tmp_path, monkeypatch) -> None:
    import subprocess

    model = tmp_path / "ggml-small.bin"
    model.write_bytes(b"ggml-fixture")
    audio = _wav(tmp_path / "audio" / "source.wav", seconds=0.25)
    monkeypatch.setattr(stt_common, "acquire_audio", lambda _video_id: audio)
    monkeypatch.setattr(whisper_cpp, "_engine_version", lambda _binary: "whisper.cpp 1.8.0")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        out = Path(command[command.index("-of") + 1]).with_suffix(".json")
        out.write_text(json.dumps({
            "result": {"language": "de"},
            "transcription": [
                {"offsets": {"from": 0, "to": 1500}, "text": "Guten Tag"},
                {"offsets": {"from": 1500, "to": 4000}, "text": "zusammen"},
            ],
        }), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    settings = whisper_cpp.WhisperCppSettings(binary="whisper-cli", model=model, timeout_s=5, language="de")
    result = whisper_cpp.transcribe_video(VIDEO_ID, settings=settings)

    assert commands[0][commands[0].index("-l") + 1] == "de"
    prov = result.provenance
    assert prov["schema"] == "youtube-mcp.local-stt/1"
    assert prov["model"]["format"] == "ggml" and prov["model"]["revision"] is None
    assert prov["decode"] == {"language": "de", "task": "transcribe", "output": "json", "sample_rate": 16000}
    assert prov["audio"]["duration_s"] == 0.25
    assert prov["segments"]["count"] == 2 and prov["segments"]["last_end_s"] == 4.0
    assert str(tmp_path) not in json.dumps(prov)
