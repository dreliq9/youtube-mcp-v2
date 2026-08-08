from __future__ import annotations

from pathlib import Path

from youtube_mcp_v2 import skeleton
from youtube_mcp_v2.tools.corpus_compose import corpus_compose


VID_A = "jNQXAC9IVRw"
VID_B = "dQw4w9WgXcQ"
VID_C = "M7lc1UVf-VE"


def _configure(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "youtube-mcp"
    monkeypatch.setattr(skeleton, "CACHE_DIR", root)
    monkeypatch.setattr(skeleton, "SKELETON_DIR", root / "skeletons")
    monkeypatch.setattr(skeleton, "VECTOR_DIR", root / "vectors")
    monkeypatch.setattr(skeleton, "MODEL_DIR", root / "models")


def _save(handle: str, target: str, label: str, videos: list[dict]) -> None:
    skeleton.save_skeleton(
        {
            "handle": handle,
            "target": target,
            "value": label,
            "built_at": "2026-08-07T00:00:00Z",
            "expired_at": None,
            "source": "fixture",
            "channel": None,
            "videos": videos,
        }
    )


def test_collection_handle_is_first_class(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    handle = skeleton.make_handle("collection", "wisdom across domains")
    assert handle.startswith("set-wisdom-across-domains-")
    assert skeleton.is_valid_handle(handle)


def test_compose_arbitrary_videos_without_network(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)

    env = corpus_compose(
        "frontier thinking",
        videos=[
            VID_A,
            f"https://www.youtube.com/watch?v={VID_B}",
            VID_A,
        ],
    )

    assert env["error"] is None
    assert env["data"]["target"] == "collection"
    assert env["data"]["count"] == 2
    assert env["data"]["duplicate_video_ids_ignored"] == [VID_A]

    frozen = skeleton.load_skeleton(env["data"]["handle"])
    assert frozen["target"] == "collection"
    assert frozen["source"] == "compose"
    assert [video["id"] for video in frozen["videos"]] == [VID_A, VID_B]
    assert all(video["url"].startswith("https://www.youtube.com/watch?v=") for video in frozen["videos"])


def test_compose_merges_multiple_domains_and_preserves_first_source_order(
    tmp_path, monkeypatch
) -> None:
    _configure(tmp_path, monkeypatch)
    h1 = "topic-history-20260807-010101"
    h2 = "topic-cooking-20260807-010102"
    _save(
        h1,
        "topic",
        "history",
        [
            {"id": VID_A, "title": "History A", "channel": "Historian"},
            {"id": VID_B, "title": "Shared", "channel": "Archive"},
        ],
    )
    _save(
        h2,
        "topic",
        "cooking",
        [
            {"id": VID_B, "title": "Duplicate later copy", "channel": "Chef"},
            {"id": VID_C, "title": "Cooking C", "channel": "Chef"},
        ],
    )

    env = corpus_compose(
        "meaning and craft",
        include_handles=[h1, h2],
    )

    assert env["error"] is None
    frozen = skeleton.load_skeleton(env["data"]["handle"])
    assert [video["id"] for video in frozen["videos"]] == [VID_A, VID_B, VID_C]
    assert frozen["videos"][1]["title"] == "Shared"
    assert frozen["composition"]["source_handles"] == [h1, h2]
    assert VID_B in frozen["composition"]["duplicate_video_ids_ignored"]


def test_derive_from_existing_collection_with_add_and_remove(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    base = "set-original-20260807-010103"
    _save(
        base,
        "collection",
        "original",
        [
            {"id": VID_A, "title": "A"},
            {"id": VID_B, "title": "B"},
        ],
    )

    env = corpus_compose(
        "revised",
        base_handle=base,
        videos=[VID_C],
        remove=[VID_A],
    )

    assert env["error"] is None
    assert env["data"]["parent_handle"] == base
    assert env["data"]["added_video_ids"] == [VID_C]
    assert env["data"]["removed_video_ids"] == [VID_A]
    frozen = skeleton.load_skeleton(env["data"]["handle"])
    assert [video["id"] for video in frozen["videos"]] == [VID_B, VID_C]
    assert skeleton.load_skeleton(base)["videos"][0]["id"] == VID_A


def test_compose_rejects_bad_video_reference_before_writing(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    env = corpus_compose("bad input", videos=["https://example.com/not-youtube"])
    assert env["error"]["code"] == "bad_video"
    assert not list(skeleton.SKELETON_DIR.glob("*.json")) if skeleton.SKELETON_DIR.exists() else True


def test_compose_rejects_empty_result_after_removal(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    env = corpus_compose("empty", videos=[VID_A], remove=[VID_A])
    assert env["error"]["code"] == "empty_result"
