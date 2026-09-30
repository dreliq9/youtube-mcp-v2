"""v0.4 hybrid evidence search: hit contract, filters, exact-identifier fusion,
revision reproducibility, the vector seam, and 100-hour scale.

All deterministic and offline: corpora are synthetic and the dense model is the
feature-hashing stand-in from `bench/retrieval_fixture.py`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

import pytest

from bench import retrieval_fixture
from bench.retrieval_fixture import HashingEmbedder, build_corpus
from youtube_mcp_v2 import (
    cache,
    corpus_index,
    embeddings,
    hybrid_index,
    skeleton,
    vector_search,
)
from youtube_mcp_v2.tools import corpus as corpus_tools


@pytest.fixture
def storage(tmp_path, monkeypatch):
    retrieval_fixture.configure_storage(tmp_path, monkeypatch.setattr)
    return tmp_path


@pytest.fixture
def small_corpus(storage):
    return build_corpus(videos=36, video_duration_s=600.0)


def _case(corpus, stratum):
    return next(case for case in corpus.cases if case.stratum == stratum)


def _gold_hit(hits, case):
    return [
        hit for hit in hits
        if any(
            hit["video_id"] == g.video_id and hit["start_s"] <= g.start_s and hit["end_s"] >= g.end_s
            for g in case.gold
        )
    ]


def _use_embedder(monkeypatch, backend):
    monkeypatch.setattr(embeddings, "resolve_backend", lambda *_a, **_k: backend)


# --------------------------------------------------------------------------- #
# Public contract


def test_lexical_hit_contract_carries_evidence_provenance(small_corpus) -> None:
    case = _case(small_corpus, "product_identifier")
    env = corpus_tools.corpus_search(small_corpus.handle, case.query, top_k=5, semantic="off")
    data = env["data"]
    assert data is not None, env
    assert data["corpus_revision"] == small_corpus.handle
    assert data["index_revision"].startswith("idx1-")
    assert "corpus_manifest_sha256" in data
    retrieval = data["retrieval"]
    assert retrieval["class"] == "lexical"
    assert retrieval["chunker"] == corpus_index.CHUNKER_ID
    assert retrieval["top_k"] == 5

    hit = data["hits"][0]
    assert _gold_hit([hit], case)
    for key in (
        "rank", "evidence_id", "video_id", "url", "start_s", "end_s", "excerpt", "scores",
        "chunk", "transcript", "anchor", "corpus_revision", "index_revision",
    ):
        assert key in hit, key
    assert hit["evidence_id"].startswith("ev_")
    assert hit["url"].startswith(f"https://www.youtube.com/watch?v={hit['video_id']}&t=")
    assert set(hit["scores"]) >= {"lexical", "semantic", "hybrid", "exact_match"}
    assert hit["scores"]["exact_match"] is True
    assert hit["chunk"]["segment_range"][0] < hit["chunk"]["segment_range"][1]
    t = hit["transcript"]
    assert {"revision_id", "sha256", "validation_status", "acquisition", "is_generated"} <= set(t)
    assert t["validation_status"] == "pass"
    assert t["acquisition"]["method"] in {"captions", "local_stt"}
    # The anchor cites the exact matching source segment inside the chunk.
    anchor = hit["anchor"]
    assert anchor["basis"] == "exact"
    assert any(g.video_id == hit["video_id"] and g.start_s == anchor["start_s"] for g in case.gold)
    assert anchor["url"].endswith(f"&t={int(anchor['start_s'])}s")


def test_hybrid_hits_report_per_method_scores_without_backend_internals(
    small_corpus, monkeypatch
) -> None:
    _use_embedder(monkeypatch, HashingEmbedder())
    case = _case(small_corpus, "concept_paraphrase")
    data = corpus_tools.corpus_search(small_corpus.handle, case.query, top_k=5)["data"]
    assert data["backend"] == "hybrid"
    assert data["fusion"]["policy"] == hybrid_index.FUSION_POLICY
    assert data["retrieval"]["vector_search"] == vector_search.DEFAULT_VECTOR_SEARCH
    assert _gold_hit(data["hits"], case)
    scores = data["hits"][0]["scores"]
    assert {"lexical", "semantic", "hybrid", "lexical_rank", "semantic_rank", "exact_rank"} <= set(scores)


def test_corpus_tool_input_schemas_hide_vector_backend_and_model_choices() -> None:
    import asyncio

    from youtube_mcp_v2.server import mcp

    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    search = tools["corpus.search"]
    params = set(search.input_schema["properties"])
    assert {"handle", "query", "top_k", "filters"} <= params
    corpus_schemas = json.dumps(
        [t.input_schema for name, t in tools.items() if name.startswith("corpus.")]
    ).lower()
    for leaked in ("fastembed", "onnx", "embedding", "vector", "model_id", "mlx", "whisper", "bge"):
        assert leaked not in corpus_schemas, leaked
    filters = search.input_schema["$defs"]["CorpusSearchFilters"]["properties"]
    assert set(filters) == set(corpus_index.FILTER_KEYS)


# --------------------------------------------------------------------------- #
# Filters


def test_filters_restrict_evidence_without_changing_scores(small_corpus) -> None:
    case = _case(small_corpus, "product_identifier")
    handle = small_corpus.handle
    corpus_index.prepare_index(handle)
    base = corpus_index.search_index(handle, case.query, top_k=10)
    gold_video = case.gold[0].video_id

    only = corpus_index.search_index(handle, case.query, filters={"video_ids": [gold_video]})
    assert {h["video_id"] for h in only["hits"]} == {gold_video}
    same = next(h for h in base["hits"] if h["video_id"] == gold_video)
    assert only["hits"][0]["scores"]["lexical_raw"] == same["scores"]["lexical_raw"]
    assert only["retrieval"]["filters"] == {"video_ids": [gold_video]}

    excluded = corpus_index.search_index(handle, case.query, filters={"exclude_video_ids": [gold_video]})
    assert gold_video not in {h["video_id"] for h in excluded["hits"]}

    channel = same["channel"]
    by_channel = corpus_index.search_index(handle, case.query, filters={"channels": [channel]})
    assert {h["channel"] for h in by_channel["hits"]} == {channel}

    window = [case.gold[0].start_s - 1, case.gold[0].end_s + 1]
    timed = corpus_index.search_index(handle, case.query, filters={"time_range_s": window})
    assert timed["hits"]
    assert all(h["end_s"] >= window[0] and h["start_s"] <= window[1] for h in timed["hits"])

    stt = corpus_index.search_index(handle, "the", top_k=50, filters={"acquisition_methods": ["local_stt"]})
    assert stt["hits"] and all(h["transcript"]["acquisition"]["method"] == "local_stt" for h in stt["hits"])
    generated = corpus_index.search_index(handle, "the", top_k=50, filters={"is_generated": False})
    assert all(h["transcript"]["is_generated"] is False for h in generated["hits"])


@pytest.mark.parametrize(
    "filters, message",
    [
        ({"model": "bge-small"}, "unknown filter key"),
        ({"video_ids": ["not a video id!"]}, "invalid video id"),
        ({"time_range_s": [30, 10]}, "time_range_s"),
        ({"acquisition_methods": ["cloud"]}, "acquisition_methods"),
        ({"validated_only": "yes"}, "boolean"),
    ],
)
def test_bad_filters_are_structured_errors(small_corpus, filters, message) -> None:
    env = corpus_tools.corpus_search(small_corpus.handle, "anything", filters=filters, semantic="off")
    assert env["data"] is None
    assert env["error"]["code"] == "bad_filters"
    assert message in env["error"]["message"]


# --------------------------------------------------------------------------- #
# Exact identifiers survive fusion; legacy indexes keep their ranking


@dataclass
class _Adversarial:
    """Dense model that prefers every chunk *except* the exact identifier."""

    model_id: str = "fixture/adversarial-v1"

    def _vec(self, text: str) -> list[float]:
        return [0.0, 1.0] if "TPS62132-Q1" in text else [1.0, 0.0]

    def embed_passages(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


def _identifier_corpus() -> str:
    handle = "topic-exact-fusion-20260901-000000"
    videos = []
    rows = {
        "aaaaaaaaaa1": "Long review. " + "general discussion of the board layout " * 12
        + "the part is TPS62132-Q1 on the left " + "more filler about thermals " * 12,
        "aaaaaaaaaa2": "TPS62132 TPS62132 TPS62132 family Q1 Q1 Q1 overview",
        "aaaaaaaaaa3": "TPS62132 variants and Q1 automotive grade compared",
        "aaaaaaaaaa4": "TPS62132 buck in a Q1 rated enclosure",
    }
    for vid, text in rows.items():
        cache.put_transcript(
            video_id=vid, lang="en", actual_lang="en", is_generated=False, text=text,
            segments=[{"start_s": 5.0, "duration_s": 30.0, "end_s": 35.0, "text": text}],
            word_count=len(text.split()), validated=True, warnings=[],
        )
        videos.append({"id": vid, "title": vid, "channel": "Lab"})
    skeleton.save_skeleton({
        "handle": handle, "target": "topic", "value": "exact", "built_at": "2026-09-01T00:00:00Z",
        "expired_at": None, "source": "fixture", "videos": videos,
    })
    return handle


def test_exact_identifier_is_not_buried_by_fusion_and_legacy_policy_is_preserved(storage) -> None:
    handle = _identifier_corpus()
    backend = _Adversarial()
    prepared = hybrid_index.prepare_hybrid_index(handle, backend=backend)
    result = hybrid_index.search_hybrid_index(
        handle, "TPS62132-Q1", backend=backend, index_revision=prepared.index_revision
    )
    top = result["hits"][0]
    assert top["video_id"] == "aaaaaaaaaa1"
    assert top["scores"]["exact_match"] is True and top["scores"]["exact_rank"] == 1
    assert result["fusion"]["lists"] == ["lexical", "semantic", "exact"]

    # Simulate an index pinned before rrf-exact-v1 existed: no fusion meta key.
    conn = sqlite3.connect(prepared.path)
    with conn:
        conn.execute("DELETE FROM meta WHERE key = 'fusion'")
    conn.close()
    legacy = hybrid_index.search_hybrid_index(
        handle, "TPS62132-Q1", backend=backend, index_revision=prepared.index_revision
    )
    assert legacy["fusion"]["policy"] == hybrid_index.LEGACY_FUSION_POLICY
    assert legacy["fusion"]["lists"] == ["lexical", "semantic"]
    assert legacy["hits"][0]["video_id"] != "aaaaaaaaaa1"  # what rrf-exact fixes


def test_unknown_vector_search_implementation_fails_closed(storage) -> None:
    handle = _identifier_corpus()
    backend = _Adversarial()
    prepared = hybrid_index.prepare_hybrid_index(handle, backend=backend)
    conn = sqlite3.connect(prepared.path)
    with conn:
        conn.execute("UPDATE meta SET value = 'hnsw-future-v9' WHERE key = 'vector_search'")
    conn.close()
    with pytest.raises(hybrid_index.HybridIndexError, match="hnsw-future-v9"):
        hybrid_index.search_hybrid_index(
            handle, "TPS62132-Q1", backend=backend, index_revision=prepared.index_revision
        )


def test_registered_vector_search_is_used_by_new_indexes(storage, monkeypatch) -> None:
    calls = []

    class Recording(vector_search.ExactScan):
        name = "recording-scan-v1"

        def similarities(self, conn, query_vector, *, allowed):
            calls.append(len(query_vector))
            return super().similarities(conn, query_vector, allowed=allowed)

    monkeypatch.setitem(vector_search._REGISTRY, Recording.name, Recording())
    monkeypatch.setattr(vector_search, "DEFAULT_VECTOR_SEARCH", Recording.name)
    handle = _identifier_corpus()
    backend = _Adversarial()
    prepared = hybrid_index.prepare_hybrid_index(handle, backend=backend)
    result = hybrid_index.search_hybrid_index(
        handle, "board layout", backend=backend, index_revision=prepared.index_revision
    )
    assert calls == [2]
    assert result["retrieval"]["vector_search"] == Recording.name


def test_schema1_index_without_sources_still_searches_but_rejects_acquisition_filter(storage) -> None:
    handle = _identifier_corpus()
    prepared = corpus_index.prepare_index(handle)
    conn = sqlite3.connect(prepared.path)
    with conn:
        conn.execute("DROP TABLE sources")
    conn.close()
    result = corpus_index.search_index(handle, "TPS62132-Q1", index_revision=prepared.index_revision)
    assert result["hits"][0]["transcript"]["acquisition"] is None
    with pytest.raises(corpus_index.CorpusIndexError, match="acquisition"):
        corpus_index.search_index(
            handle, "TPS62132-Q1", index_revision=prepared.index_revision,
            filters={"acquisition_methods": ["captions"]},
        )


def test_corpus_revision_manifest_digest_is_echoed(storage) -> None:
    handle = "topic-manifested-20260901-000000"
    cache.put_transcript(
        video_id="bbbbbbbbbb1", lang="en", actual_lang="en", is_generated=False,
        text="manifest seam", segments=[{"start_s": 0.0, "duration_s": 2.0, "text": "manifest seam"}],
        word_count=2, validated=True, warnings=[],
    )
    skeleton.save_skeleton({
        "handle": handle, "target": "topic", "value": "m", "built_at": "2026-09-01T00:00:00Z",
        "expired_at": None, "source": "fixture", "videos": [{"id": "bbbbbbbbbb1"}],
        "manifest_sha256": "f" * 64,
    })
    prepared = corpus_index.prepare_index(handle)
    assert prepared.corpus_manifest_sha256 == "f" * 64
    result = corpus_index.search_index(handle, "manifest")
    assert result["corpus_manifest_sha256"] == "f" * 64
    assert result["coverage"]["corpus_manifest_sha256"] == "f" * 64


# --------------------------------------------------------------------------- #
# Chunking


def test_chunker_closes_on_sentence_or_pause_and_keeps_segment_timings() -> None:
    segments = [
        {"start_s": float(i * 5), "duration_s": 5.0, "text": ("word " * 30).strip() + ("." if i == 3 else "")}
        for i in range(12)
    ]
    chunks = corpus_index.chunk_segments(segments, chunk_tokens=200, chunk_overlap=0)
    assert chunks[0]["segment_end"] == 4  # closed after the sentence end, not at the budget
    legacy = corpus_index.chunk_segments(segments, chunk_tokens=200, chunk_overlap=0, boundary_aware=False)
    assert legacy[0]["segment_end"] > 4
    for chunk in chunks:
        assert chunk["start_s"] == segments[chunk["segment_start"]]["start_s"]
        times = chunk["segment_times"]
        assert len(times) == chunk["segment_end"] - chunk["segment_start"]
        assert [t[1] for t in times] == [s["start_s"] for s in segments[chunk["segment_start"]:chunk["segment_end"]]]


def test_pause_is_a_boundary() -> None:
    segments = [{"start_s": 0.0, "duration_s": 5.0, "text": "alpha " * 40}]
    segments.append({"start_s": 5.0, "duration_s": 5.0, "text": "beta " * 40})
    segments.append({"start_s": 20.0, "duration_s": 5.0, "text": "gamma " * 40})
    chunks = corpus_index.chunk_segments(segments, chunk_tokens=160, chunk_overlap=0)
    assert chunks[0]["segment_end"] == 2 and chunks[1]["start_s"] == 20.0


# --------------------------------------------------------------------------- #
# Acceptance: frozen revision A reproducible after B exists


def test_frozen_revision_a_reproduces_after_refresh_backfill_and_revision_b(storage) -> None:
    corpus = build_corpus(videos=36, video_duration_s=600.0)
    handle_a = corpus.handle
    backend = HashingEmbedder()
    lexical_a = corpus_index.prepare_index(handle_a)
    hybrid_a = hybrid_index.prepare_hybrid_index(handle_a, backend=backend)
    queries = [case.query for case in corpus.cases]
    before_lex = [corpus_index.search_index(handle_a, q, index_revision=lexical_a.index_revision) for q in queries]
    before_hyb = [
        hybrid_index.search_hybrid_index(handle_a, q, backend=backend, index_revision=hybrid_a.index_revision)
        for q in queries
    ]

    # Refresh one captioned transcript and backfill another via local STT.
    refreshed, backfilled = corpus.video_ids[0], corpus.video_ids[2]
    for vid, method in ((refreshed, "captions"), (backfilled, "local_stt")):
        text = "Revised narration mentions TPS62132-Q1 and the ERR_CONN_RESET fix."
        cache.put_transcript(
            video_id=vid, lang="en", actual_lang="en", is_generated=(method == "local_stt"),
            text=text, segments=[{"start_s": 1.0, "duration_s": 9.0, "end_s": 10.0, "text": text}],
            word_count=len(text.split()), validated=True, warnings=[],
            provider="mlx-whisper" if method == "local_stt" else "youtube-transcript-api",
            method=method, attempts=[], details={"engine": "mlx-whisper"} if method == "local_stt" else None,
        )
    handle_b = "topic-synthetic-evidence-20260915-000000"
    members_a = skeleton.load_skeleton(handle_a)["videos"]
    skeleton.save_skeleton({
        **skeleton.load_skeleton(handle_a), "handle": handle_b, "built_at": "2026-09-15T00:00:00Z",
        "videos": members_a[:30],
    })
    lexical_b = corpus_index.prepare_index(handle_b)
    hybrid_b = hybrid_index.prepare_hybrid_index(handle_b, backend=backend)
    assert lexical_b.index_revision != lexical_a.index_revision
    assert hybrid_b.index_revision != hybrid_a.index_revision
    # Re-preparing A now sees newer transcript revisions => a *new* index revision.
    assert corpus_index.prepare_index(handle_a).index_revision != lexical_a.index_revision

    after_lex = [corpus_index.search_index(handle_a, q, index_revision=lexical_a.index_revision) for q in queries]
    after_hyb = [
        hybrid_index.search_hybrid_index(handle_a, q, backend=backend, index_revision=hybrid_a.index_revision)
        for q in queries
    ]
    assert [r["hits"] for r in after_lex] == [r["hits"] for r in before_lex]
    assert [r["hits"] for r in after_hyb] == [r["hits"] for r in before_hyb]

    in_b = corpus_index.search_index(handle_b, "ERR_CONN_RESET fix", index_revision=lexical_b.index_revision)
    revised = [h for h in in_b["hits"] if h["video_id"] in {refreshed, backfilled}]
    assert {h["video_id"] for h in revised} == {refreshed, backfilled}
    methods = {h["video_id"]: h["transcript"]["acquisition"]["method"] for h in revised}
    assert methods[backfilled] == "local_stt"
    assert all(h["corpus_revision"] == handle_b for h in in_b["hits"])


# --------------------------------------------------------------------------- #
# Acceptance: >= 100 hours searchable without loading transcripts into context


def test_hundred_hour_corpus_returns_bounded_timestamped_evidence(storage) -> None:
    started = time.perf_counter()
    corpus = build_corpus(videos=100, video_duration_s=3660.0, segment_s=30.0, words_per_segment=60)
    lexical = corpus_index.prepare_index(corpus.handle)
    coverage = lexical.as_dict()
    assert coverage["indexed_hours"] >= 100
    backend = HashingEmbedder()
    hybrid = hybrid_index.prepare_hybrid_index(corpus.handle, backend=backend)

    total_transcript_bytes = sum(
        len(cache.get_transcript(vid, "en")[0]["text"]) for vid in corpus.video_ids
    )
    for stratum in ("product_identifier", "legal_identifier", "error_code", "concept_paraphrase"):
        case = _case(corpus, stratum)
        results = [
            hybrid_index.search_hybrid_index(
                corpus.handle, case.query, backend=backend, top_k=10, index_revision=hybrid.index_revision
            )
        ]
        if stratum != "concept_paraphrase":
            results.append(
                corpus_index.search_index(corpus.handle, case.query, top_k=10, index_revision=lexical.index_revision)
            )
        for result in results:
            assert _gold_hit(result["hits"][:5], case), (stratum, result["backend"])
            size = len(json.dumps(result))
            # Only bounded excerpts come back (~2 wps speech => ~4.6 MB of text).
            assert size < 64_000 and size < total_transcript_bytes / 100
    assert time.perf_counter() - started < 60
