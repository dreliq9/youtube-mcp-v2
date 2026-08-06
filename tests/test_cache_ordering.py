from __future__ import annotations

from youtube_mcp_v2 import cache


def _redirect_cache(tmp_path, monkeypatch) -> None:
    cache_dir = tmp_path / "youtube-mcp"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_PATH", cache_dir / "v2.sqlite")


def _freeze_cache_clock(monkeypatch) -> str:
    fixed = cache._now_iso()
    monkeypatch.setattr(cache, "_now_iso", lambda: fixed)
    return fixed


def test_transcript_latest_revision_uses_id_to_break_timestamp_tie(
    tmp_path, monkeypatch
) -> None:
    _redirect_cache(tmp_path, monkeypatch)
    _freeze_cache_clock(monkeypatch)

    common = {
        "video_id": "jNQXAC9IVRw",
        "lang": "en",
        "segments": [],
        "word_count": 1,
        "validated": True,
        "warnings": [],
    }
    cache.put_transcript(text="first", **common)
    cache.put_transcript(text="second", **common)

    hit = cache.get_transcript("jNQXAC9IVRw", "en")
    assert hit is not None
    row, _age = hit
    assert row["text"] == "second"


def test_video_meta_latest_revision_uses_id_to_break_timestamp_tie(
    tmp_path, monkeypatch
) -> None:
    _redirect_cache(tmp_path, monkeypatch)
    _freeze_cache_clock(monkeypatch)

    cache.put_video_meta("jNQXAC9IVRw", {"revision": "first"})
    cache.put_video_meta("jNQXAC9IVRw", {"revision": "second"})

    hit = cache.get_video_meta("jNQXAC9IVRw")
    assert hit is not None
    payload, _age = hit
    assert payload["revision"] == "second"


def test_search_latest_revision_uses_id_to_break_timestamp_tie(
    tmp_path, monkeypatch
) -> None:
    _redirect_cache(tmp_path, monkeypatch)
    _freeze_cache_clock(monkeypatch)

    cache.put_search_results("battery", [{"revision": "first"}])
    cache.put_search_results("battery", [{"revision": "second"}])

    hit = cache.get_search_results("battery", min_results=1)
    assert hit is not None
    payload, _age = hit
    assert payload[0]["revision"] == "second"


def test_same_second_revisions_remain_append_only(tmp_path, monkeypatch) -> None:
    _redirect_cache(tmp_path, monkeypatch)
    _freeze_cache_clock(monkeypatch)

    cache.put_video_meta("jNQXAC9IVRw", {"revision": 1})
    cache.put_video_meta("jNQXAC9IVRw", {"revision": 2})

    with cache.connect() as conn:
        rows = conn.execute(
            "SELECT id, payload_json, fetched_at FROM video_meta "
            "WHERE video_id = ? ORDER BY id ASC",
            ("jNQXAC9IVRw",),
        ).fetchall()

    assert len(rows) == 2
    assert rows[0]["id"] < rows[1]["id"]
    assert rows[0]["fetched_at"] == rows[1]["fetched_at"]
