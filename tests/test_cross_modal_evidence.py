from __future__ import annotations

import json

import pytest

from youtube_mcp_v2.tools import evidence


HANDLE = "topic-cross-modal-20260806-230000"
VID_A = "jNQXAC9IVRw"
VID_B = "dQw4w9WgXcQ"
VID_C = "aqz-KE-bpKQ"


def _ok(data, *, warnings=None, validated=True):
    return {
        "data": data,
        "fetched_at": "2026-08-07T05:00:00Z",
        "source": "cache",
        "cache_age_s": 0,
        "validated": validated,
        "warnings": warnings or [],
        "error": None,
    }


def _fail(code: str, message: str = "fixture failure", *, recoverable=True):
    return {
        "data": None,
        "fetched_at": "2026-08-07T05:00:00Z",
        "source": "cache",
        "cache_age_s": 0,
        "validated": False,
        "warnings": [],
        "error": {"code": code, "message": message, "recoverable": recoverable},
    }


def _transcript_response():
    return _ok(
        {
            "corpus_revision": HANDLE,
            "index_revision": "idx1-transcript",
            "coverage": {"total_videos": 3, "indexed_videos": 3},
            "hits": [
                {
                    "rank": 1,
                    "video_id": VID_A,
                    "title": "Power Test",
                    "channel": "Lab A",
                    "start_s": 100.0,
                    "end_s": 110.0,
                    "url": f"https://www.youtube.com/watch?v={VID_A}&t=100s",
                    "excerpt": "the transient briefly exceeded the rated ceiling",
                    "scores": {"lexical": 0.0, "semantic": 0.98, "hybrid": 0.016},
                    "transcript_revision_id": 77,
                    "transcript_sha256": "a" * 64,
                    "chunk_sha256": "b" * 64,
                    "validated": True,
                },
                {
                    "rank": 2,
                    "video_id": VID_B,
                    "title": "Other Claim",
                    "channel": "Lab B",
                    "start_s": 50.0,
                    "end_s": 55.0,
                    "excerpt": "mechanical enclosure discussion",
                    "scores": {"lexical": 1.4, "semantic": 0.2, "hybrid": 0.015},
                    "transcript_revision_id": 78,
                    "transcript_sha256": "c" * 64,
                    "chunk_sha256": "d" * 64,
                    "validated": True,
                },
            ],
        },
        warnings=["partial transcript coverage"],
    )


def _visual_response():
    return _ok(
        {
            "corpus_revision": HANDLE,
            "visual_index_revision": "vidx1-visual",
            "coverage": {"total_videos": 3, "indexed_videos": 2},
            "hits": [
                {
                    "rank": 1,
                    "video_id": VID_A,
                    "title": "Power Test",
                    "channel": "Lab A",
                    "timestamp_s": 105.0,
                    "url": f"https://www.youtube.com/watch?v={VID_A}&t=105s",
                    "artifact_path": "/private/cache/frame.png",
                    "artifact_ext": "png",
                    "frame_sha256": "e" * 64,
                    "source_kind": "contact_sheet_frame",
                    "score": 0.91,
                    "resource_uri": f"youtube-mcp://evidence/frame/png/{'e' * 64}",
                    "resource_mime_type": "image/png",
                    "resource_size_bytes": 12345,
                    "resource_portable": True,
                },
                {
                    "rank": 2,
                    "video_id": VID_C,
                    "title": "Separate Visual",
                    "channel": "Lab C",
                    "timestamp_s": 25.0,
                    "artifact_path": "/private/cache/other.png",
                    "artifact_ext": "png",
                    "frame_sha256": "f" * 64,
                    "source_kind": "single_frame",
                    "score": 0.8,
                    "resource_uri": f"youtube-mcp://evidence/frame/png/{'f' * 64}",
                    "resource_mime_type": "image/png",
                    "resource_size_bytes": 999,
                    "resource_portable": True,
                },
            ],
        },
        warnings=["partial visual coverage"],
    )


def test_cross_modal_agreement_becomes_one_top_ranked_temporal_cluster(monkeypatch) -> None:
    monkeypatch.setattr(evidence, "corpus_search", lambda *_a, **_k: _transcript_response())
    monkeypatch.setattr(
        evidence, "corpus_visual_search", lambda *_a, **_k: _visual_response()
    )

    env = evidence.corpus_evidence_search(
        HANDLE,
        "brief power excursion shown on the chart",
        top_k=5,
        temporal_window_s=20.0,
    )

    assert env["error"] is None
    data = env["data"]
    assert data["modalities_used"] == ["transcript", "visual"]
    assert data["transcript_index_revision"] == "idx1-transcript"
    assert data["visual_index_revision"] == "vidx1-visual"
    assert data["hits"][0]["evidence_type"] == "cross_modal"
    assert data["hits"][0]["video_id"] == VID_A
    assert data["hits"][0]["fusion"]["transcript_rank"] == 1
    assert data["hits"][0]["fusion"]["visual_rank"] == 1
    assert data["hits"][0]["fusion"]["temporal_distance_s"] == 0.0
    assert data["hits"][0]["fusion"]["score"] == pytest.approx(1.0)
    assert data["hits"][0]["transcript"]["excerpt"].startswith("the transient")
    assert data["hits"][0]["frame"]["resource_uri"].startswith("youtube-mcp://")
    assert "artifact_path" not in data["hits"][0]["frame"]
    assert "/private/cache" not in json.dumps(env)
    assert any(warning.startswith("transcript:") for warning in env["warnings"])
    assert any(warning.startswith("visual:") for warning in env["warnings"])


def test_auto_mode_falls_back_to_transcript_when_visual_is_not_local(monkeypatch) -> None:
    monkeypatch.setattr(evidence, "corpus_search", lambda *_a, **_k: _transcript_response())
    monkeypatch.setattr(
        evidence,
        "corpus_visual_search",
        lambda *_a, **_k: _fail("visual_unavailable"),
    )

    env = evidence.corpus_evidence_search(HANDLE, "mechanical enclosure", modalities="auto")
    assert env["error"] is None
    assert env["data"]["modalities_used"] == ["transcript"]
    assert all(hit["evidence_type"] == "transcript" for hit in env["data"]["hits"])
    assert any("visual evidence unavailable in auto mode" in w for w in env["warnings"])


def test_both_mode_requires_visual_component(monkeypatch) -> None:
    monkeypatch.setattr(evidence, "corpus_search", lambda *_a, **_k: _transcript_response())
    monkeypatch.setattr(
        evidence,
        "corpus_visual_search",
        lambda *_a, **_k: _fail("visual_index_missing", recoverable=True),
    )

    env = evidence.corpus_evidence_search(HANDLE, "scope", modalities="both")
    assert env["error"]["code"] == "visual_evidence_failed"
    assert env["error"]["recoverable"] is True


def test_explicit_visual_revision_is_required_even_in_auto_mode(monkeypatch) -> None:
    monkeypatch.setattr(evidence, "corpus_search", lambda *_a, **_k: _transcript_response())
    monkeypatch.setattr(
        evidence,
        "corpus_visual_search",
        lambda *_a, **_k: _fail("visual_unavailable"),
    )
    env = evidence.corpus_evidence_search(
        HANDLE,
        "scope",
        modalities="auto",
        visual_index_revision="vidx1-pinned",
    )
    assert env["error"]["code"] == "visual_evidence_failed"


def test_component_calls_are_local_only_and_receive_pinned_revisions(monkeypatch) -> None:
    seen = {}

    def transcript(*args, **kwargs):
        seen["transcript"] = (args, kwargs)
        return _transcript_response()

    def visual(*args, **kwargs):
        seen["visual"] = (args, kwargs)
        return _visual_response()

    monkeypatch.setattr(evidence, "corpus_search", transcript)
    monkeypatch.setattr(evidence, "corpus_visual_search", visual)

    env = evidence.corpus_evidence_search(
        HANDLE,
        "scope",
        index_revision="idx1-pinned",
        visual_index_revision="vidx1-pinned",
        validated_only=True,
        auto_prepare=False,
    )
    assert env["error"] is None
    assert seen["transcript"][1]["semantic"] == "auto"
    assert seen["visual"][1]["visual"] == "auto"
    assert seen["transcript"][1]["index_revision"] == "idx1-pinned"
    assert seen["visual"][1]["visual_index_revision"] == "vidx1-pinned"
    assert seen["transcript"][1]["validated_only"] is True
    assert seen["transcript"][1]["auto_prepare"] is False
    assert seen["visual"][1]["auto_prepare"] is False


def test_one_visual_hit_pairs_with_only_one_nearest_transcript(monkeypatch) -> None:
    transcript = _transcript_response()
    transcript["data"]["hits"] = [
        {
            "rank": 1,
            "video_id": VID_A,
            "start_s": 90.0,
            "end_s": 99.0,
            "excerpt": "first segment",
        },
        {
            "rank": 2,
            "video_id": VID_A,
            "start_s": 104.0,
            "end_s": 106.0,
            "excerpt": "nearest segment",
        },
    ]
    visual = _visual_response()
    visual["data"]["hits"] = [visual["data"]["hits"][0]]
    monkeypatch.setattr(evidence, "corpus_search", lambda *_a, **_k: transcript)
    monkeypatch.setattr(evidence, "corpus_visual_search", lambda *_a, **_k: visual)

    env = evidence.corpus_evidence_search(
        HANDLE, "scope", top_k=5, temporal_window_s=20.0
    )
    cross_modal = [
        hit for hit in env["data"]["hits"] if hit["evidence_type"] == "cross_modal"
    ]
    assert len(cross_modal) == 1
    assert cross_modal[0]["transcript"]["excerpt"] == "nearest segment"


def test_visual_only_mode_never_calls_transcript(monkeypatch) -> None:
    monkeypatch.setattr(
        evidence,
        "corpus_search",
        lambda *_a, **_k: pytest.fail("transcript component should not run"),
    )
    monkeypatch.setattr(evidence, "corpus_visual_search", lambda *_a, **_k: _visual_response())
    env = evidence.corpus_evidence_search(HANDLE, "scope", modalities="visual")
    assert env["error"] is None
    assert env["data"]["modalities_used"] == ["visual"]
    assert all(hit["evidence_type"] == "frame" for hit in env["data"]["hits"])


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": "   "}, "query"),
        ({"query": "x", "top_k": 0}, "top_k"),
        ({"query": "x", "top_k": 26}, "top_k"),
        ({"query": "x", "modalities": "audio"}, "modalities"),
        ({"query": "x", "temporal_window_s": -1}, "temporal_window_s"),
        ({"query": "x", "temporal_window_s": 121}, "temporal_window_s"),
    ],
)
def test_bad_compound_query_fails_before_component_search(monkeypatch, kwargs, message) -> None:
    monkeypatch.setattr(
        evidence,
        "corpus_search",
        lambda *_a, **_k: pytest.fail("component should not run"),
    )
    monkeypatch.setattr(
        evidence,
        "corpus_visual_search",
        lambda *_a, **_k: pytest.fail("component should not run"),
    )
    env = evidence.corpus_evidence_search(HANDLE, **kwargs)
    assert env["error"]["code"] == "bad_evidence_query"
    assert message in env["error"]["message"]
