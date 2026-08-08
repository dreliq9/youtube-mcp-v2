from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server import MCPServer

from youtube_mcp_v2 import cache, skeleton, validate
from youtube_mcp_v2.server import mcp
from youtube_mcp_v2.tools.transcript import transcript_get


PINNED_ID = "jNQXAC9IVRw"


def test_server_uses_mcp_sdk_v2_high_level_server() -> None:
    assert isinstance(mcp, MCPServer)


def test_transcript_unknown_mode_is_explicit_error() -> None:
    env = transcript_get(PINNED_ID, mode="mystery")  # type: ignore[arg-type]
    assert env["data"] is None
    assert env["error"]["code"] == "bad_mode"
    assert env["error"]["recoverable"] is False


def test_transcript_rejects_invalid_chunk_budget() -> None:
    zero = transcript_get(PINNED_ID, mode="chunked", chunk_tokens=0)
    assert zero["error"]["code"] == "bad_chunk_tokens"

    overlap = transcript_get(
        PINNED_ID,
        mode="chunked",
        chunk_tokens=100,
        chunk_overlap=100,
    )
    assert overlap["error"]["code"] == "bad_chunk_overlap"


def test_missing_duration_is_not_reported_as_fully_validated() -> None:
    ok, warnings = validate.validate_transcript(
        text="this is an otherwise plausible transcript",
        segments=[{"start_s": 0.0, "duration_s": 1.0}],
        duration_s=None,
        requested_lang="en",
        actual_lang="en",
    )
    assert ok is False
    assert any("word-rate validation skipped" in warning for warning in warnings)


def test_cache_preserves_requested_and_actual_language(tmp_path, monkeypatch) -> None:
    cache_dir = tmp_path / "youtube-mcp"
    cache_path = cache_dir / "v2.sqlite"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_PATH", cache_path)

    cache.put_transcript(
        video_id=PINNED_ID,
        lang="fr",
        actual_lang="en",
        is_generated=True,
        text="hello world",
        segments=[{"start_s": 0.0, "duration_s": 1.0, "text": "hello world"}],
        word_count=2,
        validated=True,
        warnings=["lang fallback: requested=fr got=en"],
    )

    hit = cache.get_transcript(PINNED_ID, "fr")
    assert hit is not None
    row, _age = hit
    assert row["lang"] == "fr"
    assert row["actual_lang"] == "en"
    assert row["is_generated"] == 1


def test_cache_migrates_legacy_transcript_table(tmp_path, monkeypatch) -> None:
    import sqlite3

    cache_dir = tmp_path / "youtube-mcp"
    cache_dir.mkdir(parents=True)
    cache_path = cache_dir / "v2.sqlite"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_PATH", cache_path)

    conn = sqlite3.connect(cache_path)
    conn.execute(
        """
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL,
            lang TEXT NOT NULL,
            text TEXT NOT NULL,
            segments_json TEXT,
            word_count INTEGER,
            fetched_at TEXT NOT NULL,
            validated INTEGER NOT NULL,
            warnings_json TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    with cache.connect() as migrated:
        columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(transcripts)").fetchall()
        }

    assert "actual_lang" in columns
    assert "is_generated" in columns
    assert Path(cache.CACHE_PATH).exists()


def _redirect_skeleton_dirs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(skeleton, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(skeleton, "SKELETON_DIR", tmp_path / "skeletons")
    monkeypatch.setattr(skeleton, "VECTOR_DIR", tmp_path / "vectors")
    monkeypatch.setattr(skeleton, "MODEL_DIR", tmp_path / "models")


def test_new_and_legacy_skeleton_handles_are_valid() -> None:
    new_handle = skeleton.make_handle("topic", "battery research")
    assert skeleton.is_valid_handle(new_handle)
    assert skeleton.is_valid_handle("topic-battery-research-20260430-120000")


def test_skeleton_creation_refuses_to_overwrite(tmp_path, monkeypatch) -> None:
    _redirect_skeleton_dirs(tmp_path, monkeypatch)
    handle = skeleton.make_handle("topic", "collision test")
    payload = {
        "handle": handle,
        "target": "topic",
        "value": "collision test",
        "built_at": skeleton._now_iso(),
        "source": "scrape",
        "expired_at": None,
        "channel": None,
        "videos": [],
    }

    skeleton.save_skeleton(payload)
    with pytest.raises(FileExistsError):
        skeleton.save_skeleton(payload)
