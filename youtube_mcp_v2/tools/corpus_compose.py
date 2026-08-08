"""Compose heterogeneous frozen corpora without live acquisition.

This is the domain-neutral source-selection primitive. A composed corpus can mix
arbitrary videos with one or more existing frozen corpora (channel/topic/other
collections). Composition copies membership into a new immutable collection
revision; it never mutates the included sources and never performs network I/O.
"""

from __future__ import annotations

from typing import Any

from .. import cache, envelope, skeleton
from ..adapters.url import parse_video_id

MAX_EXPLICIT_VIDEOS = 500
MAX_INCLUDED_CORPORA = 25
MAX_RESULT_VIDEOS = 2000


def _video_from_cache(video_id: str) -> dict[str, Any]:
    """Build a stable lean membership record, using cached metadata when present."""
    video: dict[str, Any] = {
        "id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": None,
        "channel": None,
        "channel_id": None,
        "duration_s": None,
        "published": None,
        "view_count": None,
        "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "has_transcript": None,
        "caption_track_id": None,
        "lang": None,
    }
    hit = cache.get_video_meta(video_id, fresh_only=False)
    if hit is None:
        return video
    meta = hit[0]
    for source, target in (
        ("title", "title"),
        ("channel", "channel"),
        ("channel_id", "channel_id"),
        ("duration_s", "duration_s"),
        ("publish_date", "published"),
        ("view_count", "view_count"),
        ("thumbnail_url", "thumbnail_url"),
        ("has_transcript", "has_transcript"),
        ("lang_default", "lang"),
    ):
        if source in meta and meta[source] not in (None, "", []):
            video[target] = meta[source]
    return video


def _parse_refs(refs: list[str], field_name: str) -> tuple[list[str], list[str]]:
    ids: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for raw in refs:
        try:
            video_id = parse_video_id(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {field_name} video reference {raw!r}: {exc}") from exc
        if video_id in seen:
            duplicates.append(video_id)
            continue
        seen.add(video_id)
        ids.append(video_id)
    return ids, duplicates


def corpus_compose(
    label: str,
    videos: list[str] | None = None,
    include_handles: list[str] | None = None,
    base_handle: str | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new immutable corpus from arbitrary videos and frozen corpora.

    Membership order is deterministic:
      1. base corpus (if supplied),
      2. included corpora in caller order,
      3. explicit videos in caller order,
      4. removals applied last.

    Duplicate video IDs are collapsed by first occurrence. No live YouTube calls are
    made; new explicit videos use cached metadata when available and otherwise keep a
    lean ID/URL record suitable for later inspect/hydration.
    """
    label = (label or "").strip()
    if not label:
        return envelope.fail("bad_label", "label must not be empty", recoverable=False)

    videos = list(videos or [])
    include_handles = list(include_handles or [])
    remove = list(remove or [])

    if len(videos) > MAX_EXPLICIT_VIDEOS:
        return envelope.fail(
            "too_many_videos",
            f"videos is capped at {MAX_EXPLICIT_VIDEOS} explicit references per call",
            recoverable=False,
        )
    if len(include_handles) > MAX_INCLUDED_CORPORA:
        return envelope.fail(
            "too_many_corpora",
            f"include_handles is capped at {MAX_INCLUDED_CORPORA}",
            recoverable=False,
        )
    if not videos and not include_handles and base_handle is None:
        return envelope.fail(
            "empty_composition",
            "provide videos, include_handles, or base_handle",
            recoverable=False,
        )

    try:
        explicit_ids, duplicate_inputs = _parse_refs(videos, "explicit")
        remove_ids, duplicate_removals = _parse_refs(remove, "remove")
    except ValueError as exc:
        return envelope.fail("bad_video", str(exc), recoverable=False)

    source_handles: list[str] = []
    membership: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    duplicate_membership: list[str] = []

    def append_from_payload(payload: dict[str, Any], handle: str) -> None:
        raw_videos = payload.get("videos")
        if not isinstance(raw_videos, list):
            raise ValueError(f"corpus {handle!r} has no valid videos list")
        source_handles.append(handle)
        for raw_video in raw_videos:
            if not isinstance(raw_video, dict):
                raise ValueError(f"corpus {handle!r} contains a non-object video entry")
            video_id = str(raw_video.get("id") or "")
            try:
                canonical_id = parse_video_id(video_id)
            except ValueError as exc:
                raise ValueError(
                    f"corpus {handle!r} contains invalid video id {video_id!r}"
                ) from exc
            if canonical_id in seen_ids:
                duplicate_membership.append(canonical_id)
                continue
            entry = dict(raw_video)
            entry["id"] = canonical_id
            entry.setdefault("url", f"https://www.youtube.com/watch?v={canonical_id}")
            membership.append(entry)
            seen_ids.add(canonical_id)

    try:
        if base_handle is not None:
            append_from_payload(skeleton.load_skeleton(base_handle), base_handle)
        for handle in include_handles:
            append_from_payload(skeleton.load_skeleton(handle), handle)
    except FileNotFoundError as exc:
        return envelope.fail("corpus_not_found", str(exc), recoverable=False)
    except ValueError as exc:
        return envelope.fail("bad_corpus", str(exc), recoverable=False)

    added_ids: list[str] = []
    for video_id in explicit_ids:
        if video_id in seen_ids:
            duplicate_membership.append(video_id)
            continue
        membership.append(_video_from_cache(video_id))
        seen_ids.add(video_id)
        added_ids.append(video_id)

    remove_set = set(remove_ids)
    removed_ids = [item["id"] for item in membership if item.get("id") in remove_set]
    membership = [item for item in membership if item.get("id") not in remove_set]

    if len(membership) > MAX_RESULT_VIDEOS:
        return envelope.fail(
            "corpus_too_large",
            f"composed corpus would contain {len(membership)} videos; cap is {MAX_RESULT_VIDEOS}",
            recoverable=False,
        )
    if not membership:
        return envelope.fail(
            "empty_result",
            "composition produced an empty corpus after removals",
            recoverable=False,
        )

    handle = skeleton.make_handle("collection", label)
    payload = {
        "handle": handle,
        "target": "collection",
        "value": label,
        "built_at": skeleton._now_iso(),
        "source": "compose",
        "expired_at": None,
        "channel": None,
        "parent_handle": base_handle,
        "included_handles": include_handles,
        "composition": {
            "source_handles": source_handles,
            "explicit_video_ids": explicit_ids,
            "added_video_ids": added_ids,
            "removed_video_ids": removed_ids,
            "duplicate_video_ids_ignored": list(
                dict.fromkeys([*duplicate_inputs, *duplicate_removals, *duplicate_membership])
            ),
        },
        "videos": membership,
    }
    skeleton.save_skeleton(payload)

    warnings: list[str] = []
    unknown_removals = [video_id for video_id in remove_ids if video_id not in removed_ids]
    if unknown_removals:
        warnings.append(
            f"{len(unknown_removals)} requested removals were not present in the composed source set"
        )
    duplicates = payload["composition"]["duplicate_video_ids_ignored"]
    if duplicates:
        warnings.append(f"ignored {len(duplicates)} duplicate video references")

    return envelope.ok(
        {
            "handle": handle,
            "target": "collection",
            "label": label,
            "count": len(membership),
            "parent_handle": base_handle,
            "included_handles": include_handles,
            "added_video_ids": added_ids,
            "removed_video_ids": removed_ids,
            "duplicate_video_ids_ignored": duplicates,
        },
        source="cache",
        warnings=warnings,
    )
