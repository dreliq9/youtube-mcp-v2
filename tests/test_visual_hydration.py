from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import pytest

from youtube_mcp_v2 import skeleton, visual_hydration, visual_index
from youtube_mcp_v2.adapters import frame_extract


HANDLE = "topic-visual-hydration-20260806-221500"
VIDEO_IDS = [
    "jNQXAC9IVRw",
    "dQw4w9WgXcQ",
    "aqz-KE-bpKQ",
    "M7lc1UVf-VE",
    "9bZkp7q19f0",
]


def _configure(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "youtube-mcp"
    monkeypatch.setattr(skeleton, "CACHE_DIR", root)
    monkeypatch.setattr(skeleton, "SKELETON_DIR", root / "skeletons")
    monkeypatch.setattr(skeleton, "VECTOR_DIR", root / "vectors")
    monkeypatch.setattr(skeleton, "MODEL_DIR", root / "models")
    monkeypatch.setattr(visual_index, "FRAMES_DIR", root / "frames")
    return root


def _save(ids: list[str] | None = None) -> None:
    ids = ids or VIDEO_IDS
    skeleton.save_skeleton(
        {
            "handle": HANDLE,
            "target": "topic",
            "value": "visual hydration fixture",
            "built_at": "2026-08-06T22:15:00Z",
            "expired_at": None,
            "source": "fixture",
            "videos": [
                {"id": video_id, "title": f"Video {index}"}
                for index, video_id in enumerate(ids)
            ],
        }
    )


def _cache_single(root: Path, video_id: str, timestamp_ms: int) -> Path:
    path = root / "frames" / video_id / f"single_{timestamp_ms}ms_640x360.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PNG fixture")
    return path


def _result(video_id: str, *, n: int, cached: bool = False) -> frame_extract.ContactSheetResult:
    timestamps = [float(i * 10) for i in range(n)]
    return frame_extract.ContactSheetResult(
        path=f"/PRIVATE/{video_id}/sheet.png",
        layout="4x3",
        frame_timestamps=timestamps,
        frame_paths=[f"/PRIVATE/{video_id}/f{i}.png" for i in range(n)],
        cached=cached,
    )


def test_all_members_with_required_cached_frame_count_skip_acquisition(
    tmp_path, monkeypatch
) -> None:
    root = _configure(tmp_path, monkeypatch)
    ids = VIDEO_IDS[:3]
    _save(ids)
    for video_id in ids:
        _cache_single(root, video_id, 1000)
        _cache_single(root, video_id, 2000)

    monkeypatch.setattr(
        visual_hydration,
        "_extract",
        lambda *_a, **_k: pytest.fail("visual acquisition should not run"),
    )

    result = visual_hydration.hydrate_visuals(HANDLE, n=2, layout="2x1")
    assert result["complete"] is True
    assert result["next_cursor"] is None
    assert result["skipped_cached"] == 3
    assert result["attempted"] == 0


def test_cursor_scans_cached_members_and_batches_only_undercovered_videos(
    tmp_path, monkeypatch
) -> None:
    root = _configure(tmp_path, monkeypatch)
    _save()
    # Position 0 already has the required two trustworthy frames.
    _cache_single(root, VIDEO_IDS[0], 1000)
    _cache_single(root, VIDEO_IDS[0], 2000)

    calls: list[str] = []

    def fake_extract(candidate, **kwargs):
        calls.append(candidate.video_id)
        return _result(candidate.video_id, n=kwargs["n"])

    monkeypatch.setattr(visual_hydration, "_extract", fake_extract)

    first = visual_hydration.hydrate_visuals(
        HANDLE,
        batch_size=2,
        max_workers=1,
        n=2,
        layout="2x1",
    )
    assert first["cursor"] == "0"
    assert first["next_cursor"] == "3"
    assert first["skipped_cached"] == 1
    assert [row["video_id"] for row in first["results"]] == VIDEO_IDS[1:3]

    second = visual_hydration.hydrate_visuals(
        HANDLE,
        cursor=first["next_cursor"],
        batch_size=2,
        max_workers=1,
        n=2,
        layout="2x1",
    )
    assert second["complete"] is True
    assert second["next_cursor"] is None
    assert [row["video_id"] for row in second["results"]] == VIDEO_IDS[3:5]
    assert calls == VIDEO_IDS[1:5]


def test_results_are_compact_and_never_return_frame_or_sheet_paths(
    tmp_path, monkeypatch
) -> None:
    _configure(tmp_path, monkeypatch)
    _save(VIDEO_IDS[:1])
    monkeypatch.setattr(
        visual_hydration,
        "_extract",
        lambda candidate, **kwargs: _result(candidate.video_id, n=kwargs["n"]),
    )

    result = visual_hydration.hydrate_visuals(
        HANDLE, n=2, layout="2x1", batch_size=1
    )
    row = result["results"][0]
    assert row["frame_count"] == 2
    assert row["first_timestamp_s"] == 0.0
    assert row["last_timestamp_s"] == 10.0
    serialized = json.dumps(result)
    assert "/PRIVATE/" not in serialized
    assert "frame_paths" not in serialized
    assert "sheet.png" not in serialized


def test_failure_advances_cursor_and_restart_retries_only_undercovered_member(
    tmp_path, monkeypatch
) -> None:
    root = _configure(tmp_path, monkeypatch)
    ids = VIDEO_IDS[:3]
    _save(ids)
    calls: Counter[str] = Counter()

    def fake_extract(candidate, **kwargs):
        calls[candidate.video_id] += 1
        if candidate.video_id == ids[1] and calls[candidate.video_id] == 1:
            raise frame_extract.FrameExtractError("fixture timeout with /private/path")
        # Simulate the durable cache side effect produced by real extraction so
        # a restart can skip prior successes.
        _cache_single(root, candidate.video_id, 1000)
        _cache_single(root, candidate.video_id, 2000)
        return _result(candidate.video_id, n=kwargs["n"])

    monkeypatch.setattr(visual_hydration, "_extract", fake_extract)

    first = visual_hydration.hydrate_visuals(
        HANDLE,
        n=2,
        layout="2x1",
        batch_size=3,
        max_workers=1,
    )
    assert first["complete"] is True
    assert first["succeeded"] == 2
    assert first["failed"] == 1
    assert first["results"][1]["error"]["code"] == "frame_extract_timeout"
    assert "/private/path" not in json.dumps(first)

    retry = visual_hydration.hydrate_visuals(
        HANDLE,
        cursor="0",
        n=2,
        layout="2x1",
        batch_size=3,
        max_workers=1,
    )
    assert retry["skipped_cached"] == 2
    assert retry["attempted"] == 1
    assert retry["results"][0]["video_id"] == ids[1]
    assert calls[ids[0]] == 1
    assert calls[ids[1]] == 2
    assert calls[ids[2]] == 1


def test_concurrent_completion_order_does_not_change_frozen_corpus_order(
    tmp_path, monkeypatch
) -> None:
    _configure(tmp_path, monkeypatch)
    ids = VIDEO_IDS[:3]
    _save(ids)
    delays = {ids[0]: 0.04, ids[1]: 0.01, ids[2]: 0.0}

    def fake_extract(candidate, **kwargs):
        time.sleep(delays[candidate.video_id])
        return _result(candidate.video_id, n=kwargs["n"])

    monkeypatch.setattr(visual_hydration, "_extract", fake_extract)
    result = visual_hydration.hydrate_visuals(
        HANDLE,
        n=2,
        layout="2x1",
        batch_size=3,
        max_workers=2,
    )
    assert [row["video_id"] for row in result["results"]] == ids
    assert [row["position"] for row in result["results"]] == [0, 1, 2]


def test_partial_cache_does_not_skip_video_below_requested_frame_count(
    tmp_path, monkeypatch
) -> None:
    root = _configure(tmp_path, monkeypatch)
    _save(VIDEO_IDS[:1])
    _cache_single(root, VIDEO_IDS[0], 1000)
    monkeypatch.setattr(
        visual_hydration,
        "_extract",
        lambda candidate, **kwargs: _result(candidate.video_id, n=kwargs["n"]),
    )
    result = visual_hydration.hydrate_visuals(
        HANDLE, n=2, layout="2x1", batch_size=1
    )
    assert result["skipped_cached"] == 0
    assert result["attempted"] == 1
    assert result["results"][0]["cached_before"] == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"cursor": "bad"}, "cursor"),
        ({"cursor": "-1"}, "cursor"),
        ({"batch_size": 0}, "batch_size"),
        ({"batch_size": 7}, "batch_size"),
        ({"max_workers": 0}, "max_workers"),
        ({"max_workers": 3}, "max_workers"),
        ({"n": 0}, "n"),
        ({"n": 25}, "n"),
        ({"n": 5, "layout": "2x2"}, "layout capacity"),
        ({"layout": "bogus"}, "layout"),
        ({"size": "tiny"}, "size"),
        ({"size": "32x32"}, "size dimensions"),
        ({"fmt": "webp"}, "fmt"),
        ({"policy": "refresh"}, "policy"),
    ],
)
def test_invalid_arguments_fail_before_media_acquisition(
    tmp_path, monkeypatch, kwargs, message
) -> None:
    _configure(tmp_path, monkeypatch)
    _save(VIDEO_IDS[:1])
    monkeypatch.setattr(
        visual_hydration,
        "_extract",
        lambda *_a, **_k: pytest.fail("media acquisition should not run"),
    )
    with pytest.raises(visual_hydration.VisualHydrationError, match=message):
        visual_hydration.hydrate_visuals(HANDLE, **kwargs)
