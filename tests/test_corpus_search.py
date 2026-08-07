from __future__ import annotations

import json
from pathlib import Path

import pytest

from youtube_mcp_v2 import cache, corpus_index, skeleton
from youtube_mcp_v2.tools.corpus import corpus_prepare, corpus_search


HANDLE = "topic-power-electronics-20260806-010203"
VID_A = "jNQXAC9IVRw"
VID_B = "dQw4w9WgXcQ"
VID_C = "aqz-KE-bpKQ"


def _configure_storage(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "youtube-mcp"
    monkeypatch.setattr(cache, "CACHE_DIR", root)
    monkeypatch.setattr(cache, "CACHE_PATH", root / "v2.sqlite")
    monkeypatch.setattr(skeleton, "CACHE_DIR", root)
    monkeypatch.setattr(skeleton, "SKELETON_DIR", root / "skeletons")
    monkeypatch.setattr(skeleton, "VECTOR_DIR", root / "vectors")
    monkeypatch.setattr(skeleton, "MODEL_DIR", root / "models")
    monkeypatch.setattr(corpus_index, "VECTOR_DIR", root / "vectors")
    monkeypatch.setattr(corpus_index, "INDEX_ROOT", root / "vectors" / "corpus")


def _save_corpus(*, include_c: bool = True) -> None:
    videos = [
        {"id": VID_A, "title": "Buck Converter Teardown", "channel": "Lab A"},
        {"id": VID_B, "title": "Transient Power Test", "channel": "Lab B"},
    ]
    if include_c:
        videos.append({"id": VID_C, "title": "No Captions Yet", "channel": "Lab C"})
    skeleton.save_skeleton(
        {
            "handle": HANDLE,
            "target": "topic",
            "value": "power electronics",
            "built_at": "2026-08-06T01:02:03Z",
            "expired_at": None,
            "source": "scrape",
            "videos": videos,
        }
    )


def _put(
    video_id: str,
    text_segments: list[tuple[float, float, str]],
    *,
    validated: bool = True,
    actual_lang: str = "en",
) -> None:
    segments = [
        {
            "start_s": start,
            "duration_s": duration,
            "end_s": start + duration,
            "text": text,
        }
        for start, duration, text in text_segments
    ]
    text = " ".join(seg["text"] for seg in segments)
    cache.put_transcript(
        video_id=video_id,
        lang="en",
        actual_lang=actual_lang,
        is_generated=False,
        text=text,
        segments=segments,
        word_count=len(text.split()),
        validated=validated,
        warnings=[] if validated else ["fixture validation warning"],
    )


def _fixture(tmp_path: Path, monkeypatch) -> None:
    _configure_storage(tmp_path, monkeypatch)
    _save_corpus()
    _put(
        VID_A,
        [
            (10.0, 5.0, "The TPS62132-Q1 buck converter is efficient at light load."),
            (15.0, 5.0, "We measured switching ripple on the three point three volt rail."),
        ],
    )
    _put(
        VID_B,
        [
            (42.0, 4.0, "Transient power excursions briefly exceeded rated TGP."),
            (46.0, 4.0, "The oscilloscope captured the peak during the load step."),
        ],
    )


def test_prepare_is_content_addressed_and_reports_coverage(tmp_path, monkeypatch) -> None:
    _fixture(tmp_path, monkeypatch)

    first = corpus_index.prepare_index(HANDLE, requested_lang="en", chunk_tokens=80)
    second = corpus_index.prepare_index(HANDLE, requested_lang="en", chunk_tokens=80)

    assert first.index_revision.startswith("idx1-")
    assert first.index_revision == second.index_revision
    assert first.cached is False
    assert second.cached is True
    assert first.total_videos == 3
    assert first.indexed_videos == 2
    assert first.missing_videos == [VID_C]
    assert first.path.is_file()

    conn = corpus_index.sqlite3.connect(first.path)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()
    sources = json.loads(meta["source_revisions"])
    assert {source["video_id"] for source in sources} == {VID_A, VID_B}
    assert all(source["transcript_revision_id"] > 0 for source in sources)
    assert all(len(source["transcript_sha256"]) == 64 for source in sources)


def test_search_returns_timestamped_provenance_and_part_number_hit(
    tmp_path, monkeypatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    prepared = corpus_index.prepare_index(HANDLE, chunk_tokens=20, chunk_overlap=2)

    result = corpus_index.search_index(
        HANDLE,
        "TPS62132-Q1",
        top_k=5,
        index_revision=prepared.index_revision,
    )

    assert result["index_revision"] == prepared.index_revision
    assert result["hits"]
    hit = result["hits"][0]
    assert hit["video_id"] == VID_A
    assert hit["start_s"] == 10.0
    assert "t=10s" in hit["url"]
    assert "TPS62132-Q1" in hit["excerpt"]
    assert hit["scores"]["lexical"] == pytest.approx(1.0)
    assert hit["scores"]["semantic"] is None
    assert hit["transcript"]["revision_id"] > 0
    assert len(hit["transcript"]["sha256"]) == 64
    assert hit["transcript"]["actual_lang"] == "en"
    assert hit["chunk"]["sha256"]


def test_pinned_index_remains_reproducible_after_transcript_refresh(
    tmp_path, monkeypatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    old = corpus_index.prepare_index(HANDLE, chunk_tokens=30)

    _put(
        VID_A,
        [(10.0, 5.0, "The replacement transcript discusses a gallium nitride stage only.")],
    )
    new = corpus_index.prepare_index(HANDLE, chunk_tokens=30)

    assert new.index_revision != old.index_revision

    old_result = corpus_index.search_index(
        HANDLE, "TPS62132-Q1", index_revision=old.index_revision
    )
    new_result = corpus_index.search_index(
        HANDLE, "TPS62132-Q1", index_revision=new.index_revision
    )
    assert old_result["hits"][0]["video_id"] == VID_A
    assert new_result["hits"] == []

    new_topic = corpus_index.search_index(
        HANDLE, "gallium nitride", index_revision=new.index_revision
    )
    assert new_topic["hits"][0]["video_id"] == VID_A


def test_expiration_metadata_does_not_change_index_identity(tmp_path, monkeypatch) -> None:
    _fixture(tmp_path, monkeypatch)
    before = corpus_index.prepare_index(HANDLE)
    skeleton.expire_skeleton(HANDLE)
    after = corpus_index.prepare_index(HANDLE)
    assert after.index_revision == before.index_revision


def test_validated_only_excludes_unvalidated_evidence(tmp_path, monkeypatch) -> None:
    _configure_storage(tmp_path, monkeypatch)
    _save_corpus(include_c=False)
    _put(VID_A, [(1.0, 2.0, "shared phrase from validated evidence")], validated=True)
    _put(VID_B, [(3.0, 2.0, "shared phrase from suspect evidence")], validated=False)
    prepared = corpus_index.prepare_index(HANDLE, chunk_tokens=20)

    all_hits = corpus_index.search_index(
        HANDLE, "shared phrase", index_revision=prepared.index_revision, top_k=10
    )
    valid_hits = corpus_index.search_index(
        HANDLE,
        "shared phrase",
        index_revision=prepared.index_revision,
        top_k=10,
        validated_only=True,
    )
    assert {hit["video_id"] for hit in all_hits["hits"]} == {VID_A, VID_B}
    assert [hit["video_id"] for hit in valid_hits["hits"]] == [VID_A]


def test_tool_auto_prepare_warns_on_partial_coverage(tmp_path, monkeypatch) -> None:
    _fixture(tmp_path, monkeypatch)

    env = corpus_search(HANDLE, "transient power", top_k=3, auto_prepare=True)
    assert env["error"] is None
    assert env["data"]["hits"][0]["video_id"] == VID_B
    assert env["data"]["coverage"]["indexed_videos"] == 2
    assert env["warnings"]

    prepared_env = corpus_prepare(HANDLE)
    assert prepared_env["error"] is None
    assert prepared_env["data"]["missing_videos"] == [VID_C]
    assert prepared_env["validated"] is False


def test_duplicate_video_ids_are_rejected(tmp_path, monkeypatch) -> None:
    _configure_storage(tmp_path, monkeypatch)
    skeleton.save_skeleton(
        {
            "handle": HANDLE,
            "target": "topic",
            "value": "duplicate",
            "built_at": "2026-08-06T01:02:03Z",
            "expired_at": None,
            "source": "scrape",
            "videos": [{"id": VID_A}, {"id": VID_A}],
        }
    )
    _put(VID_A, [(1.0, 1.0, "duplicate fixture")])

    with pytest.raises(corpus_index.CorpusIndexError, match="duplicate video id"):
        corpus_index.prepare_index(HANDLE)
