from __future__ import annotations

import pytest

from youtube_mcp_v2 import skeleton
from youtube_mcp_v2.tools.skeleton_tools import skeleton_diff


def _redirect_dirs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(skeleton, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(skeleton, "SKELETON_DIR", tmp_path / "skeletons")
    monkeypatch.setattr(skeleton, "VECTOR_DIR", tmp_path / "vectors")
    monkeypatch.setattr(skeleton, "MODEL_DIR", tmp_path / "models")


def _save(
    handle: str,
    *,
    value: str = "battery research",
    source: str = "scrape",
    videos: list[dict] | None = None,
) -> None:
    skeleton.save_skeleton(
        {
            "handle": handle,
            "target": "topic",
            "value": value,
            "built_at": "2026-08-06T12:00:00Z",
            "source": source,
            "expired_at": None,
            "channel": None,
            "videos": videos or [],
        }
    )


def test_diff_reports_add_remove_change_and_unchanged(tmp_path, monkeypatch) -> None:
    _redirect_dirs(tmp_path, monkeypatch)
    base = "topic-battery-research-20260806-120000"
    head = "topic-battery-research-20260806-130000"

    _save(
        base,
        videos=[
            {"id": "aaaaaaaaaaa", "title": "unchanged", "view_count": 10},
            {"id": "bbbbbbbbbbb", "title": "changed", "view_count": 10},
            {"id": "ccccccccccc", "title": "removed", "view_count": 10},
        ],
    )
    _save(
        head,
        source="api",
        videos=[
            {"id": "aaaaaaaaaaa", "title": "unchanged", "view_count": 10},
            {"id": "bbbbbbbbbbb", "title": "changed", "view_count": 11},
            {"id": "ddddddddddd", "title": "added", "view_count": 1},
        ],
    )

    env = skeleton_diff(base, head)
    assert env["error"] is None
    data = env["data"]

    assert data["source_changed"] is True
    assert data["counts"] == {
        "base": 3,
        "head": 3,
        "added": 1,
        "removed": 1,
        "changed": 1,
        "unchanged": 1,
    }
    assert [row["id"] for row in data["added"]] == ["ddddddddddd"]
    assert [row["id"] for row in data["removed"]] == ["ccccccccccc"]
    assert data["changed"][0]["id"] == "bbbbbbbbbbb"
    assert data["changed"][0]["changed_fields"] == ["view_count"]
    assert data["changed"][0]["before"]["view_count"] == 10
    assert data["changed"][0]["after"]["view_count"] == 11


def test_diff_rejects_unrelated_scopes(tmp_path, monkeypatch) -> None:
    _redirect_dirs(tmp_path, monkeypatch)
    base = "topic-battery-research-20260806-120000"
    head = "topic-other-research-20260806-130000"
    _save(base, value="battery research")
    _save(head, value="other research")

    env = skeleton_diff(base, head)
    assert env["data"] is None
    assert env["error"]["code"] == "scope_mismatch"
    assert env["error"]["recoverable"] is False


def test_diff_rejects_duplicate_video_ids(tmp_path, monkeypatch) -> None:
    _redirect_dirs(tmp_path, monkeypatch)
    base = "topic-battery-research-20260806-120000"
    head = "topic-battery-research-20260806-130000"
    _save(
        base,
        videos=[
            {"id": "aaaaaaaaaaa", "title": "one"},
            {"id": "aaaaaaaaaaa", "title": "duplicate"},
        ],
    )
    _save(head, videos=[])

    env = skeleton_diff(base, head)
    assert env["error"]["code"] == "skeleton_diff_invalid"


def test_diff_is_based_on_frozen_payload_not_current_cache(tmp_path, monkeypatch) -> None:
    _redirect_dirs(tmp_path, monkeypatch)
    base = "topic-battery-research-20260806-120000"
    head = "topic-battery-research-20260806-130000"
    captured = [{"id": "aaaaaaaaaaa", "title": "captured", "view_count": None}]
    _save(base, videos=captured)
    _save(head, videos=captured)

    # The diff implementation must never call current enrichment. If it does,
    # this turns the test into a deterministic failure.
    monkeypatch.setattr(
        skeleton,
        "enrich_videos_with_meta",
        lambda *_args, **_kwargs: pytest.fail("diff consulted mutable enrichment"),
    )

    env = skeleton_diff(base, head)
    assert env["error"] is None
    assert env["data"]["counts"]["unchanged"] == 1
    assert env["data"]["counts"]["changed"] == 0
