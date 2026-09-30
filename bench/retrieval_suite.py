"""Evidence-retrieval benchmark: Recall@k and temporal-window metrics.

    python bench/retrieval_suite.py                 # deterministic, offline
    python bench/retrieval_suite.py --hours 100     # scale run (>= 100 hours)
    python bench/retrieval_suite.py --embedder local  # add the configured local
                                                      # dense model if cached

Builds the synthetic multi-domain corpus from `bench/retrieval_fixture.py` in an
isolated temporary cache, prepares immutable lexical and hybrid indexes, runs
every annotated query, and writes `bench/results/retrieval.json` + `.md`.
Metrics are reported per retrieval mode and per domain stratum; thresholds in
`bench/retrieval_thresholds.json` gate the exit status. There is deliberately
no single aggregate score.

The default hybrid mode uses a deterministic hashing embedder, which measures
indexing/fusion mechanics and exact-identifier preservation, not the semantic
quality of a real model.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench import retrieval_fixture, retrieval_metrics  # noqa: E402

BENCHMARK_ID = "youtube-mcp-retrieval"
BENCHMARK_VERSION = "0.1"
KS = (1, 5, 10)
TOLERANCE_S = 0.5
THRESHOLDS_PATH = Path(__file__).with_name("retrieval_thresholds.json")


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[index]


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_stratum: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_stratum.setdefault(row["stratum"], []).append(row["metrics"])
    latencies = [row["latency_ms"] for row in rows]
    return {
        "queries": len(rows),
        "overall": retrieval_metrics.mean_metrics([row["metrics"] for row in rows]),
        "strata": {name: retrieval_metrics.mean_metrics(items) for name, items in sorted(by_stratum.items())},
        "latency_ms": {"p50": _percentile(latencies, 50), "p95": _percentile(latencies, 95)},
        "max_response_bytes": max((row["response_bytes"] for row in rows), default=0),
    }


def _cited(hit: dict[str, Any]) -> dict[str, Any]:
    """Hit span for overlap metrics plus the timestamp a user would jump to."""
    anchor = hit.get("anchor") or {}
    return {
        "video_id": hit["video_id"],
        "start_s": hit["start_s"],
        "end_s": hit["end_s"],
        "cited_start_s": anchor.get("start_s", hit["start_s"]),
    }


def _run_mode(
    corpus: retrieval_fixture.SyntheticCorpus,
    search: Callable[[str], dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for case in corpus.cases:
        started = time.perf_counter()
        result = search(case.query)
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        hits = result["hits"]
        rows.append(
            {
                "case_id": case.case_id,
                "stratum": case.stratum,
                "query": case.query,
                "gold": [vars(window) for window in case.gold],
                "top_hits": [
                    {"video_id": h["video_id"], "start_s": h["start_s"], "end_s": h["end_s"],
                     "cited_start_s": h.get("anchor", {}).get("start_s")}
                    for h in hits[:3]
                ],
                "metrics": retrieval_metrics.score_query(
                    [_cited(h) for h in hits], case.gold, ks=KS, tol=TOLERANCE_S
                ),
                "latency_ms": latency_ms,
                "response_bytes": len(json.dumps(result)),
            }
        )
    return rows


def check_thresholds(report: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    violations = []
    for mode, gates in thresholds.get("modes", {}).items():
        summary = report["modes"].get(mode)
        if summary is None:
            continue
        for scope, metrics in gates.items():
            values = summary["overall"] if scope == "overall" else summary["strata"].get(scope, {})
            for metric, minimum in metrics.items():
                actual = values.get(metric)
                if actual is None or actual < minimum:
                    violations.append(f"{mode}/{scope}/{metric}: {actual} < {minimum}")
    return violations


def run_suite(
    *,
    root: Path,
    setattr_fn: Callable[[Any, str, Any], None] = setattr,
    hours: float = 8.0,
    embedder: str = "fixture",
    top_k: int = 10,
) -> dict[str, Any]:
    from youtube_mcp_v2 import corpus_index, embeddings, hybrid_index

    retrieval_fixture.configure_storage(root, setattr_fn)
    video_duration_s = 1200.0
    videos = max(len(retrieval_fixture.NEEDLES) * 3, round(hours * 3600.0 / video_duration_s))
    started = time.perf_counter()
    corpus = retrieval_fixture.build_corpus(videos=videos, video_duration_s=video_duration_s)
    lexical = corpus_index.prepare_index(corpus.handle)
    build_s = {"transcripts_and_lexical": round(time.perf_counter() - started, 2)}

    backends: dict[str, Any] = {"hybrid": retrieval_fixture.HashingEmbedder()}
    if embedder == "local":
        local = embeddings.resolve_backend("auto")
        if local is not None:
            backends["hybrid-local"] = local

    modes: dict[str, list[dict[str, Any]]] = {
        "lexical": _run_mode(
            corpus,
            lambda q: corpus_index.search_index(
                corpus.handle, q, top_k=top_k, index_revision=lexical.index_revision
            ),
        )
    }
    indexes = {"lexical": lexical.as_dict()}
    for mode, backend in backends.items():
        started = time.perf_counter()
        prepared = hybrid_index.prepare_hybrid_index(corpus.handle, backend=backend)
        build_s[mode] = round(time.perf_counter() - started, 2)
        indexes[mode] = prepared.as_dict()
        modes[mode] = _run_mode(
            corpus,
            lambda q, b=backend, rev=prepared.index_revision: hybrid_index.search_hybrid_index(
                corpus.handle, q, backend=b, top_k=top_k, index_revision=rev
            ),
        )

    report = {
        "benchmark": BENCHMARK_ID,
        "version": BENCHMARK_VERSION,
        "params": {"ks": list(KS), "tolerance_s": TOLERANCE_S, "top_k": top_k, "embedder": embedder},
        "corpus": {
            "handle": corpus.handle,
            "videos": len(corpus.video_ids),
            "hours": round(corpus.duration_s / 3600.0, 2),
            "segments": corpus.segment_count,
            "chunks": lexical.chunk_count,
            "indexes": indexes,
        },
        "build_s": build_s,
        "modes": {mode: _summarize(rows) for mode, rows in modes.items()},
        "cases": {mode: rows for mode, rows in modes.items()},
    }
    thresholds = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    report["threshold_violations"] = check_thresholds(report, thresholds)
    return report


def render_markdown(report: dict[str, Any]) -> str:
    corpus = report["corpus"]
    lines = [
        f"# {report['benchmark']} {report['version']}",
        "",
        f"Corpus: {corpus['videos']} videos, {corpus['hours']} h, {corpus['chunks']} chunks.",
        "",
        "| mode | recall@1 | recall@5 | recall@10 | MRR | temporal_hit@1 | temporal_error_s | p95 ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for mode, summary in report["modes"].items():
        o = summary["overall"]
        lines.append(
            f"| {mode} | {o['recall@1']} | {o['recall@5']} | {o['recall@10']} | "
            f"{o['reciprocal_rank']} | {o['temporal_hit@1']} | {o['temporal_error_s']} | "
            f"{summary['latency_ms']['p95']} |"
        )
    lines += ["", "## Strata (recall@5 by mode)", "", "| stratum | " + " | ".join(report["modes"]) + " |",
              "|---|" + "---|" * len(report["modes"])]
    strata = sorted({s for summary in report["modes"].values() for s in summary["strata"]})
    for stratum in strata:
        cells = [str(report["modes"][m]["strata"].get(stratum, {}).get("recall@5")) for m in report["modes"]]
        lines.append(f"| {stratum} | " + " | ".join(cells) + " |")
    lines += ["", "Threshold violations: " + (", ".join(report["threshold_violations"]) or "none"), ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the evidence-retrieval benchmark")
    parser.add_argument("--hours", type=float, default=8.0, help="synthetic corpus size in hours")
    parser.add_argument("--embedder", choices=["fixture", "local"], default="fixture")
    parser.add_argument("--output-dir", default=str(Path(__file__).with_name("results")))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ytmcp-retrieval-") as tmp:
        report = run_suite(root=Path(tmp), hours=args.hours, embedder=args.embedder)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "retrieval.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "retrieval.md").write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report))
    return 1 if report["threshold_violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
