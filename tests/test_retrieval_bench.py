"""The retrieval benchmark emits Recall@k and temporal-window metrics offline."""

from __future__ import annotations

import pytest

from bench import retrieval_metrics as m
from bench import retrieval_suite
from bench.retrieval_fixture import GoldWindow

GOLD = [GoldWindow("vid00000001", 100.0, 106.0), GoldWindow("vid00000002", 40.0, 46.0)]


def _hit(video, start, end, cited=None):
    hit = {"video_id": video, "start_s": start, "end_s": end}
    if cited is not None:
        hit["cited_start_s"] = cited
    return hit


def test_recall_success_and_reciprocal_rank() -> None:
    hits = [_hit("other000001", 0, 60), _hit("vid00000001", 90, 150), _hit("vid00000002", 0, 30)]
    assert m.recall_at_k(hits, GOLD, 1) == 0.0
    assert m.recall_at_k(hits, GOLD, 2) == 0.5
    assert m.recall_at_k(hits, GOLD, 3) == 0.5  # [0, 30] misses [40, 46]
    assert m.recall_at_k(hits, GOLD, 3, tol=10.0) == 1.0
    assert m.success_at_k(hits, GOLD, 2) == 1.0
    assert m.reciprocal_rank(hits, GOLD) == 0.5


def test_temporal_metrics_use_the_cited_timestamp() -> None:
    chunk_start_only = [_hit("vid00000001", 90, 150)]
    anchored = [_hit("vid00000001", 90, 150, cited=100.0)]
    assert m.temporal_hit_at_k(chunk_start_only, GOLD, 1) == 0.0
    assert m.temporal_error_s(chunk_start_only, GOLD) == 10.0
    assert m.temporal_hit_at_k(anchored, GOLD, 1) == 1.0
    assert m.temporal_error_s(anchored, GOLD) == 0.0
    assert m.window_iou(chunk_start_only, GOLD) == pytest.approx(6.0 / 60.0)
    assert m.temporal_error_s([_hit("x", 0, 1)], GOLD) is None


def test_mean_metrics_ignores_missing_values() -> None:
    rows = [{"recall@5": 1.0, "temporal_error_s": None}, {"recall@5": 0.0, "temporal_error_s": 4.0}]
    assert m.mean_metrics(rows) == {"recall@5": 0.5, "temporal_error_s": 4.0}


def test_recall_requires_gold() -> None:
    with pytest.raises(ValueError):
        m.recall_at_k([], [], 5)


def test_suite_emits_per_mode_and_stratum_metrics(tmp_path, monkeypatch) -> None:
    report = retrieval_suite.run_suite(root=tmp_path, setattr_fn=monkeypatch.setattr, hours=4.0)

    assert report["benchmark"] == "youtube-mcp-retrieval"
    assert set(report["modes"]) == {"lexical", "hybrid"}
    for summary in report["modes"].values():
        overall = summary["overall"]
        for key in ("recall@1", "recall@5", "recall@10", "success@5", "reciprocal_rank",
                    "temporal_hit@1", "temporal_hit@5", "temporal_error_s", "window_iou"):
            assert key in overall, key
        assert len(summary["strata"]) == 12
        assert summary["latency_ms"]["p95"] >= summary["latency_ms"]["p50"] >= 0
    assert report["threshold_violations"] == []
    # Hybrid recovers the paraphrase stratum that lexical retrieval cannot.
    assert report["modes"]["lexical"]["strata"]["concept_paraphrase"]["recall@5"] == 0.0
    assert report["modes"]["hybrid"]["strata"]["concept_paraphrase"]["recall@5"] == 1.0
    markdown = retrieval_suite.render_markdown(report)
    assert "| hybrid |" in markdown and "concept_paraphrase" in markdown


def test_threshold_violations_are_reported() -> None:
    report = {"modes": {"lexical": {"overall": {"recall@5": 0.4}, "strata": {}}}}
    thresholds = {"modes": {"lexical": {"overall": {"recall@5": 0.9}, "error_code": {"recall@5": 1.0}}}}
    assert retrieval_suite.check_thresholds(report, thresholds) == [
        "lexical/overall/recall@5: 0.4 < 0.9",
        "lexical/error_code/recall@5: None < 1.0",
    ]
