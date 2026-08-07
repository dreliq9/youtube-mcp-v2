"""Immutable editor-neutral clip-plan artifacts.

A clip plan is a deterministic selection of timestamped source ranges derived from
frozen corpus evidence.  It is deliberately not Declip JSON, FCPXML, or any other
editor-specific representation.  Downstream editors consume materialized assets
through the small handoff contract documented in EDITOR_HANDOFF.md.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import EDIT_PLAN_DIR as _DEFAULT_EDIT_PLAN_DIR

EDIT_PLAN_DIR = _DEFAULT_EDIT_PLAN_DIR
PLAN_SCHEMA = "youtube-mcp.clip-plan/v1"
_PLAN_RE = re.compile(r"^cp-[0-9a-f]{24}$")
_IDENTITY_FIELDS = (
    "schema",
    "corpus_revision",
    "query",
    "transcript_index_revision",
    "visual_index_revision",
    "settings",
    "clips",
)


class ClipPlanError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _identity_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {field: plan.get(field) for field in _IDENTITY_FIELDS}


def _revision_for_identity(identity: dict[str, Any]) -> str:
    return "cp-" + hashlib.sha256(_canonical_json(identity)).hexdigest()[:24]


def _verify_identity(plan: dict[str, Any], plan_revision: str) -> None:
    if plan.get("schema") != PLAN_SCHEMA or plan.get("plan_revision") != plan_revision:
        raise ClipPlanError(f"clip plan identity mismatch: {plan_revision}")
    expected = _revision_for_identity(_identity_from_plan(plan))
    if expected != plan_revision:
        raise ClipPlanError(
            f"clip plan content hash mismatch: expected {expected}, found {plan_revision}"
        )


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _overlap_ratio(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    overlap = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    if overlap <= 0:
        return 0.0
    a_duration = max(a_end - a_start, 1e-9)
    b_duration = max(b_end - b_start, 1e-9)
    return overlap / min(a_duration, b_duration)


def _window_from_hit(
    hit: dict[str, Any],
    *,
    min_clip_duration_s: float,
    max_clip_duration_s: float,
    context_before_s: float,
    context_after_s: float,
) -> tuple[float, float] | None:
    evidence_start = _float_or_none(hit.get("start_s"))
    evidence_end = _float_or_none(hit.get("end_s"))
    if evidence_start is None and evidence_end is None:
        frame = hit.get("frame")
        if isinstance(frame, dict):
            evidence_start = _float_or_none(frame.get("timestamp_s"))
            evidence_end = evidence_start
    if evidence_start is None and evidence_end is None:
        return None
    if evidence_start is None:
        evidence_start = evidence_end
    if evidence_end is None:
        evidence_end = evidence_start
    assert evidence_start is not None and evidence_end is not None
    if evidence_end < evidence_start:
        evidence_start, evidence_end = evidence_end, evidence_start

    start = max(0.0, evidence_start - context_before_s)
    end = max(evidence_end + context_after_s, start + min_clip_duration_s)

    if end - start > max_clip_duration_s:
        center = (evidence_start + evidence_end) / 2.0
        start = max(0.0, center - max_clip_duration_s / 2.0)
        end = start + max_clip_duration_s
    return round(start, 3), round(end, 3)


def build_plan(
    evidence_response: dict[str, Any],
    *,
    handle: str,
    query: str,
    target_duration_s: float,
    max_clips: int,
    min_clip_duration_s: float,
    max_clip_duration_s: float,
    context_before_s: float,
    context_after_s: float,
) -> dict[str, Any]:
    """Build a deterministic clip plan from a successful evidence-search envelope."""
    data = evidence_response.get("data")
    if not isinstance(data, dict):
        raise ClipPlanError("evidence search did not return a data object")

    selected: list[dict[str, Any]] = []
    total_duration = 0.0
    for hit in data.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        video_id = str(hit.get("video_id") or "")
        if not video_id:
            continue
        window = _window_from_hit(
            hit,
            min_clip_duration_s=min_clip_duration_s,
            max_clip_duration_s=max_clip_duration_s,
            context_before_s=context_before_s,
            context_after_s=context_after_s,
        )
        if window is None:
            continue
        start_s, end_s = window

        duplicate = False
        for existing in selected:
            if existing["video_id"] != video_id:
                continue
            if _overlap_ratio(
                start_s,
                end_s,
                float(existing["start_s"]),
                float(existing["end_s"]),
            ) >= 0.60:
                duplicate = True
                break
        if duplicate:
            continue

        duration = end_s - start_s
        remaining = target_duration_s - total_duration
        if remaining < min_clip_duration_s:
            break
        if duration > remaining:
            if selected:
                continue
            end_s = round(start_s + remaining, 3)
            duration = end_s - start_s

        clip_number = len(selected) + 1
        selected.append(
            {
                "clip_id": f"clip-{clip_number:03d}",
                "video_id": video_id,
                "title": hit.get("title"),
                "channel": hit.get("channel"),
                "source_url": f"https://www.youtube.com/watch?v={video_id}",
                "start_s": start_s,
                "end_s": end_s,
                "duration_s": round(duration, 3),
                "selection_rank": hit.get("rank"),
                "evidence_type": hit.get("evidence_type") or "transcript",
                "evidence": hit,
            }
        )
        total_duration += duration
        if len(selected) >= max_clips:
            break

    if not selected:
        raise ClipPlanError("no timestamped evidence could be converted into edit ranges")

    identity = {
        "schema": PLAN_SCHEMA,
        "corpus_revision": handle,
        "query": query,
        "transcript_index_revision": data.get("transcript_index_revision")
        or data.get("index_revision"),
        "visual_index_revision": data.get("visual_index_revision"),
        "settings": {
            "target_duration_s": target_duration_s,
            "max_clips": max_clips,
            "min_clip_duration_s": min_clip_duration_s,
            "max_clip_duration_s": max_clip_duration_s,
            "context_before_s": context_before_s,
            "context_after_s": context_after_s,
        },
        "clips": selected,
    }
    revision = _revision_for_identity(identity)
    return {
        **identity,
        "plan_revision": revision,
        "created_at": _now_iso(),
        "planned_duration_s": round(sum(float(c["duration_s"]) for c in selected), 3),
        "clip_count": len(selected),
        "materialization": {
            "status": "not_materialized",
            "tool": "media.materialize",
            "input": {"plan_revision": revision},
        },
    }


def plan_path(plan_revision: str) -> Path:
    if not _PLAN_RE.fullmatch(plan_revision):
        raise ClipPlanError(f"invalid clip-plan revision: {plan_revision!r}")
    return EDIT_PLAN_DIR / f"{plan_revision}.json"


def save_plan(plan: dict[str, Any]) -> Path:
    revision = str(plan.get("plan_revision") or "")
    path = plan_path(revision)
    _verify_identity(plan, revision)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(plan, indent=2, ensure_ascii=False)
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write(encoded)
    except FileExistsError:
        existing = load_plan(revision)
        if _canonical_json(_identity_from_plan(existing)) != _canonical_json(
            _identity_from_plan(plan)
        ):
            raise ClipPlanError(
                f"existing clip-plan artifact differs for revision: {revision}"
            )
    return path


def load_plan(plan_revision: str) -> dict[str, Any]:
    path = plan_path(plan_revision)
    if not path.exists():
        raise FileNotFoundError(f"clip plan not found: {plan_revision}")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClipPlanError(f"clip plan is unreadable: {plan_revision}") from exc
    if not isinstance(plan, dict):
        raise ClipPlanError(f"clip plan root is not an object: {plan_revision}")
    _verify_identity(plan, plan_revision)
    return plan
