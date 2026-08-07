from __future__ import annotations

import base64
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import BlobResourceContents

from youtube_mcp_v2 import artifacts
from youtube_mcp_v2.adapters import audio_extract, frame_extract
from youtube_mcp_v2.server import mcp
from youtube_mcp_v2.tools.audio import audio_get
from youtube_mcp_v2.tools.frame import frame_get


VIDEO_ID = "jNQXAC9IVRw"


def _frame_file(tmp_path: Path, monkeypatch, *, name: str = "single_1000ms_640x360") -> Path:
    root = tmp_path / "frames"
    monkeypatch.setattr(artifacts, "FRAMES_DIR", root)
    path = root / VIDEO_ID / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\nfixture")
    return path


def _audio_file(tmp_path: Path, monkeypatch, *, name: str = "audio_full_22050hz_mono") -> Path:
    root = tmp_path / "audio"
    monkeypatch.setattr(artifacts, "AUDIO_DIR", root)
    path = root / VIDEO_ID / f"{name}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFFfixture-wave")
    return path


def test_reference_for_managed_frame_path(tmp_path, monkeypatch) -> None:
    path = _frame_file(tmp_path, monkeypatch)
    ref = artifacts.reference_for_path(path, kind="frame", video_id=VIDEO_ID)
    assert ref.portable is True
    assert ref.mime_type == "image/png"
    assert ref.uri == (
        f"youtube-mcp://artifact/frame/png/{VIDEO_ID}/single_1000ms_640x360"
    )
    assert ref.size_bytes == path.stat().st_size


def test_reference_rejects_file_outside_managed_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(artifacts, "FRAMES_DIR", tmp_path / "managed")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    with pytest.raises(artifacts.ArtifactError, match="outside the managed"):
        artifacts.reference_for_path(outside, kind="frame", video_id=VIDEO_ID)


def test_resource_reader_rejects_traversal_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(artifacts, "FRAMES_DIR", tmp_path / "frames")
    with pytest.raises(artifacts.ArtifactError, match="invalid artifact name"):
        artifacts.read_resource(
            kind="frame",
            ext="png",
            video_id=VIDEO_ID,
            name="../secret",
        )


def test_resource_size_limit_makes_large_artifact_nonportable(tmp_path, monkeypatch) -> None:
    path = _audio_file(tmp_path, monkeypatch)
    monkeypatch.setenv("YOUTUBE_MCP_MAX_RESOURCE_BYTES", "4")

    ref = artifacts.reference_for_path(path, kind="audio", video_id=VIDEO_ID)
    assert ref.portable is False
    assert ref.uri is None

    with pytest.raises(artifacts.ArtifactTooLarge, match="portable resource limit"):
        artifacts.read_resource(
            kind="audio",
            ext="wav",
            video_id=VIDEO_ID,
            name=path.stem,
        )


def test_invalid_resource_limit_falls_back_to_safe_default(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_MCP_MAX_RESOURCE_BYTES", "not-an-int")
    assert artifacts.max_resource_bytes() == artifacts.DEFAULT_MAX_RESOURCE_BYTES
    monkeypatch.setenv("YOUTUBE_MCP_MAX_RESOURCE_BYTES", "-1")
    assert artifacts.max_resource_bytes() == artifacts.DEFAULT_MAX_RESOURCE_BYTES


def test_frame_tool_returns_resource_reference(tmp_path, monkeypatch) -> None:
    path = _frame_file(tmp_path, monkeypatch)
    monkeypatch.setattr(
        frame_extract,
        "extract_single_frame",
        lambda *_a, **_k: frame_extract.SingleFrameResult(
            path=str(path), timestamp_s=1.0, cached=False
        ),
    )

    env = frame_get(VIDEO_ID, mode="single", timestamp_s=1.0, size="640x360")
    assert env["error"] is None
    assert env["data"]["path"] == str(path)
    assert env["data"]["resource_portable"] is True
    assert env["data"]["resource_uri"].startswith("youtube-mcp://artifact/frame/png/")


def test_audio_tool_guides_remote_caller_to_clip_when_resource_too_large(
    tmp_path, monkeypatch
) -> None:
    path = _audio_file(tmp_path, monkeypatch)
    monkeypatch.setenv("YOUTUBE_MCP_MAX_RESOURCE_BYTES", "4")
    monkeypatch.setattr(
        audio_extract,
        "extract_audio",
        lambda *_a, **_k: audio_extract.AudioResult(
            path=str(path),
            format="wav",
            sample_rate=22050,
            mono=True,
            start_s=None,
            end_s=None,
            cached=False,
        ),
    )

    env = audio_get(VIDEO_ID)
    assert env["error"] is None
    assert env["data"]["resource_portable"] is False
    assert env["data"]["resource_uri"] is None
    assert any("shorter start/end clip" in warning for warning in env["warnings"])


@pytest.mark.asyncio
async def test_binary_resource_round_trip_through_in_memory_mcp_client(
    tmp_path, monkeypatch
) -> None:
    path = _frame_file(tmp_path, monkeypatch, name="roundtrip")
    payload = path.read_bytes()
    uri = f"youtube-mcp://artifact/frame/png/{VIDEO_ID}/roundtrip"

    async with Client(mcp) as client:
        result = await client.read_resource(uri)

    assert len(result.contents) == 1
    content = result.contents[0]
    assert isinstance(content, BlobResourceContents)
    assert content.mime_type == "image/png"
    assert base64.b64decode(content.blob) == payload
