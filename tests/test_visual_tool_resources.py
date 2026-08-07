from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from youtube_mcp_v2 import skeleton, visual_embeddings, visual_index, visual_resources
from youtube_mcp_v2.tools.visual import corpus_visual_search


HANDLE = "topic-visual-tool-20260806-123456"
VIDEO_ID = "jNQXAC9IVRw"


def _configure(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "youtube-mcp"
    vectors = root / "vectors"
    monkeypatch.setattr(skeleton, "CACHE_DIR", root)
    monkeypatch.setattr(skeleton, "SKELETON_DIR", root / "skeletons")
    monkeypatch.setattr(skeleton, "VECTOR_DIR", vectors)
    monkeypatch.setattr(skeleton, "MODEL_DIR", root / "models")
    monkeypatch.setattr(visual_index, "FRAMES_DIR", root / "frames")
    monkeypatch.setattr(visual_index, "VECTOR_DIR", vectors)
    monkeypatch.setattr(visual_index, "VISUAL_INDEX_ROOT", vectors / "visual")
    monkeypatch.setattr(
        visual_index, "VISUAL_ARTIFACT_ROOT", vectors / "visual-artifacts"
    )
    skeleton.save_skeleton(
        {
            "handle": HANDLE,
            "target": "topic",
            "value": "visual tool fixture",
            "built_at": "2026-08-06T12:34:56Z",
            "expired_at": None,
            "source": "fixture",
            "videos": [{"id": VIDEO_ID, "title": "Scope", "channel": "Lab"}],
        }
    )
    video_dir = root / "frames" / VIDEO_ID
    video_dir.mkdir(parents=True)
    frame = video_dir / "single_15000ms_640x360.png"
    frame.write_bytes(b"PNG red-oscilloscope-trace fixture")
    return frame


@dataclass
class Backend:
    text_model_id: str = "fixture/text"
    image_model_id: str = "fixture/image"

    def embed_images(self, paths):
        return [[1.0, 0.0] for _path in paths]

    def embed_query(self, text):
        return [1.0, 0.0]


def test_tool_auto_prepare_returns_portable_content_addressed_resource(
    tmp_path, monkeypatch
) -> None:
    frame = _configure(tmp_path, monkeypatch)
    backend = Backend()
    monkeypatch.setattr(
        visual_embeddings,
        "resolve_visual_backend",
        lambda mode, **_kwargs: backend,
    )

    env = corpus_visual_search(
        HANDLE,
        "red oscilloscope trace",
        visual="required",
        auto_prepare=True,
    )
    assert env["error"] is None
    hit = env["data"]["hits"][0]
    expected_sha = hashlib.sha256(frame.read_bytes()).hexdigest()
    assert hit["frame_sha256"] == expected_sha
    assert hit["artifact_ext"] == "png"
    assert hit["resource_uri"] == (
        f"youtube-mcp://evidence/frame/png/{expected_sha}"
    )
    assert hit["resource_mime_type"] == "image/png"
    assert hit["resource_portable"] is True
    assert visual_resources.read(expected_sha, "png") == frame.read_bytes()
    assert "red-oscilloscope" not in json.dumps(env)  # pixels are a resource, not tool text


def test_visual_resource_limit_marks_tool_hit_nonportable(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    backend = Backend()
    monkeypatch.setattr(
        visual_embeddings,
        "resolve_visual_backend",
        lambda mode, **_kwargs: backend,
    )
    monkeypatch.setenv("YOUTUBE_MCP_MAX_VISUAL_RESOURCE_BYTES", "4")

    env = corpus_visual_search(HANDLE, "scope", visual="required")
    assert env["error"] is None
    hit = env["data"]["hits"][0]
    assert hit["resource_portable"] is False
    assert any("portable MCP resource limit" in warning for warning in env["warnings"])
    with pytest.raises(visual_resources.VisualResourceTooLarge):
        visual_resources.read(hit["frame_sha256"], hit["artifact_ext"])


def test_visual_resource_rejects_invalid_sha_without_filesystem_lookup() -> None:
    with pytest.raises(visual_resources.VisualResourceError, match="SHA-256"):
        visual_resources.read("../secret", "png")


def test_auto_visual_mode_returns_recoverable_unavailable_without_download(
    tmp_path, monkeypatch
) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        visual_embeddings,
        "resolve_visual_backend",
        lambda *_a, **_k: None,
    )

    env = corpus_visual_search(HANDLE, "scope", visual="auto")
    assert env["error"]["code"] == "visual_unavailable"
    assert env["error"]["recoverable"] is True


def test_no_auto_prepare_fails_before_embedding_when_index_absent(
    tmp_path, monkeypatch
) -> None:
    _configure(tmp_path, monkeypatch)
    backend = Backend()
    monkeypatch.setattr(
        visual_embeddings,
        "resolve_visual_backend",
        lambda *_a, **_k: backend,
    )

    env = corpus_visual_search(
        HANDLE,
        "scope",
        visual="required",
        auto_prepare=False,
    )
    assert env["error"]["code"] == "visual_index_missing"
