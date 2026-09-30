"""Case scoring and per-suite metric aggregation.

`expected` objects are *subsets* of adapter output (extra diagnostics never
change case semantics). Leaves may use small operators so annotations can carry
tolerances instead of brittle exact values:

    {"$approx": 212, "$tol": 2}    numeric within tolerance
    {"$gte": 1} / {"$lte": 5}      numeric bounds (may be combined)
    {"$in": ["a", "b"]}            one of
    {"$contains": ["x", "y"]}      list contains all / string contains all
    {"$present": true|false}       key present with a non-null value (or not)
    {"$type": "string"}            JSON type name
    {"$len": 3} / {"$len": {"$gte": 1}}

Scorers (case `scoring.method`):

* `subset` (default) — score = fraction of expected leaves satisfied.
* `metadata_fields` — subset; reports `field_accuracy`.
* `provenance` — subset + provenance completeness over the fields listed in
  BENCHMARK.md ("Provenance completeness score").
* `temporal_window` — ranked hits vs annotated evidence windows (+ tolerance);
  reports `temporal_hit`, `temporal_error_s`.
* `fault` — subset + server survival / structured-error / fallback checks.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

_OPERATORS = {"$approx", "$tol", "$gte", "$lte", "$in", "$contains", "$present", "$type", "$len"}

_JSON_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}

# Provenance fields from BENCHMARK.md. `None`/missing means "not exposed".
PROVENANCE_FIELDS = (
    "video_id",
    "provider",
    "method",
    "fetched_at",
    "requested_lang",
    "actual_lang",
    "transcript_type",
    "validation_state",
    "attempts",
    "content_hash",
)


def _is_operator(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and set(value) <= _OPERATORS


def _match_operator(actual: Any, spec: dict[str, Any], path: str) -> list[str]:
    problems: list[str] = []
    if "$present" in spec:
        present = actual is not None
        if present != bool(spec["$present"]):
            problems.append(f"{path}: expected present={spec['$present']}, got {actual!r}")
        return problems
    if "$type" in spec:
        py = _JSON_TYPES.get(spec["$type"])
        ok = py is not None and isinstance(actual, py) and not (
            spec["$type"] in ("number", "integer") and isinstance(actual, bool)
        )
        if not ok:
            problems.append(f"{path}: expected type {spec['$type']}, got {type(actual).__name__}")
    if "$approx" in spec:
        tol = float(spec.get("$tol", 0))
        if not isinstance(actual, (int, float)) or isinstance(actual, bool) or math.isnan(float(actual)):
            problems.append(f"{path}: expected ~{spec['$approx']}, got {actual!r}")
        elif abs(float(actual) - float(spec["$approx"])) > tol:
            problems.append(f"{path}: expected {spec['$approx']}±{tol}, got {actual!r}")
    for op, cmp in (("$gte", lambda a, b: a >= b), ("$lte", lambda a, b: a <= b)):
        if op in spec:
            if not isinstance(actual, (int, float)) or isinstance(actual, bool):
                problems.append(f"{path}: expected number {op} {spec[op]}, got {actual!r}")
            elif not cmp(actual, spec[op]):
                problems.append(f"{path}: expected {op} {spec[op]}, got {actual!r}")
    if "$in" in spec and actual not in spec["$in"]:
        problems.append(f"{path}: expected one of {spec['$in']!r}, got {actual!r}")
    if "$contains" in spec:
        wanted = spec["$contains"] if isinstance(spec["$contains"], list) else [spec["$contains"]]
        if isinstance(actual, (list, str)):
            missing = [w for w in wanted if w not in actual]
            if missing:
                problems.append(f"{path}: missing {missing!r}")
        else:
            problems.append(f"{path}: expected list/string, got {type(actual).__name__}")
    if "$len" in spec:
        if not hasattr(actual, "__len__"):
            problems.append(f"{path}: expected sized value, got {type(actual).__name__}")
        else:
            sub = spec["$len"]
            problems.extend(
                _match_operator(len(actual), sub, f"{path}.length")
                if _is_operator(sub)
                else ([] if len(actual) == sub else [f"{path}: expected length {sub}, got {len(actual)}"])
            )
    return problems


def match(actual: Any, expected: Any, path: str = "$") -> tuple[list[str], int]:
    """Return (mismatches, leaves_checked). Dicts are subset-matched."""
    if _is_operator(expected):
        return _match_operator(actual, expected, path), 1
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected object, got {type(actual).__name__}"], max(1, len(expected))
        mismatches: list[str] = []
        leaves = 0
        for key, value in expected.items():
            if key not in actual:
                if _is_operator(value) and value.get("$present") is False:
                    leaves += 1
                    continue
                mismatches.append(f"{path}.{key}: missing")
                leaves += 1
                continue
            sub_mismatches, sub_leaves = match(actual[key], value, f"{path}.{key}")
            mismatches.extend(sub_mismatches)
            leaves += sub_leaves
        return mismatches, leaves
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected list, got {type(actual).__name__}"], 1
        if len(actual) != len(expected):
            return [f"{path}: expected {expected!r}, got {actual!r}"], 1
        mismatches = []
        leaves = 0
        for i, (a, e) in enumerate(zip(actual, expected, strict=True)):
            sub_m, sub_l = match(a, e, f"{path}[{i}]")
            mismatches.extend(sub_m)
            leaves += sub_l
        return mismatches, max(1, leaves)
    if actual != expected:
        return [f"{path}: expected {expected!r}, got {actual!r}"], 1
    return [], 1


def expected_subset(actual: Any, expected: Any, path: str = "$") -> list[str]:
    """Backward-compatible helper returning only mismatch descriptions."""
    return match(actual, expected, path)[0]


@dataclass
class CaseScore:
    passed: bool
    score: float
    mismatches: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


def _subset_score(actual: Any, expected: dict[str, Any]) -> tuple[list[str], float]:
    mismatches, leaves = match(actual, expected)
    if leaves == 0:
        return mismatches, 1.0 if not mismatches else 0.0
    return mismatches, max(0.0, (leaves - len(mismatches)) / leaves)


def provenance_completeness(actual: Any) -> float:
    prov = actual.get("provenance") if isinstance(actual, dict) else None
    if not isinstance(prov, dict):
        return 0.0
    present = 0
    for name in PROVENANCE_FIELDS:
        value = prov.get(name)
        if value is not None and value != [] and value != "":
            present += 1
    return present / len(PROVENANCE_FIELDS)


def _window_distance(start: float, window: list[float], tol: float) -> float:
    """Seconds between a cited start timestamp and an annotated window.

    A hit lands when its *start* falls inside ``[window_start - tol,
    window_end + tol]``: jumping to the cited timestamp must put the viewer in
    the evidence, not merely somewhere whose span happens to overlap it.
    """
    lo, hi = float(window[0]) - tol, float(window[1]) + tol
    if lo <= start <= hi:
        return 0.0
    return lo - start if start < lo else start - hi


def score_case(case: dict[str, Any], actual: Any) -> CaseScore:
    scoring = case.get("scoring") or {}
    method = scoring.get("method", "subset")
    params = scoring.get("params", {})
    expected = case.get("expected", {})

    mismatches, score = _subset_score(actual, expected)
    metrics: dict[str, float] = {}
    passed = not mismatches

    if method == "subset":
        pass
    elif method == "metadata_fields":
        metrics["field_accuracy"] = score
    elif method == "provenance":
        completeness = provenance_completeness(actual)
        metrics["provenance_completeness"] = completeness
        minimum = float(params.get("min_completeness", 0.8))
        if completeness < minimum:
            mismatches.append(
                f"$.provenance: completeness {completeness:.2f} below {minimum:.2f}"
            )
            passed = False
        score = (score + completeness) / 2
    elif method == "temporal_window":
        windows = params.get("windows") or []
        tol = float(params.get("tolerance_s", 1.0))
        k = int(params.get("k", 1))
        hits = actual.get("hits", []) if isinstance(actual, dict) else []
        hit_ranks = [
            rank
            for rank, hit in enumerate(hits[:k], start=1)
            if any(
                _window_distance(float(hit["start_s"]), w, tol) == 0.0
                for w in windows
            )
        ]
        top_error = (
            min(
                _window_distance(float(hits[0]["start_s"]), w, tol)
                for w in windows
            )
            if hits and windows
            else None
        )
        metrics["temporal_hit"] = 1.0 if hit_ranks else 0.0
        metrics["reciprocal_rank"] = 1.0 / hit_ranks[0] if hit_ranks else 0.0
        if top_error is not None:
            metrics["temporal_error_s"] = top_error
            # Precision of the rank-1 citation: distance to the nearest window start.
            metrics["start_offset_s"] = round(
                min(abs(float(hits[0]["start_s"]) - float(w[0])) for w in windows), 3
            )
        if not hit_ranks:
            mismatches.append(
                f"$.hits: no hit within top-{k} inside {windows} ±{tol}s"
                + (f" (rank-1 error {top_error:.2f}s)" if top_error is not None else " (no hits)")
            )
            passed = False
        score = (score + metrics["temporal_hit"]) / 2
    elif method == "fault":
        survived = isinstance(actual, dict) and actual.get("exception") is None
        metrics["survived"] = 1.0 if survived else 0.0
        if not survived:
            mismatches.append(f"$.exception: server raised {actual.get('exception') if isinstance(actual, dict) else actual!r}")
            passed = False
        if "expect_error_code" in params:
            ok = isinstance(actual, dict) and actual.get("error_code") == params["expect_error_code"]
            metrics["structured_error"] = 1.0 if ok else 0.0
            if not ok:
                got = actual.get("error_code") if isinstance(actual, dict) else None
                mismatches.append(
                    f"$.error_code: expected {params['expect_error_code']!r}, got {got!r}"
                )
        if "expect_provider" in params:
            ok = isinstance(actual, dict) and actual.get("provider") == params["expect_provider"]
            metrics["fallback_correct"] = 1.0 if ok else 0.0
            if not ok:
                got = actual.get("provider") if isinstance(actual, dict) else None
                mismatches.append(
                    f"$.provider: expected {params['expect_provider']!r}, got {got!r}"
                )
        checks = list(metrics.values())
        score = (score + sum(checks) / len(checks)) / 2 if checks else score
        if any(v < 1.0 for v in checks):
            passed = False
    else:
        raise ValueError(f"unknown scoring method {method!r}")

    return CaseScore(passed=passed, score=round(score, 4), mismatches=mismatches, metrics=metrics)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

STATUSES = ("passed", "failed", "xfail", "xpass", "skipped", "unsupported")


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return ordered[index]


def _dist(values: list[float]) -> dict[str, float]:
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "min": round(min(values), 4),
        "median": round(statistics.median(values), 4),
        "max": round(max(values), 4),
    }


def aggregate(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-group counts and metric distributions for a list of case results."""
    counts = {status: 0 for status in STATUSES}
    scores: list[float] = []
    metric_values: dict[str, list[float]] = {}
    latencies: list[float] = []
    leaks = 0
    network = 0
    for result in case_results:
        counts[result["status"]] += 1
        if result["status"] in ("passed", "failed"):
            scores.append(float(result.get("score", 0.0)))
            for name, value in (result.get("metrics") or {}).items():
                metric_values.setdefault(name, []).append(float(value))
        if result["status"] in ("passed", "failed", "xfail", "xpass"):
            latencies.append(float(result.get("duration_ms", 0.0)))
        leaks += len(result.get("secret_leaks") or [])
        network += len(result.get("network_attempts") or [])

    scored = counts["passed"] + counts["failed"]
    summary: dict[str, Any] = {
        "cases": len(case_results),
        **counts,
        "pass_rate": round(counts["passed"] / scored, 4) if scored else None,
        "score": _dist(scores) if scores else None,
        "secret_leaks": leaks,
        "network_attempts": network,
        "latency_ms": (
            {
                "p50": round(_percentile(latencies, 50), 1),
                "p95": round(_percentile(latencies, 95), 1),
            }
            if latencies
            else None
        ),
        "metrics": {name: _dist(values) for name, values in sorted(metric_values.items())},
    }
    return summary


def flatten(summary: dict[str, Any], prefix: str = "") -> dict[str, float]:
    """Flatten a suite summary into dotted metric names for threshold checks."""
    flat: dict[str, float] = {}
    for key, value in summary.items():
        name = f"{prefix}{key}"
        if key == "metrics" and isinstance(value, dict):
            for metric, dist in value.items():
                for stat, number in dist.items():
                    flat[f"{prefix}{metric}.{stat}"] = number
        elif isinstance(value, dict):
            flat.update(flatten(value, f"{name}."))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            flat[name] = value
    return flat
