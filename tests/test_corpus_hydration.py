from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import pytest

from youtube_mcp_v2 import cache, corpus_hydration, skeleton
from youtube_mcp_v2.tools.corpus import corpus_hydrate


HANDLE = "topic-hydration-20260806-221500"
VIDEO_IDS = [
    "jNQXAC9IVRw",
    "dQw4w9WgXcQ",
    "aqz-KE-bpKQ",
    "M7lc1UVf-VE",
    "9bZkp7q19f0",
    "kJQP7kiw5Fk",
]


def _configure_storage(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "youtube-mcp"
    monkeypatch.setattr(cache, "CACHE_DIR", root)
    monkeypatch.setattr(cache, "CACHE_PATH", root / "v2.sqlite")
    monkeypatch.setattr(skeleton, "CACHE_DIR", root)
    monkeypatch.setattr(skeleton, "SKELETON_DIR", root / "skeletons")
    monkeypatch.setattr(skeleton, "VECTOR_DIR", root / "vectors")
    monkeypatch.setattr(skeleton, "MODEL_DIR", root / "models")


def _save_corpus(ids: list[str] | None = None) -> None:
    ids = ids or VIDEO_IDS
    skeleton.save_skeleton(
        {
            "handle": HANDLE,
            "target": "topic",
            "value": "hydration fixture",
            "built_at": "2026-08-06T22:15:00Z",
            "expired_at": None,
            "source": "fixture",
            "videos": [
                {"id": video_id, "title": f"Fixture {index}"}
                for index, video_id in enumerate(ids)
            ],
        }
    )


def _cache_transcript(video_id: str, *, text: str = "cached evidence") -> None:
    cache.put_transcript(
        video_id=video_id,
        lang="en",
        actual_lang="en",
        is_generated=False,
        text=text,
        segments=[
            {
                "start_s": 1.0,
                "duration_s": 2.0,
                "end_s": 3.0,
                "text": text,
            }
        ],
        word_count=len(text.split()),
        validated=True,
        warnings=[],
    )


def _success_response(video_id: str, *, text: str) -> dict:
    return {
        "data": {
            "id": video_id,
            "lang": "en",
            "requested_lang": "en",
            "is_generated": False,
            "text": text,
            "word_count": len(text.split()),
        },
        "fetched_at": "2026-08-07T04:40:00Z",
        "source": "scrape",
        "cache_age_s": 0,
        "validated": True,
        "warnings": [],
        "provenance": {
            "acquisition": {
                "provider": "fixture-provider",
                "method": "captions",
                "attempts": [],
            }
        },
        "error": None,
    }


def test_all_cached_corpus_completes_without_acquisition(tmp_path, monkeypatch) -> None:
    _configure_storage(tmp_path, monkeypatch)
    ids = VIDEO_IDS[:4]
    _save_corpus(ids)
    for video_id in ids:
        _cache_transcript(video_id)

    calls: list[str] = []
    monkeypatch.setattr(
        corpus_hydration,
        "_transcript_get",
        lambda video_id, **_kwargs: calls.append(video_id),
    )

    result = corpus_hydration.hydrate_corpus(HANDLE, batch_size=2)
    assert result["complete"] is True
    assert result["next_cursor"] is None
    assert result["scanned"] == 4
    assert result["skipped_cached"] == 4
    assert result["attempted"] == 0
    assert result["results"] == []
    assert calls == []


def test_batch_cursor_skips_cached_members_and_never_returns_transcript_text(
    tmp_path, monkeypatch
) -> None:
    _configure_storage(tmp_path, monkeypatch)
    _save_corpus()
    _cache_transcript(VIDEO_IDS[0])
    secret_text = "SECRET TRANSCRIPT BODY MUST NOT ENTER HYDRATION RESULT"

    def fake_get(video_id: str, **_kwargs) -> dict:
        _cache_transcript(video_id, text=secret_text)
        return _success_response(video_id, text=secret_text)

    monkeypatch.setattr(corpus_hydration, "_transcript_get", fake_get)

    first = corpus_hydration.hydrate_corpus(
        HANDLE, batch_size=2, max_workers=1
    )
    assert first["cursor"] == "0"
    assert first["next_cursor"] == "3"
    assert first["complete"] is False
    assert first["scanned"] == 3
    assert first["skipped_cached"] == 1
    assert [row["position"] for row in first["results"]] == [1, 2]
    assert [row["video_id"] for row in first["results"]] == VIDEO_IDS[1:3]
    assert all(row["status"] == "ready" for row in first["results"])
    assert first["results"][0]["provenance"]["acquisition"]["provider"] == "fixture-provider"
    assert secret_text not in json.dumps(first)

    second = corpus_hydration.hydrate_corpus(
        HANDLE, cursor=first["next_cursor"], batch_size=2, max_workers=1
    )
    assert second["next_cursor"] == "5"
    assert [row["position"] for row in second["results"]] == [3, 4]

    third = corpus_hydration.hydrate_corpus(
        HANDLE, cursor=second["next_cursor"], batch_size=2, max_workers=1
    )
    assert third["complete"] is True
    assert third["next_cursor"] is None
    assert [row["position"] for row in third["results"]] == [5]


def test_failed_member_does_not_stall_cursor_and_restart_retries_only_failure(
    tmp_path, monkeypatch
) -> None:
    _configure_storage(tmp_path, monkeypatch)
    ids = VIDEO_IDS[:3]
    _save_corpus(ids)
    calls: Counter[str] = Counter()

    def fake_get(video_id: str, **_kwargs) -> dict:
        calls[video_id] += 1
        if video_id == ids[1] and calls[video_id] == 1:
            return {
                "data": None,
                "fetched_at": "2026-08-07T04:40:00Z",
                "source": "scrape",
                "cache_age_s": 0,
                "validated": False,
                "warnings": [],
                "error": {
                    "code": "transcript_fetch_failed",
                    "message": "fixture transient failure",
                    "recoverable": True,
                },
            }
        _cache_transcript(video_id, text=f"evidence for {video_id}")
        return _success_response(video_id, text=f"evidence for {video_id}")

    monkeypatch.setattr(corpus_hydration, "_transcript_get", fake_get)

    first = corpus_hydration.hydrate_corpus(
        HANDLE, batch_size=3, max_workers=1
    )
    assert first["complete"] is True
    assert first["succeeded"] == 2
    assert first["failed"] == 1
    assert first["results"][1]["error"]["code"] == "transcript_fetch_failed"

    retry = corpus_hydration.hydrate_corpus(
        HANDLE, cursor="0", batch_size=3, max_workers=1
    )
    assert retry["complete"] is True
    assert retry["skipped_cached"] == 2
    assert retry["attempted"] == 1
    assert retry["succeeded"] == 1
    assert retry["results"][0]["video_id"] == ids[1]
    assert calls[ids[0]] == 1
    assert calls[ids[1]] == 2
    assert calls[ids[2]] == 1


def test_concurrent_completion_order_does_not_change_result_order(
    tmp_path, monkeypatch
) -> None:
    _configure_storage(tmp_path, monkeypatch)
    ids = VIDEO_IDS[:3]
    _save_corpus(ids)
    delays = {ids[0]: 0.04, ids[1]: 0.01, ids[2]: 0.0}

    def fake_get(video_id: str, **_kwargs) -> dict:
        time.sleep(delays[video_id])
        return _success_response(video_id, text=f"body {video_id}")

    monkeypatch.setattr(corpus_hydration, "_transcript_get", fake_get)

    result = corpus_hydration.hydrate_corpus(
        HANDLE, batch_size=3, max_workers=3
    )
    assert [row["video_id"] for row in result["results"]] == ids
    assert [row["position"] for row in result["results"]] == [0, 1, 2]


def test_fresh_policy_refetches_stale_row_while_missing_policy_preserves_it(
    tmp_path, monkeypatch
) -> None:
    _configure_storage(tmp_path, monkeypatch)
    ids = VIDEO_IDS[:2]
    _save_corpus(ids)
    for video_id in ids:
        _cache_transcript(video_id)

    with cache.connect() as conn:
        conn.execute(
            "UPDATE transcripts SET fetched_at = ? WHERE video_id = ?",
            ("2020-01-01T00:00:00Z", ids[0]),
        )

    calls: list[str] = []

    def fake_get(video_id: str, **_kwargs) -> dict:
        calls.append(video_id)
        return _success_response(video_id, text="refetched body")

    monkeypatch.setattr(corpus_hydration, "_transcript_get", fake_get)

    missing = corpus_hydration.hydrate_corpus(
        HANDLE, policy="missing", batch_size=2, max_workers=1
    )
    assert missing["attempted"] == 0
    assert missing["skipped_cached"] == 2

    fresh = corpus_hydration.hydrate_corpus(
        HANDLE, policy="fresh", batch_size=2, max_workers=1
    )
    assert fresh["attempted"] == 1
    assert fresh["skipped_cached"] == 1
    assert fresh["results"][0]["video_id"] == ids[0]
    assert calls == [ids[0]]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"cursor": "nope"}, "cursor"),
        ({"cursor": "-1"}, "cursor"),
        ({"batch_size": 0}, "batch_size"),
        ({"batch_size": 26}, "batch_size"),
        ({"max_workers": 0}, "max_workers"),
        ({"max_workers": 5}, "max_workers"),
        ({"policy": "always"}, "policy"),
        ({"lang": "   "}, "lang"),
    ],
)
def test_invalid_hydration_arguments_fail_before_acquisition(
    tmp_path, monkeypatch, kwargs, message
) -> None:
    _configure_storage(tmp_path, monkeypatch)
    _save_corpus(VIDEO_IDS[:1])
    monkeypatch.setattr(
        corpus_hydration,
        "_transcript_get",
        lambda *_a, **_k: pytest.fail("acquisition should not run"),
    )

    with pytest.raises(corpus_hydration.CorpusHydrationError, match=message):
        corpus_hydration.hydrate_corpus(HANDLE, **kwargs)


def test_tool_wraps_partial_failures_without_leaking_body(tmp_path, monkeypatch) -> None:
    _configure_storage(tmp_path, monkeypatch)
    _save_corpus(VIDEO_IDS[:2])

    def fake_get(video_id: str, **_kwargs) -> dict:
        if video_id == VIDEO_IDS[0]:
            return _success_response(video_id, text="private transcript fixture")
        return {
            "data": None,
            "fetched_at": "2026-08-07T04:40:00Z",
            "source": "scrape",
            "cache_age_s": 0,
            "validated": False,
            "warnings": [],
            "error": {
                "code": "no_transcript",
                "message": "fixture has no transcript",
                "recoverable": False,
            },
        }

    monkeypatch.setattr(corpus_hydration, "_transcript_get", fake_get)
    env = corpus_hydrate(HANDLE, batch_size=2, max_workers=1)

    assert env["error"] is None
    assert env["validated"] is False
    assert env["data"]["failed"] == 1
    assert env["warnings"]
    assert "private transcript fixture" not in json.dumps(env)
