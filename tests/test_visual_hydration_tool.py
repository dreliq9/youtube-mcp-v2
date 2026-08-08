from __future__ import annotations

import json

from youtube_mcp_v2 import visual_hydration
from youtube_mcp_v2.tools.visual_hydrate import corpus_visual_hydrate


HANDLE = "topic-visual-hydration-20260806-221500"


def test_wrapper_returns_progress_and_warning_without_media_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        visual_hydration,
        "hydrate_visuals",
        lambda *_a, **_k: {
            "corpus_revision": HANDLE,
            "policy": "missing",
            "cursor": "0",
            "next_cursor": "2",
            "complete": False,
            "total_videos": 4,
            "scanned": 2,
            "skipped_cached": 0,
            "attempted": 2,
            "succeeded": 1,
            "failed": 1,
            "remaining_members": 2,
            "requested_frames_per_video": 12,
            "layout": "4x3",
            "size": "1280x720",
            "format": "png",
            "results": [
                {
                    "position": 0,
                    "video_id": "jNQXAC9IVRw",
                    "title": "Ready",
                    "status": "ready",
                    "cached_before": 0,
                    "cached_extraction": False,
                    "frame_count": 12,
                    "first_timestamp_s": 4.0,
                    "last_timestamp_s": 98.0,
                    "error": None,
                },
                {
                    "position": 1,
                    "video_id": "dQw4w9WgXcQ",
                    "title": "Failed",
                    "status": "failed",
                    "cached_before": 0,
                    "cached_extraction": None,
                    "frame_count": 0,
                    "first_timestamp_s": None,
                    "last_timestamp_s": None,
                    "error": {
                        "code": "frame_extract_timeout",
                        "message": "visual hydration failed with FrameExtractError",
                        "recoverable": True,
                    },
                },
            ],
        },
    )

    env = corpus_visual_hydrate(HANDLE)
    assert env["error"] is None
    assert env["validated"] is False
    assert env["data"]["next_cursor"] == "2"
    assert len(env["warnings"]) == 2
    serialized = json.dumps(env)
    assert "/private/" not in serialized.lower()
    assert "frame_paths" not in serialized


def test_wrapper_maps_invalid_request_to_nonrecoverable_error(monkeypatch) -> None:
    def fail(*_a, **_k):
        raise visual_hydration.VisualHydrationError("batch_size must be between 1 and 6")

    monkeypatch.setattr(visual_hydration, "hydrate_visuals", fail)
    env = corpus_visual_hydrate(HANDLE, batch_size=99)
    assert env["error"]["code"] == "visual_hydrate_failed"
    assert env["error"]["recoverable"] is False


def test_wrapper_maps_missing_corpus(monkeypatch) -> None:
    monkeypatch.setattr(
        visual_hydration,
        "hydrate_visuals",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("missing corpus")),
    )
    env = corpus_visual_hydrate(HANDLE)
    assert env["error"]["code"] == "corpus_not_found"
    assert env["error"]["recoverable"] is False
