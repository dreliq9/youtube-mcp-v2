from __future__ import annotations

from pathlib import Path

from youtube_mcp_v2 import edit_plan
from youtube_mcp_v2.adapters import video_clip
from youtube_mcp_v2.tools import clip_plan as clip_plan_tool
from youtube_mcp_v2.tools.materialize import media_materialize


VID_A = "jNQXAC9IVRw"
VID_B = "dQw4w9WgXcQ"


def _configure(tmp_path: Path, monkeypatch) -> None:
    plans = tmp_path / "edit-plans"
    clips = tmp_path / "editor-clips"
    monkeypatch.setattr(edit_plan, "EDIT_PLAN_DIR", plans)
    monkeypatch.setattr(video_clip, "EDIT_PLAN_DIR", plans)
    monkeypatch.setattr(video_clip, "EDITOR_CLIP_DIR", clips)


def _evidence_response() -> dict:
    return {
        "data": {
            "corpus_revision": "set-cross-domain-20260807-010101",
            "query": "frontier thinking",
            "transcript_index_revision": "idx-text",
            "visual_index_revision": "vidx-vision",
            "hits": [
                {
                    "rank": 1,
                    "evidence_type": "cross_modal",
                    "video_id": VID_A,
                    "title": "Historian on frontiers",
                    "channel": "History Channel",
                    "start_s": 100.0,
                    "end_s": 106.0,
                    "transcript": {"excerpt": "A frontier changes institutions."},
                    "frame": {"timestamp_s": 104.0, "resource_uri": "youtube-mcp://evidence/frame/png/abc"},
                },
                {
                    "rank": 2,
                    "evidence_type": "transcript",
                    "video_id": VID_A,
                    "title": "Historian on frontiers",
                    "channel": "History Channel",
                    "start_s": 102.0,
                    "end_s": 108.0,
                    "transcript": {"excerpt": "Overlapping evidence should dedupe."},
                    "frame": None,
                },
                {
                    "rank": 3,
                    "evidence_type": "transcript",
                    "video_id": VID_B,
                    "title": "Artist interview",
                    "channel": "Studio",
                    "start_s": 40.0,
                    "end_s": 45.0,
                    "transcript": {"excerpt": "Creative work also has frontiers."},
                    "frame": None,
                },
            ],
        },
        "fetched_at": "2026-08-07T00:00:00Z",
        "source": "cache",
        "cache_age_s": 0,
        "validated": True,
        "warnings": [],
        "error": None,
    }


def test_corpus_clip_plan_is_immutable_editor_neutral_artifact(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        clip_plan_tool,
        "corpus_evidence_search",
        lambda *args, **kwargs: _evidence_response(),
    )

    env = clip_plan_tool.corpus_clip_plan(
        "set-cross-domain-20260807-010101",
        "frontier thinking",
        target_duration_s=30,
        max_clips=4,
        context_before_s=2,
        context_after_s=2,
    )

    assert env["error"] is None
    data = env["data"]
    assert data["schema"] == edit_plan.PLAN_SCHEMA
    assert data["plan_revision"].startswith("cp-")
    assert data["clip_count"] == 2
    assert data["clips"][0]["video_id"] == VID_A
    assert data["clips"][0]["start_s"] == 98.0
    assert data["clips"][0]["end_s"] == 108.0
    assert data["clips"][1]["video_id"] == VID_B
    assert Path(data["plan_path"]).exists()
    loaded = edit_plan.load_plan(data["plan_revision"])
    assert loaded["clips"] == data["clips"]
    assert data["materialization"]["tool"] == "media.materialize"


def test_clip_plan_never_materializes_media(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        clip_plan_tool,
        "corpus_evidence_search",
        lambda *args, **kwargs: _evidence_response(),
    )
    env = clip_plan_tool.corpus_clip_plan(
        "set-cross-domain-20260807-010101",
        "frontier thinking",
    )
    assert env["error"] is None
    assert not (tmp_path / "editor-clips").exists()


def test_materialize_downloads_each_source_once_and_emits_editor_contract(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    plan = edit_plan.build_plan(
        _evidence_response(),
        handle="set-cross-domain-20260807-010101",
        query="frontier thinking",
        target_duration_s=30,
        max_clips=4,
        min_clip_duration_s=4,
        max_clip_duration_s=20,
        context_before_s=2,
        context_after_s=2,
    )
    edit_plan.save_plan(plan)

    downloads: list[str] = []

    def fake_download(video_id: str, dest_dir: Path, max_height: int) -> Path:
        downloads.append(video_id)
        path = dest_dir / f"{video_id}.mp4"
        path.write_bytes(b"source")
        return path

    def fake_extract(source: Path, output: Path, start_s: float, end_s: float) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(f"{source.name}:{start_s:.3f}:{end_s:.3f}".encode())

    monkeypatch.setattr(video_clip, "_download_video", fake_download)
    monkeypatch.setattr(video_clip, "_extract_clip", fake_extract)

    env = media_materialize(plan["plan_revision"], max_height=720)
    assert env["error"] is None
    manifest = env["data"]
    assert manifest["schema"] == video_clip.MATERIALIZED_SCHEMA
    assert manifest["complete"] is True
    assert len(manifest["assets"]) == 2
    assert downloads == [VID_A, VID_B]
    assert all(Path(asset["path"]).exists() for asset in manifest["assets"])
    assert all(asset["sha256"] for asset in manifest["assets"])
    assert manifest["editor_contract"]["asset_semantics"].startswith("each path is already trimmed")
    assert Path(manifest["manifest_path"]).exists()

    downloads.clear()
    env_cached = media_materialize(plan["plan_revision"], max_height=720)
    assert env_cached["error"] is None
    assert downloads == []
    assert all(asset["cached"] is True for asset in env_cached["data"]["assets"])


def test_partial_materialization_by_clip_id(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    plan = edit_plan.build_plan(
        _evidence_response(),
        handle="set-cross-domain-20260807-010101",
        query="frontier thinking",
        target_duration_s=30,
        max_clips=4,
        min_clip_duration_s=4,
        max_clip_duration_s=20,
        context_before_s=2,
        context_after_s=2,
    )
    edit_plan.save_plan(plan)

    def fake_download(video_id: str, dest_dir: Path, max_height: int) -> Path:
        path = dest_dir / f"{video_id}.mp4"
        path.write_bytes(b"source")
        return path

    def fake_extract(source: Path, output: Path, start_s: float, end_s: float) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"clip")

    monkeypatch.setattr(video_clip, "_download_video", fake_download)
    monkeypatch.setattr(video_clip, "_extract_clip", fake_extract)

    env = media_materialize(
        plan["plan_revision"],
        clip_ids=[plan["clips"][1]["clip_id"]],
    )
    assert env["error"] is None
    assert env["data"]["complete"] is False
    assert env["data"]["materialized_clip_count"] == 1
    assert env["data"]["assets"][0]["clip_id"] == plan["clips"][1]["clip_id"]


def test_materialize_rejects_unknown_plan(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    env = media_materialize("cp-000000000000000000000000")
    assert env["error"]["code"] == "clip_plan_not_found"
