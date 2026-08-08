"""Bounded, resumable transcript hydration for frozen corpora.

The purpose of hydration is orchestration efficiency: an agent should not need
one MCP call per video merely to populate the append-only transcript cache.
Hydration scans frozen membership in stable order, skips evidence already cached,
and acquires only a small bounded batch per call.

Transcript bodies are deliberately omitted from results. The durable cache is the
artifact; this tool returns only compact status/provenance plus a cursor for the
next batch.
"""

from __future__ import annotations

import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

from . import cache, skeleton
from .tools.transcript import transcript_get as _transcript_get

HydrationPolicy = Literal["missing", "fresh"]
DEFAULT_BATCH_SIZE = 8
MAX_BATCH_SIZE = 25
DEFAULT_MAX_WORKERS = 3
MAX_WORKERS = 4
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class CorpusHydrationError(RuntimeError):
    """Invalid frozen membership or hydration request."""


@dataclass(frozen=True)
class Candidate:
    position: int
    video_id: str
    title: str | None


def _parse_cursor(cursor: str | None, total: int) -> int:
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except (TypeError, ValueError) as exc:
        raise CorpusHydrationError("cursor must be a non-negative integer string") from exc
    if value < 0:
        raise CorpusHydrationError("cursor must be >= 0")
    if value > total:
        raise CorpusHydrationError(
            f"cursor {value} exceeds corpus size {total}"
        )
    return value


def _validate_request(
    *,
    lang: str,
    policy: HydrationPolicy,
    batch_size: int,
    max_workers: int,
) -> str:
    normalized_lang = lang.strip()
    if not normalized_lang:
        raise CorpusHydrationError("lang must not be empty")
    if policy not in {"missing", "fresh"}:
        raise CorpusHydrationError("policy must be 'missing' or 'fresh'")
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise CorpusHydrationError(
            f"batch_size must be between 1 and {MAX_BATCH_SIZE}"
        )
    if max_workers < 1 or max_workers > MAX_WORKERS:
        raise CorpusHydrationError(
            f"max_workers must be between 1 and {MAX_WORKERS}"
        )
    return normalized_lang


def _load_membership(handle: str) -> list[dict[str, Any]]:
    payload = skeleton.load_skeleton(handle)
    videos = payload.get("videos")
    if not isinstance(videos, list):
        raise CorpusHydrationError("corpus revision has no valid videos list")

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for position, video in enumerate(videos):
        if not isinstance(video, dict):
            raise CorpusHydrationError(
                f"corpus video at position {position} is not an object"
            )
        video_id = str(video.get("id") or "")
        if not _VIDEO_ID_RE.fullmatch(video_id):
            raise CorpusHydrationError(
                f"invalid video id at corpus position {position}: {video_id!r}"
            )
        if video_id in seen:
            raise CorpusHydrationError(f"duplicate video id in corpus: {video_id}")
        seen.add(video_id)
        normalized.append(video)
    return normalized


def _latest_cached_row(
    conn,
    video_id: str,
    lang: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, fetched_at FROM transcripts WHERE video_id = ? AND lang = ? "
        "ORDER BY fetched_at DESC, id DESC LIMIT 1",
        (video_id, lang),
    ).fetchone()
    return dict(row) if row is not None else None


def _is_ready(row: dict[str, Any] | None, policy: HydrationPolicy) -> bool:
    if row is None:
        return False
    if policy == "missing":
        return True
    try:
        fresh, _age = cache._is_fresh(row["fetched_at"], cache.TTL_TRANSCRIPTS)
    except Exception:
        # A malformed historical timestamp should be repaired by refetch rather
        # than preventing hydration from progressing.
        return False
    return bool(fresh)


def _scan_batch(
    videos: list[dict[str, Any]],
    *,
    start: int,
    lang: str,
    policy: HydrationPolicy,
    batch_size: int,
) -> tuple[list[Candidate], int, int]:
    """Return candidates, scan_end (exclusive), skipped-cached count."""
    candidates: list[Candidate] = []
    skipped_cached = 0
    scan_end = start

    # Initialize/migrate schema once before worker threads begin using it.
    with cache.connect() as conn:
        for position in range(start, len(videos)):
            video = videos[position]
            video_id = str(video["id"])
            row = _latest_cached_row(conn, video_id, lang)
            scan_end = position + 1
            if _is_ready(row, policy):
                skipped_cached += 1
                continue

            candidates.append(
                Candidate(
                    position=position,
                    video_id=video_id,
                    title=(str(video["title"]) if video.get("title") else None),
                )
            )
            if len(candidates) >= batch_size:
                break

    return candidates, scan_end, skipped_cached


def _compact_result(candidate: Candidate, response: dict[str, Any]) -> dict[str, Any]:
    error = response.get("error")
    if error:
        compact_error = {
            "code": error.get("code"),
            "message": error.get("message"),
            "recoverable": bool(error.get("recoverable", True)),
        }
        return {
            "position": candidate.position,
            "video_id": candidate.video_id,
            "title": candidate.title,
            "status": "failed",
            "source": response.get("source"),
            "validated": False,
            "actual_lang": None,
            "is_generated": None,
            "fetched_at": response.get("fetched_at"),
            "warnings": list(response.get("warnings") or []),
            "provenance": response.get("provenance"),
            "error": compact_error,
        }

    data = response.get("data") or {}
    return {
        "position": candidate.position,
        "video_id": candidate.video_id,
        "title": candidate.title,
        "status": "ready",
        "source": response.get("source"),
        "validated": bool(response.get("validated")),
        "actual_lang": data.get("lang") or data.get("actual_lang"),
        "is_generated": data.get("is_generated"),
        "fetched_at": response.get("fetched_at"),
        "warnings": list(response.get("warnings") or []),
        "provenance": response.get("provenance"),
        "error": None,
    }


def _unexpected_failure(candidate: Candidate, exc: BaseException) -> dict[str, Any]:
    # Do not echo repr(exc): arbitrary provider exceptions may contain proxy or
    # cookie-bearing command/config text. The detailed provider path should be
    # translated at the transcript tool boundary whenever possible.
    return {
        "position": candidate.position,
        "video_id": candidate.video_id,
        "title": candidate.title,
        "status": "failed",
        "source": None,
        "validated": False,
        "actual_lang": None,
        "is_generated": None,
        "fetched_at": None,
        "warnings": [],
        "provenance": None,
        "error": {
            "code": "hydration_worker_failed",
            "message": f"unexpected {type(exc).__name__} during transcript hydration",
            "recoverable": True,
        },
    }


def _acquire(candidate: Candidate, lang: str) -> dict[str, Any]:
    return _transcript_get(candidate.video_id, mode="text", lang=lang)


def hydrate_corpus(
    handle: str,
    *,
    lang: str = "en",
    cursor: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_workers: int = DEFAULT_MAX_WORKERS,
    policy: HydrationPolicy = "missing",
) -> dict[str, Any]:
    """Hydrate at most one bounded batch of transcript evidence.

    Cursor semantics are corpus-position based. Failures still advance the cursor;
    restarting from cursor 0 later naturally retries failures while skipping rows
    that succeeded on an earlier pass.
    """
    normalized_lang = _validate_request(
        lang=lang,
        policy=policy,
        batch_size=batch_size,
        max_workers=max_workers,
    )
    videos = _load_membership(handle)
    total = len(videos)
    start = _parse_cursor(cursor, total)

    candidates, scan_end, skipped_cached = _scan_batch(
        videos,
        start=start,
        lang=normalized_lang,
        policy=policy,
        batch_size=batch_size,
    )

    results: list[dict[str, Any]] = []
    if candidates:
        worker_count = min(max_workers, len(candidates))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="ytmcp-hydrate",
        ) as pool:
            futures: list[tuple[Candidate, Future[dict[str, Any]]]] = [
                (candidate, pool.submit(_acquire, candidate, normalized_lang))
                for candidate in candidates
            ]
            # Resolve in corpus order rather than completion order so identical
            # evidence/cache state yields stable output ordering.
            for candidate, future in futures:
                try:
                    response = future.result()
                    if not isinstance(response, dict):
                        raise TypeError("transcript tool returned non-object result")
                except BaseException as exc:
                    results.append(_unexpected_failure(candidate, exc))
                else:
                    results.append(_compact_result(candidate, response))

    succeeded = sum(result["status"] == "ready" for result in results)
    failed = len(results) - succeeded
    complete = scan_end >= total
    return {
        "corpus_revision": handle,
        "lang": normalized_lang,
        "policy": policy,
        "cursor": str(start),
        "next_cursor": None if complete else str(scan_end),
        "complete": complete,
        "total_videos": total,
        "scanned": scan_end - start,
        "skipped_cached": skipped_cached,
        "attempted": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "remaining_members": max(0, total - scan_end),
        "results": results,
    }
