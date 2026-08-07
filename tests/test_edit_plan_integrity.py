from __future__ import annotations

import json
from pathlib import Path

import pytest

from youtube_mcp_v2 import edit_plan


VID = "jNQXAC9IVRw"


def _configure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(edit_plan, "EDIT_PLAN_DIR", tmp_path / "edit-plans")


def _evidence_response() -> dict:
    return {
        "data": {
            "index_revision": "idx-text",
            "visual_index_revision": None,
            "hits": [
                {
                    "rank": 1,
                    "evidence_type": "transcript",
                    "video_id": VID,
                    "title": "Example",
                    "channel": "Channel",
                    "start_s": 10.0,
                    "end_s": 15.0,
                    "transcript": {"excerpt": "Evidence."},
                }
            ],
        },
        "error": None,
    }


def _plan() -> dict:
    return edit_plan.build_plan(
        _evidence_response(),
        handle="set-example-20260807-010101",
        query="evidence",
        target_duration_s=20,
        max_clips=4,
        min_clip_duration_s=4,
        max_clip_duration_s=20,
        context_before_s=1,
        context_after_s=1,
    )


def test_load_rejects_tampered_content_addressed_plan(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    plan = _plan()
    path = edit_plan.save_plan(plan)
    tampered = json.loads(path.read_text())
    tampered["clips"][0]["start_s"] = 0.0
    path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(edit_plan.ClipPlanError, match="content hash mismatch"):
        edit_plan.load_plan(plan["plan_revision"])


def test_idempotent_save_allows_nonidentity_timestamp_change(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    first = _plan()
    path = edit_plan.save_plan(first)

    second = _plan()
    second["created_at"] = "2099-01-01T00:00:00Z"
    assert second["plan_revision"] == first["plan_revision"]
    assert edit_plan.save_plan(second) == path

    persisted = edit_plan.load_plan(first["plan_revision"])
    assert persisted["created_at"] == first["created_at"]
