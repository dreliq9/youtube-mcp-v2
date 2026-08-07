"""Bounded cross-modal evidence retrieval across transcript and frame indexes.

The calling model should be able to ask one research question and receive the
smallest useful set of timestamped evidence, regardless of whether the support is
spoken, visible, or both. Backend-specific score scales are never added directly;
component rankings are fused with reciprocal rank fusion (RRF).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .. import envelope
from .corpus import corpus_search
from .visual import corpus_visual_search

Modalities = Literal["auto", "transcript", "visual", "both"]
MAX_TOP_K = 25
MAX_TEMPORAL_WINDOW_S = 120.0
RRF_K = 60.0


class EvidenceSearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class _TranscriptCandidate:
    hit: dict[str, Any]
    rank: int
    video_id: str
    start_s: float | None
    end_s: float | None


@dataclass(frozen=True)
class _VisualCandidate:
    hit: dict[str, Any]
    rank: int
    video_id: str
    timestamp_s: float


def _candidate_top_k(top_k: int) -> int:
    # Pull enough candidates for temporal pairing without making a compound tool
    # accidentally return an unbounded amount of evidence from either modality.
    return min(50, max(20, top_k * 5))


def _validate_request(
    *,
    query: str,
    top_k: int,
    modalities: str,
    temporal_window_s: float,
) -> str:
    normalized = query.strip()
    if not normalized:
        raise EvidenceSearchError("query must not be empty")
    if top_k < 1 or top_k > MAX_TOP_K:
        raise EvidenceSearchError(f"top_k must be between 1 and {MAX_TOP_K}")
    if modalities not in {"auto", "transcript", "visual", "both"}:
        raise EvidenceSearchError(
            "modalities must be 'auto', 'transcript', 'visual', or 'both'"
        )
    if temporal_window_s < 0 or temporal_window_s > MAX_TEMPORAL_WINDOW_S:
        raise EvidenceSearchError(
            f"temporal_window_s must be between 0 and {MAX_TEMPORAL_WINDOW_S:g}"
        )
    return normalized


def _should_attempt_transcript(modalities: Modalities, index_revision: str | None) -> bool:
    return modalities in {"auto", "transcript", "both"} or index_revision is not None


def _should_attempt_visual(
    modalities: Modalities, visual_index_revision: str | None
) -> bool:
    return modalities in {"auto", "visual", "both"} or visual_index_revision is not None


def _component_required(
    component: Literal["transcript", "visual"],
    *,
    modalities: Modalities,
    index_revision: str | None,
    visual_index_revision: str | None,
) -> bool:
    if component == "transcript":
        return modalities in {"transcript", "both"} or index_revision is not None
    return modalities in {"visual", "both"} or visual_index_revision is not None


def _component_failure(
    component: str,
    response: dict[str, Any],
) -> tuple[str, str, bool]:
    error = response.get("error") or {}
    code = str(error.get("code") or f"{component}_failed")
    message = str(error.get("message") or f"{component} evidence retrieval failed")
    recoverable = bool(error.get("recoverable", True))
    return code, message, recoverable


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _transcript_candidates(response: dict[str, Any]) -> list[_TranscriptCandidate]:
    data = response.get("data") or {}
    candidates: list[_TranscriptCandidate] = []
    for fallback_rank, hit in enumerate(data.get("hits") or [], start=1):
        if not isinstance(hit, dict):
            continue
        video_id = str(hit.get("video_id") or "")
        if not video_id:
            continue
        try:
            rank = int(hit.get("rank") or fallback_rank)
        except (TypeError, ValueError):
            rank = fallback_rank
        candidates.append(
            _TranscriptCandidate(
                hit=hit,
                rank=max(1, rank),
                video_id=video_id,
                start_s=_float_or_none(hit.get("start_s")),
                end_s=_float_or_none(hit.get("end_s")),
            )
        )
    return candidates


def _visual_candidates(response: dict[str, Any]) -> list[_VisualCandidate]:
    data = response.get("data") or {}
    candidates: list[_VisualCandidate] = []
    for fallback_rank, hit in enumerate(data.get("hits") or [], start=1):
        if not isinstance(hit, dict):
            continue
        video_id = str(hit.get("video_id") or "")
        timestamp_s = _float_or_none(hit.get("timestamp_s"))
        if not video_id or timestamp_s is None:
            continue
        try:
            rank = int(hit.get("rank") or fallback_rank)
        except (TypeError, ValueError):
            rank = fallback_rank
        candidates.append(
            _VisualCandidate(
                hit=hit,
                rank=max(1, rank),
                video_id=video_id,
                timestamp_s=timestamp_s,
            )
        )
    return candidates


def _temporal_distance(
    transcript: _TranscriptCandidate,
    visual: _VisualCandidate,
) -> float | None:
    if transcript.video_id != visual.video_id:
        return None
    start = transcript.start_s
    end = transcript.end_s
    if start is None and end is None:
        return None
    if start is None:
        start = end
    if end is None:
        end = start
    assert start is not None and end is not None
    if end < start:
        start, end = end, start
    if start <= visual.timestamp_s <= end:
        return 0.0
    return min(abs(visual.timestamp_s - start), abs(visual.timestamp_s - end))


def _rrf(rank: int) -> float:
    return 1.0 / (RRF_K + max(1, rank))


def _safe_visual_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """Drop same-host implementation paths from compound research results."""
    allowed = {
        "video_id",
        "title",
        "channel",
        "timestamp_s",
        "url",
        "artifact_ext",
        "frame_sha256",
        "source_kind",
        "score",
        "resource_uri",
        "resource_mime_type",
        "resource_size_bytes",
        "resource_portable",
    }
    return {key: hit.get(key) for key in allowed if key in hit}


def _safe_transcript_hit(hit: dict[str, Any]) -> dict[str, Any]:
    # Corpus search already returns bounded chunks rather than full transcripts.
    # Preserve its evidence/provenance fields while dropping its component rank;
    # the compound result has a separate component/fusion rank contract.
    return {key: value for key, value in hit.items() if key != "rank"}


def _youtube_url(video_id: str, timestamp_s: float | None) -> str:
    base = f"https://www.youtube.com/watch?v={video_id}"
    if timestamp_s is None:
        return base
    return f"{base}&t={max(0, int(timestamp_s))}s"


def _cluster(
    transcript: _TranscriptCandidate | None,
    visual: _VisualCandidate | None,
    *,
    temporal_distance_s: float | None,
) -> dict[str, Any]:
    if transcript is None and visual is None:
        raise EvidenceSearchError("cannot build an empty evidence cluster")

    transcript_raw = _rrf(transcript.rank) if transcript is not None else 0.0
    visual_raw = _rrf(visual.rank) if visual is not None else 0.0
    raw = transcript_raw + visual_raw

    if transcript is not None and visual is not None:
        evidence_type = "cross_modal"
        video_id = transcript.video_id
        title = transcript.hit.get("title") or visual.hit.get("title")
        channel = transcript.hit.get("channel") or visual.hit.get("channel")
        timestamps = [
            value
            for value in (
                transcript.start_s,
                transcript.end_s,
                visual.timestamp_s,
            )
            if value is not None
        ]
    elif transcript is not None:
        evidence_type = "transcript"
        video_id = transcript.video_id
        title = transcript.hit.get("title")
        channel = transcript.hit.get("channel")
        timestamps = [
            value for value in (transcript.start_s, transcript.end_s) if value is not None
        ]
    else:
        assert visual is not None
        evidence_type = "frame"
        video_id = visual.video_id
        title = visual.hit.get("title")
        channel = visual.hit.get("channel")
        timestamps = [visual.timestamp_s]

    start_s = min(timestamps) if timestamps else None
    end_s = max(timestamps) if timestamps else None
    return {
        "evidence_type": evidence_type,
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "start_s": start_s,
        "end_s": end_s,
        "url": _youtube_url(video_id, start_s),
        "fusion": {
            "method": "reciprocal_rank_fusion",
            "rrf_k": RRF_K,
            "raw": raw,
            "score": None,
            "transcript_rank": transcript.rank if transcript is not None else None,
            "visual_rank": visual.rank if visual is not None else None,
            "temporal_distance_s": temporal_distance_s,
        },
        "transcript": (
            _safe_transcript_hit(transcript.hit) if transcript is not None else None
        ),
        "frame": _safe_visual_hit(visual.hit) if visual is not None else None,
    }


def _fuse(
    transcript_candidates: list[_TranscriptCandidate],
    visual_candidates: list[_VisualCandidate],
    *,
    top_k: int,
    temporal_window_s: float,
) -> list[dict[str, Any]]:
    """Greedily pair nearest cross-modal candidates, then rank clusters by RRF.

    Pairing is one-to-one. This prevents one highly ranked frame from being copied
    into many nearby transcript hits (or vice versa), while still rewarding true
    agreement between independent spoken and visual retrieval paths.
    """
    edges: list[tuple[float, int, int, int, int]] = []
    for ti, transcript in enumerate(transcript_candidates):
        for vi, visual in enumerate(visual_candidates):
            distance = _temporal_distance(transcript, visual)
            if distance is None or distance > temporal_window_s:
                continue
            edges.append(
                (distance, transcript.rank + visual.rank, transcript.rank, visual.rank, ti * 100000 + vi)
            )
    edges.sort()

    paired_t: set[int] = set()
    paired_v: set[int] = set()
    clusters: list[dict[str, Any]] = []
    for distance, _rank_sum, _trank, _vrank, packed in edges:
        ti, vi = divmod(packed, 100000)
        if ti in paired_t or vi in paired_v:
            continue
        paired_t.add(ti)
        paired_v.add(vi)
        clusters.append(
            _cluster(
                transcript_candidates[ti],
                visual_candidates[vi],
                temporal_distance_s=distance,
            )
        )

    for ti, transcript in enumerate(transcript_candidates):
        if ti not in paired_t:
            clusters.append(_cluster(transcript, None, temporal_distance_s=None))
    for vi, visual in enumerate(visual_candidates):
        if vi not in paired_v:
            clusters.append(_cluster(None, visual, temporal_distance_s=None))

    clusters.sort(
        key=lambda item: (
            -float(item["fusion"]["raw"]),
            0 if item["evidence_type"] == "cross_modal" else 1,
            str(item["video_id"]),
            float(item["start_s"] or 0.0),
        )
    )
    clusters = clusters[:top_k]
    max_raw = max((float(item["fusion"]["raw"]) for item in clusters), default=1.0)
    for rank, item in enumerate(clusters, start=1):
        item["rank"] = rank
        item["fusion"]["score"] = (
            float(item["fusion"]["raw"]) / max_raw if max_raw else 0.0
        )
    return clusters


def corpus_evidence_search(
    handle: str,
    query: str,
    top_k: int = 10,
    lang: str = "en",
    index_revision: str | None = None,
    visual_index_revision: str | None = None,
    validated_only: bool = False,
    auto_prepare: bool = True,
    modalities: Modalities = "auto",
    temporal_window_s: float = 20.0,
) -> dict[str, Any]:
    """Retrieve and temporally fuse spoken + on-screen evidence for one query.

    This compound tool is deliberately local-only with respect to ML model setup:
    transcript search uses `semantic='auto'` and visual search uses `visual='auto'`,
    so it never turns a research query into a surprise model download. Users can
    initialize/pin richer indexes through the component prepare/search tools first.
    """
    try:
        normalized_query = _validate_request(
            query=query,
            top_k=top_k,
            modalities=modalities,
            temporal_window_s=temporal_window_s,
        )
    except EvidenceSearchError as exc:
        return envelope.fail("bad_evidence_query", str(exc), recoverable=False)

    component_top_k = _candidate_top_k(top_k)
    transcript_response: dict[str, Any] | None = None
    visual_response: dict[str, Any] | None = None
    warnings: list[str] = []
    used: list[str] = []

    if _should_attempt_transcript(modalities, index_revision):
        transcript_response = corpus_search(
            handle,
            normalized_query,
            top_k=component_top_k,
            lang=lang,
            index_revision=index_revision,
            validated_only=validated_only,
            auto_prepare=auto_prepare,
            semantic="auto",
        )
        if transcript_response.get("error") is not None:
            code, message, recoverable = _component_failure(
                "transcript", transcript_response
            )
            if _component_required(
                "transcript",
                modalities=modalities,
                index_revision=index_revision,
                visual_index_revision=visual_index_revision,
            ):
                return envelope.fail(
                    "transcript_evidence_failed",
                    f"transcript evidence failed ({code}): {message}",
                    recoverable=recoverable,
                )
            warnings.append(
                f"transcript evidence unavailable in auto mode ({code}); continuing "
                "with other available modalities"
            )
            transcript_response = None
        else:
            used.append("transcript")
            warnings.extend(
                f"transcript: {warning}"
                for warning in (transcript_response.get("warnings") or [])
            )

    if _should_attempt_visual(modalities, visual_index_revision):
        visual_response = corpus_visual_search(
            handle,
            normalized_query,
            top_k=component_top_k,
            visual_index_revision=visual_index_revision,
            auto_prepare=auto_prepare,
            visual="auto",
        )
        if visual_response.get("error") is not None:
            code, message, recoverable = _component_failure("visual", visual_response)
            if _component_required(
                "visual",
                modalities=modalities,
                index_revision=index_revision,
                visual_index_revision=visual_index_revision,
            ):
                return envelope.fail(
                    "visual_evidence_failed",
                    f"visual evidence failed ({code}): {message}",
                    recoverable=recoverable,
                )
            warnings.append(
                f"visual evidence unavailable in auto mode ({code}); continuing "
                "with other available modalities"
            )
            visual_response = None
        else:
            used.append("visual")
            warnings.extend(
                f"visual: {warning}"
                for warning in (visual_response.get("warnings") or [])
            )

    if transcript_response is None and visual_response is None:
        return envelope.fail(
            "evidence_unavailable",
            "no requested evidence modality is currently searchable for this corpus",
            recoverable=True,
        )

    transcript_candidates = (
        _transcript_candidates(transcript_response)
        if transcript_response is not None
        else []
    )
    visual_candidates = (
        _visual_candidates(visual_response) if visual_response is not None else []
    )
    hits = _fuse(
        transcript_candidates,
        visual_candidates,
        top_k=top_k,
        temporal_window_s=temporal_window_s,
    )

    transcript_data = transcript_response.get("data") if transcript_response else None
    visual_data = visual_response.get("data") if visual_response else None
    result = {
        "corpus_revision": handle,
        "query": normalized_query,
        "modalities_requested": modalities,
        "modalities_used": used,
        "transcript_index_revision": (
            transcript_data.get("index_revision") if isinstance(transcript_data, dict) else None
        ),
        "visual_index_revision": (
            visual_data.get("visual_index_revision")
            if isinstance(visual_data, dict)
            else None
        ),
        "coverage": {
            "transcript": (
                transcript_data.get("coverage") if isinstance(transcript_data, dict) else None
            ),
            "visual": (
                visual_data.get("coverage") if isinstance(visual_data, dict) else None
            ),
        },
        "fusion": {
            "method": "reciprocal_rank_fusion_with_temporal_pairing",
            "rrf_k": RRF_K,
            "temporal_window_s": temporal_window_s,
            "component_top_k": component_top_k,
        },
        "hits": hits,
    }

    component_validated = [
        bool(response.get("validated", True))
        for response in (transcript_response, visual_response)
        if response is not None
    ]
    return envelope.ok(
        result,
        source="cache",
        validated=all(component_validated) if component_validated else False,
        warnings=list(dict.fromkeys(warnings)),
    )
