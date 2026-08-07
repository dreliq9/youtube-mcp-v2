from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from youtube_mcp_v2 import corpus_index, skeleton, visual_embeddings, visual_index


HANDLE = "topic-visual-evidence-20260806-112233"
VID_A = "jNQXAC9IVRw"
VID_B = "dQw4w9WgXcQ"


def _configure_storage(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "youtube-mcp"
    frames = root / "frames"
    vectors = root / "vectors"
    monkeypatch.setattr(skeleton, "CACHE_DIR", root)
    monkeypatch.setattr(skeleton, "SKELETON_DIR", root / "skeletons")
    monkeypatch.setattr(skeleton, "VECTOR_DIR", vectors)
    monkeypatch.setattr(skeleton, "MODEL_DIR", root / "models")
    monkeypatch.setattr(visual_index, "FRAMES_DIR", frames)
    monkeypatch.setattr(visual_index, "VECTOR_DIR", vectors)
    monkeypatch.setattr(visual_index, "VISUAL_INDEX_ROOT", vectors / "visual")
    monkeypatch.setattr(
        visual_index, "VISUAL_ARTIFACT_ROOT", vectors / "visual-artifacts"
    )
    return root


def _save_corpus() -> None:
    skeleton.save_skeleton(
        {
            "handle": HANDLE,
            "target": "topic",
            "value": "visual evidence",
            "built_at": "2026-08-06T11:22:33Z",
            "expired_at": None,
            "source": "fixture",
            "videos": [
                {"id": VID_A, "title": "Power Scope", "channel": "Lab A"},
                {"id": VID_B, "title": "Other Frames", "channel": "Lab B"},
            ],
        }
    )


def _write_frame(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _sheet(video_dir: Path, frames: list[tuple[float, Path]]) -> Path:
    manifest = video_dir / "sheet_2_2x1_640x360.json"
    manifest.write_text(
        json.dumps(
            {
                "frame_timestamps": [timestamp for timestamp, _path in frames],
                "frame_paths": [str(path) for _timestamp, path in frames],
            }
        ),
        encoding="utf-8",
    )
    return manifest


@dataclass
class FakeVisualBackend:
    text_model_id: str = "fixture/clip-text-v1"
    image_model_id: str = "fixture/clip-image-v1"

    def _vector_for_bytes(self, payload: bytes) -> list[float]:
        if b"scope-chart" in payload:
            return [1.0, 0.0, 0.0]
        if b"battery-pack" in payload:
            return [0.0, 1.0, 0.0]
        if b"replacement-frame" in payload:
            return [0.0, 0.0, 1.0]
        return [0.3, 0.3, 0.4]

    def embed_images(self, paths):
        return [self._vector_for_bytes(Path(path).read_bytes()) for path in paths]

    def embed_query(self, text: str) -> list[float]:
        lowered = text.lower()
        if "oscilloscope" in lowered or "transient chart" in lowered:
            return [1.0, 0.0, 0.0]
        if "battery" in lowered:
            return [0.0, 1.0, 0.0]
        if "replacement" in lowered:
            return [0.0, 0.0, 1.0]
        return [0.3, 0.3, 0.4]


def _fixture(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    root = _configure_storage(tmp_path, monkeypatch)
    _save_corpus()
    video_a = root / "frames" / VID_A
    video_b = root / "frames" / VID_B
    a0 = _write_frame(
        video_a / "sheet_2_2x1_640x360_00.png", b"PNG scope-chart fixture"
    )
    a1 = _write_frame(
        video_a / "sheet_2_2x1_640x360_01.png", b"PNG battery-pack fixture"
    )
    _sheet(video_a, [(12.5, a0), (44.0, a1)])
    b0 = _write_frame(
        video_b / "single_9000ms_640x360.png", b"PNG generic mechanical fixture"
    )
    return a0, a1, b0


def test_prepare_snapshots_content_addressed_pixels_and_reports_coverage(
    tmp_path, monkeypatch
) -> None:
    a0, a1, _b0 = _fixture(tmp_path, monkeypatch)
    backend = FakeVisualBackend()

    first = visual_index.prepare_visual_index(HANDLE, backend=backend)
    second = visual_index.prepare_visual_index(HANDLE, backend=backend)

    assert first.visual_index_revision.startswith("vidx1-")
    assert first.visual_index_revision == second.visual_index_revision
    assert first.cached is False
    assert second.cached is True
    assert first.total_videos == 2
    assert first.indexed_videos == 2
    assert first.missing_videos == []
    assert first.frame_count == 3

    for source in (a0, a1):
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        frozen = visual_index._artifact_path(sha, "png")
        assert frozen.is_file()
        assert frozen.read_bytes() == source.read_bytes()


def test_visual_semantic_query_returns_timestamped_frame_without_transcript(
    tmp_path, monkeypatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    backend = FakeVisualBackend()
    prepared = visual_index.prepare_visual_index(HANDLE, backend=backend)

    result = visual_index.search_visual_index(
        HANDLE,
        "oscilloscope showing a transient chart",
        backend=backend,
        visual_index_revision=prepared.visual_index_revision,
    )

    assert result["hits"][0]["video_id"] == VID_A
    assert result["hits"][0]["timestamp_s"] == pytest.approx(12.5)
    assert "t=12s" in result["hits"][0]["url"]
    assert result["hits"][0]["score"] == pytest.approx(1.0)
    assert len(result["hits"][0]["frame_sha256"]) == 64
    assert Path(result["hits"][0]["artifact_path"]).is_file()


def test_old_visual_index_keeps_old_pixels_after_frame_cache_rebuild(
    tmp_path, monkeypatch
) -> None:
    a0, _a1, _b0 = _fixture(tmp_path, monkeypatch)
    backend = FakeVisualBackend()
    old = visual_index.prepare_visual_index(HANDLE, backend=backend)
    old_sha = hashlib.sha256(a0.read_bytes()).hexdigest()
    old_artifact = visual_index._artifact_path(old_sha, "png")

    a0.write_bytes(b"PNG replacement-frame fixture")
    new = visual_index.prepare_visual_index(HANDLE, backend=backend)
    assert new.visual_index_revision != old.visual_index_revision
    assert old_artifact.read_bytes() == b"PNG scope-chart fixture"

    old_result = visual_index.search_visual_index(
        HANDLE,
        "oscilloscope chart",
        backend=backend,
        visual_index_revision=old.visual_index_revision,
    )
    new_result = visual_index.search_visual_index(
        HANDLE,
        "replacement visual",
        backend=backend,
        visual_index_revision=new.visual_index_revision,
    )
    assert old_result["hits"][0]["frame_sha256"] == old_sha
    assert Path(old_result["hits"][0]["artifact_path"]).read_bytes() == (
        b"PNG scope-chart fixture"
    )
    assert new_result["hits"][0]["video_id"] == VID_A
    assert new_result["hits"][0]["timestamp_s"] == pytest.approx(12.5)


def test_manifest_cannot_snapshot_file_outside_managed_video_directory(
    tmp_path, monkeypatch
) -> None:
    root = _configure_storage(tmp_path, monkeypatch)
    _save_corpus()
    video_a = root / "frames" / VID_A
    video_a.mkdir(parents=True)
    outside = _write_frame(tmp_path / "outside-secret.png", b"SECRET")
    _sheet(video_a, [(1.0, outside), (2.0, outside)])

    backend = FakeVisualBackend()
    prepared = visual_index.prepare_visual_index(HANDLE, backend=backend)
    assert prepared.indexed_videos == 0
    assert set(prepared.missing_videos) == {VID_A, VID_B}
    assert prepared.frame_count == 0
    assert not any(
        path.read_bytes() == b"SECRET"
        for path in visual_index.VISUAL_ARTIFACT_ROOT.rglob("*")
        if path.is_file()
    )


def test_duplicate_manifests_do_not_duplicate_same_pixel_timestamp(
    tmp_path, monkeypatch
) -> None:
    a0, _a1, _b0 = _fixture(tmp_path, monkeypatch)
    video_dir = a0.parent
    duplicate = video_dir / "sheet_duplicate.json"
    duplicate.write_text(
        json.dumps(
            {"frame_timestamps": [12.5], "frame_paths": [str(a0)]}
        ),
        encoding="utf-8",
    )

    prepared = visual_index.prepare_visual_index(HANDLE, backend=FakeVisualBackend())
    # Original two manifest frames + one standalone frame in VID_B.
    assert prepared.frame_count == 3


def test_single_frame_filename_supplies_exact_timestamp(tmp_path, monkeypatch) -> None:
    _fixture(tmp_path, monkeypatch)
    prepared = visual_index.prepare_visual_index(HANDLE, backend=FakeVisualBackend())
    result = visual_index.search_visual_index(
        HANDLE,
        "generic mechanical object",
        backend=FakeVisualBackend(),
        top_k=10,
        visual_index_revision=prepared.visual_index_revision,
    )
    by_video = {hit["video_id"]: hit for hit in result["hits"]}
    assert by_video[VID_B]["timestamp_s"] == pytest.approx(9.0)
    assert by_video[VID_B]["source_kind"] == "single_frame"


def test_model_pair_identity_changes_visual_index_revision(tmp_path, monkeypatch) -> None:
    _fixture(tmp_path, monkeypatch)
    first = visual_index.prepare_visual_index(
        HANDLE,
        backend=FakeVisualBackend(
            text_model_id="fixture/text-a", image_model_id="fixture/image-a"
        ),
    )
    second = visual_index.prepare_visual_index(
        HANDLE,
        backend=FakeVisualBackend(
            text_model_id="fixture/text-b", image_model_id="fixture/image-b"
        ),
    )
    assert first.visual_index_revision != second.visual_index_revision


def test_pinned_visual_index_rejects_different_model_pair(tmp_path, monkeypatch) -> None:
    _fixture(tmp_path, monkeypatch)
    prepared = visual_index.prepare_visual_index(HANDLE, backend=FakeVisualBackend())
    with pytest.raises(visual_index.VisualIndexError, match="model identity"):
        visual_index.search_visual_index(
            HANDLE,
            "oscilloscope",
            backend=FakeVisualBackend(
                text_model_id="other/text", image_model_id="other/image"
            ),
            visual_index_revision=prepared.visual_index_revision,
        )


def test_corrupt_frozen_frame_artifact_is_detected_before_return(
    tmp_path, monkeypatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    backend = FakeVisualBackend()
    prepared = visual_index.prepare_visual_index(HANDLE, backend=backend)
    result = visual_index.search_visual_index(
        HANDLE,
        "oscilloscope",
        backend=backend,
        visual_index_revision=prepared.visual_index_revision,
    )
    artifact = Path(result["hits"][0]["artifact_path"])
    artifact.chmod(0o644)
    artifact.write_bytes(b"CORRUPTED")

    with pytest.raises(visual_index.VisualIndexError, match="integrity check"):
        visual_index.search_visual_index(
            HANDLE,
            "oscilloscope",
            backend=backend,
            visual_index_revision=prepared.visual_index_revision,
        )


def test_auto_visual_backend_never_upgrades_to_download(monkeypatch) -> None:
    calls = []

    class ProbeBackend:
        def __init__(
            self,
            *,
            text_model_id,
            image_model_id,
            local_files_only,
            cache_dir,
        ):
            calls.append(
                (text_model_id, image_model_id, local_files_only, cache_dir)
            )
            raise visual_embeddings.EmbeddingUnavailable("fixture models absent")

    monkeypatch.setattr(visual_embeddings, "FastEmbedClipBackend", ProbeBackend)
    assert visual_embeddings.resolve_visual_backend("auto") is None
    assert calls[0][2] is True

    with pytest.raises(visual_embeddings.EmbeddingUnavailable):
        visual_embeddings.resolve_visual_backend("required")
    assert calls[1][2] is False
