"""Evidence-retrieval metrics: Recall@k and temporal-window scoring.

Pure functions over ranked hits (`video_id`, `start_s`, `end_s`) and annotated
gold evidence windows. Standard library only, so they can be folded into the
Bench 0.2 scorer (`bench/scoring.py`, PR #31) as a `retrieval` method without
new dependencies.

Definitions (per query; `tol` = tolerance in seconds):

* a hit **matches** a gold window when it is from the same video and its
  [start_s, end_s] span overlaps [gold.start - tol, gold.end + tol];
* ``recall@k``      fraction of gold windows matched by at least one top-k hit;
* ``success@k``     1 if any top-k hit matches any gold window;
* ``reciprocal_rank`` 1 / rank of the first matching hit (0 if none);
* ``temporal_hit@k``  1 if some top-k hit's *cited start* (``cited_start_s``,
  the timestamp a user would jump to; defaults to ``start_s``) falls inside a
  gold window ± tol — stricter than overlap;
* ``temporal_error_s`` distance from the first matching hit's cited start to its
  gold window (0 when inside); ``window_iou`` IoU of that hit's span and window.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence


def _window(gold: Any) -> tuple[str, float, float]:
    if isinstance(gold, Mapping):
        return str(gold["video_id"]), float(gold["start_s"]), float(gold["end_s"])
    return str(gold.video_id), float(gold.start_s), float(gold.end_s)


def _span(hit: Mapping[str, Any]) -> tuple[str, float | None, float | None]:
    start = hit.get("start_s")
    end = hit.get("end_s")
    start_f = None if start is None else float(start)
    end_f = start_f if end is None else float(end)
    return str(hit.get("video_id")), start_f, end_f


def hit_matches(hit: Mapping[str, Any], gold: Any, tol: float = 0.0) -> bool:
    video, g_start, g_end = _window(gold)
    h_video, h_start, h_end = _span(hit)
    if h_video != video or h_start is None:
        return False
    return h_end >= g_start - tol and h_start <= g_end + tol


def recall_at_k(hits: Sequence[Mapping[str, Any]], gold: Sequence[Any], k: int, tol: float = 0.0) -> float:
    if not gold:
        raise ValueError("recall@k needs at least one gold window")
    top = hits[:k]
    found = sum(1 for window in gold if any(hit_matches(hit, window, tol) for hit in top))
    return found / len(gold)


def success_at_k(hits: Sequence[Mapping[str, Any]], gold: Sequence[Any], k: int, tol: float = 0.0) -> float:
    return 1.0 if recall_at_k(hits, gold, k, tol) > 0 else 0.0


def first_match(
    hits: Sequence[Mapping[str, Any]], gold: Sequence[Any], tol: float = 0.0
) -> tuple[int, Mapping[str, Any], Any] | None:
    for rank, hit in enumerate(hits, start=1):
        for window in gold:
            if hit_matches(hit, window, tol):
                return rank, hit, window
    return None


def reciprocal_rank(hits: Sequence[Mapping[str, Any]], gold: Sequence[Any], tol: float = 0.0) -> float:
    match = first_match(hits, gold, tol)
    return 0.0 if match is None else 1.0 / match[0]


def _cited_start(hit: Mapping[str, Any]) -> float | None:
    value = hit.get("cited_start_s", hit.get("start_s"))
    return None if value is None else float(value)


def temporal_hit_at_k(hits: Sequence[Mapping[str, Any]], gold: Sequence[Any], k: int, tol: float = 0.0) -> float:
    for hit in hits[:k]:
        h_video = str(hit.get("video_id"))
        h_start = _cited_start(hit)
        if h_start is None:
            continue
        for window in gold:
            video, g_start, g_end = _window(window)
            if h_video == video and g_start - tol <= h_start <= g_end + tol:
                return 1.0
    return 0.0


def temporal_error_s(hits: Sequence[Mapping[str, Any]], gold: Sequence[Any], tol: float = 0.0) -> float | None:
    match = first_match(hits, gold, tol)
    if match is None:
        return None
    _rank, hit, window = match
    _video, g_start, g_end = _window(window)
    start = _cited_start(hit)
    if start is None:
        return None
    if g_start <= start <= g_end:
        return 0.0
    return min(abs(start - g_start), abs(start - g_end))


def window_iou(hits: Sequence[Mapping[str, Any]], gold: Sequence[Any], tol: float = 0.0) -> float | None:
    match = first_match(hits, gold, tol)
    if match is None:
        return None
    _rank, hit, window = match
    _video, g_start, g_end = _window(window)
    _h, h_start, h_end = _span(hit)
    inter = max(0.0, min(h_end, g_end) - max(h_start, g_start))
    union = max(h_end, g_end) - min(h_start, g_start)
    return inter / union if union > 0 else 1.0


def score_query(
    hits: Sequence[Mapping[str, Any]],
    gold: Sequence[Any],
    *,
    ks: Iterable[int] = (1, 5, 10),
    tol: float = 0.0,
) -> dict[str, float | None]:
    metrics: dict[str, float | None] = {}
    for k in ks:
        metrics[f"recall@{k}"] = recall_at_k(hits, gold, k, tol)
        metrics[f"success@{k}"] = success_at_k(hits, gold, k, tol)
        metrics[f"temporal_hit@{k}"] = temporal_hit_at_k(hits, gold, k, tol)
    metrics["reciprocal_rank"] = reciprocal_rank(hits, gold, tol)
    metrics["temporal_error_s"] = temporal_error_s(hits, gold, tol)
    metrics["window_iou"] = window_iou(hits, gold, tol)
    return metrics


def mean_metrics(rows: Sequence[Mapping[str, float | None]]) -> dict[str, float | None]:
    """Mean of every metric across queries, ignoring `None` (e.g. no match)."""
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    out: dict[str, float | None] = {}
    for key in keys:
        values = [float(row[key]) for row in rows if row.get(key) is not None]
        out[key] = round(sum(values) / len(values), 4) if values else None
    return out
