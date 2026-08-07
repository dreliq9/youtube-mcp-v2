"""Editor-neutral clip planning from frozen corpus evidence."""

from __future__ import annotations

from typing import Any

from .. import edit_plan, envelope
from .evidence import Modalities, corpus_evidence_search

MAX_TARGET_DURATION_S = 1800.0
MAX_CLIPS = 20
MAX_CLIP_DURATION_S = 180.0
MAX_CONTEXT_S = 30.0


def corpus_clip_plan(
    handle: str,
    query: str,
    target_duration_s: float = 60.0,
    max_clips: int = 8,
    min_clip_duration_s: float = 4.0,
    max_clip_duration_s: float = 20.0,
    context_before_s: float = 2.0,
    context_after_s: float = 2.0,
    lang: str = "en",
    validated_only: bool = False,
    modalities: Modalities = "auto",
) -> dict[str, Any]:
    """Turn ranked corpus evidence into an immutable editor-neutral clip plan.

    The operation is retrieval/planning only.  It never downloads source video,
    initializes ML models, renders a timeline, or mutates an editor project.
    """
    normalized_query = query.strip()
    if not normalized_query:
        return envelope.fail("bad_clip_plan", "query must not be empty", recoverable=False)
    try:
        target_duration_s = float(target_duration_s)
        max_clips = int(max_clips)
        min_clip_duration_s = float(min_clip_duration_s)
        max_clip_duration_s = float(max_clip_duration_s)
        context_before_s = float(context_before_s)
        context_after_s = float(context_after_s)
    except (TypeError, ValueError):
        return envelope.fail("bad_clip_plan", "numeric clip-plan arguments are invalid", recoverable=False)

    if target_duration_s < 2 or target_duration_s > MAX_TARGET_DURATION_S:
        return envelope.fail(
            "bad_clip_plan",
            f"target_duration_s must be between 2 and {MAX_TARGET_DURATION_S:g}",
            recoverable=False,
        )
    if max_clips < 1 or max_clips > MAX_CLIPS:
        return envelope.fail(
            "bad_clip_plan",
            f"max_clips must be between 1 and {MAX_CLIPS}",
            recoverable=False,
        )
    if min_clip_duration_s < 2 or min_clip_duration_s > 60:
        return envelope.fail(
            "bad_clip_plan",
            "min_clip_duration_s must be between 2 and 60",
            recoverable=False,
        )
    if max_clip_duration_s < min_clip_duration_s or max_clip_duration_s > MAX_CLIP_DURATION_S:
        return envelope.fail(
            "bad_clip_plan",
            f"max_clip_duration_s must be >= min_clip_duration_s and <= {MAX_CLIP_DURATION_S:g}",
            recoverable=False,
        )
    if target_duration_s < min_clip_duration_s:
        return envelope.fail(
            "bad_clip_plan",
            "target_duration_s must be at least min_clip_duration_s",
            recoverable=False,
        )
    if not (0 <= context_before_s <= MAX_CONTEXT_S) or not (0 <= context_after_s <= MAX_CONTEXT_S):
        return envelope.fail(
            "bad_clip_plan",
            f"context_before_s/context_after_s must be between 0 and {MAX_CONTEXT_S:g}",
            recoverable=False,
        )

    evidence_top_k = min(25, max(10, max_clips * 3))
    evidence_response = corpus_evidence_search(
        handle,
        normalized_query,
        top_k=evidence_top_k,
        lang=lang,
        validated_only=validated_only,
        auto_prepare=True,
        modalities=modalities,
        temporal_window_s=20.0,
    )
    if evidence_response.get("error") is not None:
        error = evidence_response.get("error") or {}
        return envelope.fail(
            "clip_plan_evidence_failed",
            f"evidence retrieval failed ({error.get('code', 'unknown')}): {error.get('message', 'unknown error')}",
            recoverable=bool(error.get("recoverable", True)),
        )

    try:
        plan = edit_plan.build_plan(
            evidence_response,
            handle=handle,
            query=normalized_query,
            target_duration_s=target_duration_s,
            max_clips=max_clips,
            min_clip_duration_s=min_clip_duration_s,
            max_clip_duration_s=max_clip_duration_s,
            context_before_s=context_before_s,
            context_after_s=context_after_s,
        )
        path = edit_plan.save_plan(plan)
    except (edit_plan.ClipPlanError, OSError) as exc:
        return envelope.fail("clip_plan_failed", str(exc), recoverable=False)

    data = dict(plan)
    data["plan_path"] = str(path)
    data["plan_portable"] = False
    warnings = list(evidence_response.get("warnings") or [])
    warnings.append(
        "clip plan contains source ranges only; call media.materialize before handing assets to a local editor"
    )
    return envelope.ok(
        data,
        source="cache",
        validated=bool(evidence_response.get("validated", False)),
        warnings=list(dict.fromkeys(warnings)),
    )
