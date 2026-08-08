from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from youtube_mcp_v2 import cache, corpus_index, embeddings, hybrid_index, skeleton
from youtube_mcp_v2.tools import corpus as corpus_tools


HANDLE = "topic-hybrid-search-20260806-112233"
VID_A = "jNQXAC9IVRw"
VID_B = "dQw4w9WgXcQ"


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


def _save_corpus() -> None:
    skeleton.save_skeleton(
        {
            "handle": HANDLE,
            "target": "topic",
            "value": "hybrid retrieval",
            "built_at": "2026-08-06T11:22:33Z",
            "expired_at": None,
            "source": "scrape",
            "videos": [
                {"id": VID_A, "title": "Power Transients", "channel": "Lab A"},
                {"id": VID_B, "title": "Mechanical Notes", "channel": "Lab B"},
            ],
        }
    )


def _put(video_id: str, text: str, *, start_s: float) -> None:
    cache.put_transcript(
        video_id=video_id,
        lang="en",
        actual_lang="en",
        is_generated=False,
        text=text,
        segments=[
            {
                "start_s": start_s,
                "duration_s": 5.0,
                "end_s": start_s + 5.0,
                "text": text,
            }
        ],
        word_count=len(text.split()),
        validated=True,
        warnings=[],
    )


def _fixture(tmp_path: Path, monkeypatch) -> None:
    _configure_storage(tmp_path, monkeypatch)
    _save_corpus()
    _put(
        VID_A,
        "The TPS62132-Q1 stage showed temporary overshoot above the rated power ceiling.",
        start_s=12.0,
    )
    _put(
        VID_B,
        "The mechanical enclosure dimensions and fastener spacing are shown here.",
        start_s=55.0,
    )


@dataclass
class FakeSemanticBackend:
    model_id: str = "fixture/semantic-v1"
    query_dim: int = 3

    def _passage_vector(self, text: str) -> list[float]:
        lowered = text.lower()
        if "overshoot" in lowered or "rated power" in lowered:
            return [1.0, 0.0, 0.0]
        if "mechanical" in lowered or "enclosure" in lowered:
            return [0.0, 1.0, 0.0]
        if "gallium nitride" in lowered:
            return [0.0, 0.0, 1.0]
        return [0.3, 0.3, 0.4]

    def embed_passages(self, texts):
        return [self._passage_vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        lowered = text.lower()
        if "brief excursion" in lowered or "momentary surge" in lowered:
            return [1.0, 0.0, 0.0][: self.query_dim]
        if "tps62132-q1" in lowered:
            # Intentionally favor the *wrong* semantic document. Lexical retrieval
            # must preserve the exact identifier in the fused ranking.
            return [0.0, 1.0, 0.0][: self.query_dim]
        if "gallium" in lowered or "gan" in lowered:
            return [0.0, 0.0, 1.0][: self.query_dim]
        return [0.3, 0.3, 0.4][: self.query_dim]


def test_semantic_only_paraphrase_surfaces_without_lexical_overlap(
    tmp_path, monkeypatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    backend = FakeSemanticBackend()
    prepared = hybrid_index.prepare_hybrid_index(HANDLE, backend=backend)

    result = hybrid_index.search_hybrid_index(
        HANDLE,
        "brief excursion past limit",
        backend=backend,
        index_revision=prepared.index_revision,
    )

    assert result["backend"] == "hybrid"
    assert result["model_id"] == backend.model_id
    assert result["hits"][0]["video_id"] == VID_A
    assert result["hits"][0]["scores"]["lexical"] == 0.0
    assert result["hits"][0]["scores"]["semantic"] == pytest.approx(1.0)
    assert result["hits"][0]["start_s"] == 12.0


def test_exact_part_number_survives_semantic_disagreement(tmp_path, monkeypatch) -> None:
    _fixture(tmp_path, monkeypatch)
    backend = FakeSemanticBackend()
    prepared = hybrid_index.prepare_hybrid_index(HANDLE, backend=backend)

    result = hybrid_index.search_hybrid_index(
        HANDLE,
        "TPS62132-Q1",
        backend=backend,
        index_revision=prepared.index_revision,
    )

    assert result["hits"][0]["video_id"] == VID_A
    assert result["hits"][0]["scores"]["lexical_rank"] == 1
    # The deliberately wrong semantic preference proves lexical evidence affects
    # the final fused order instead of dense similarity replacing it.
    by_video = {hit["video_id"]: hit for hit in result["hits"]}
    assert by_video[VID_B]["scores"]["semantic_rank"] == 1
    assert by_video[VID_A]["scores"]["hybrid"] > by_video[VID_B]["scores"]["hybrid"]


def test_model_identity_changes_hybrid_index_revision(tmp_path, monkeypatch) -> None:
    _fixture(tmp_path, monkeypatch)
    first = hybrid_index.prepare_hybrid_index(
        HANDLE, backend=FakeSemanticBackend(model_id="fixture/model-a")
    )
    second = hybrid_index.prepare_hybrid_index(
        HANDLE, backend=FakeSemanticBackend(model_id="fixture/model-b")
    )
    assert first.index_revision != second.index_revision
    assert first.model_id == "fixture/model-a"
    assert second.model_id == "fixture/model-b"


def test_pinned_hybrid_index_keeps_old_transcript_evidence(tmp_path, monkeypatch) -> None:
    _fixture(tmp_path, monkeypatch)
    backend = FakeSemanticBackend()
    old = hybrid_index.prepare_hybrid_index(HANDLE, backend=backend)

    _put(
        VID_A,
        "The replacement transcript discusses a gallium nitride stage instead.",
        start_s=12.0,
    )
    new = hybrid_index.prepare_hybrid_index(HANDLE, backend=backend)
    assert new.index_revision != old.index_revision

    old_hit = hybrid_index.search_hybrid_index(
        HANDLE,
        "brief excursion past limit",
        backend=backend,
        index_revision=old.index_revision,
    )["hits"][0]
    new_gan = hybrid_index.search_hybrid_index(
        HANDLE,
        "gallium nitride",
        backend=backend,
        index_revision=new.index_revision,
    )["hits"][0]

    assert old_hit["video_id"] == VID_A
    assert "temporary overshoot" in old_hit["excerpt"]
    assert new_gan["video_id"] == VID_A
    assert "gallium nitride" in new_gan["excerpt"]


def test_query_dimension_mismatch_fails_cleanly(tmp_path, monkeypatch) -> None:
    _fixture(tmp_path, monkeypatch)
    build_backend = FakeSemanticBackend(query_dim=3)
    prepared = hybrid_index.prepare_hybrid_index(HANDLE, backend=build_backend)
    bad_query_backend = FakeSemanticBackend(query_dim=2)

    with pytest.raises(hybrid_index.HybridIndexError, match="query embedding dim"):
        hybrid_index.search_hybrid_index(
            HANDLE,
            "brief excursion past limit",
            backend=bad_query_backend,
            index_revision=prepared.index_revision,
        )


def test_auto_semantic_mode_falls_back_to_lexical_without_local_backend(
    tmp_path, monkeypatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(embeddings, "resolve_backend", lambda *_a, **_k: None)

    env = corpus_tools.corpus_search(
        HANDLE,
        "TPS62132-Q1",
        semantic="auto",
        auto_prepare=True,
    )
    assert env["error"] is None
    assert env["data"]["backend"] == "lexical"
    assert env["data"]["hits"][0]["video_id"] == VID_A
    assert any("semantic retrieval is not locally available" in w for w in env["warnings"])


def test_required_semantic_mode_uses_backend_without_public_backend_argument(
    tmp_path, monkeypatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    backend = FakeSemanticBackend()
    monkeypatch.setattr(
        embeddings,
        "resolve_backend",
        lambda mode, **_kwargs: backend if mode in {"auto", "required"} else None,
    )

    prepared = corpus_tools.corpus_prepare(HANDLE, semantic="required")
    assert prepared["error"] is None
    assert prepared["data"]["backend"] == "hybrid"
    assert prepared["data"]["model_id"] == backend.model_id

    searched = corpus_tools.corpus_search(
        HANDLE,
        "brief excursion past limit",
        semantic="required",
    )
    assert searched["error"] is None
    assert searched["data"]["backend"] == "hybrid"
    assert searched["data"]["hits"][0]["video_id"] == VID_A


def test_auto_backend_resolution_never_upgrades_to_download(monkeypatch) -> None:
    calls = []

    class ProbeBackend:
        def __init__(self, *, model_id, local_files_only, cache_dir):
            calls.append((model_id, local_files_only, cache_dir))
            raise embeddings.EmbeddingUnavailable("fixture missing local model")

    monkeypatch.setattr(embeddings, "FastEmbedBackend", ProbeBackend)
    assert embeddings.resolve_backend("auto", model_id="fixture/model") is None
    assert calls[0][1] is True

    with pytest.raises(embeddings.EmbeddingUnavailable):
        embeddings.resolve_backend("required", model_id="fixture/model")
    assert calls[1][1] is False
