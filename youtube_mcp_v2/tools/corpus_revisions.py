"""corpus.build / get / list / index / diff — immutable corpus revisions.

These are the canonical v0.3 corpus tools. Historical `skeleton.*` handles are
valid inputs everywhere (they are legacy corpus revisions); the `skeleton.*`
tools themselves are thin compatibility wrappers over the same storage.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from .. import corpus_index, corpus_revision, envelope, isolation, skeleton
from ..adapters.url import parse_video_id

CorpusTarget = Literal["channel", "playlist", "topic", "videos"]

MAX_LIMIT = 200
MAX_EXPLICIT_BUILD_VIDEOS = 100
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")


def _attempt(provider: str, method: str, outcome: str, started: float, detail_code: str | None = None) -> dict[str, Any]:
    # Messages are deliberately omitted: upstream errors can echo proxy URLs.
    return {
        "provider": provider,
        "method": method,
        "outcome": outcome,
        "detail_code": detail_code,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


class BuildFailed(RuntimeError):
    def __init__(self, code: str, message: str, attempts: list[dict[str, Any]], recoverable: bool = True):
        super().__init__(message)
        self.code = code
        self.attempts = attempts
        self.recoverable = recoverable


def _api_error_code(exc: Exception) -> str:
    from ..adapters import data_api

    if isinstance(exc, data_api.ApiQuotaError):
        return "quota_exceeded"
    if isinstance(exc, data_api.ApiAuthError):
        return "auth_failed"
    return "api_error"


# ---------------------------------------------------------------------------
# Acquisition per target (each returns raw, source, attempts)
# ---------------------------------------------------------------------------


def acquire_channel(value: str, limit: int) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """Data API when a key is configured, else channel page, else yt-dlp."""
    attempts: list[dict[str, Any]] = []
    if os.environ.get("YOUTUBE_API_KEY"):
        from ..adapters import data_api

        started = time.monotonic()
        try:
            raw = data_api.channel_uploads(value, limit=limit)
            attempts.append(_attempt("youtube-data-api", "channel_uploads", "success", started))
            return raw, "api", attempts
        except (data_api.ApiAuthError, data_api.ApiQuotaError, data_api.ApiCallError) as exc:
            attempts.append(_attempt("youtube-data-api", "channel_uploads", "error", started, _api_error_code(exc)))

    started = time.monotonic()
    try:
        raw = isolation.run_isolated(
            "youtube_mcp_v2.adapters.channel_scrape.fetch_channel_uploads",
            value,
            limit,
            timeout_s=25,
        )
    except TimeoutError as exc:
        attempts.append(_attempt("channel-page", "initial-data", "timeout", started, "timeout"))
        raise BuildFailed("skeleton_build_timeout", str(exc), attempts) from exc
    except RuntimeError as exc:
        attempts.append(_attempt("channel-page", "initial-data", "error", started, "provider_error"))
        raise BuildFailed("skeleton_build_failed", str(exc), attempts) from exc
    source = raw.get("source", "scrape")
    if source == "yt-dlp":
        attempts.append(_attempt("channel-page", "initial-data", "empty", started, "no_video_entries"))
        attempts.append(_attempt("yt-dlp", "flat-playlist", "success", started))
    else:
        attempts.append(_attempt("channel-page", "initial-data", "success", started))
    return raw, source, attempts


def parse_playlist_id(value: str) -> str:
    value = (value or "").strip()
    if _PLAYLIST_ID_RE.fullmatch(value):
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com"):
        candidates = parse_qs(parsed.query).get("list") or []
        if candidates and _PLAYLIST_ID_RE.fullmatch(candidates[0]):
            return candidates[0]
    raise ValueError(f"not a YouTube playlist id or URL: {value!r}")


def acquire_playlist(playlist_id: str, limit: int) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """Data API when a key is configured, else yt-dlp flat enumeration (no key)."""
    attempts: list[dict[str, Any]] = []
    if os.environ.get("YOUTUBE_API_KEY"):
        from ..adapters import data_api

        started = time.monotonic()
        try:
            raw = data_api.playlist_items(playlist_id, limit=limit)
            if raw.get("playlist") is not None:
                attempts.append(_attempt("youtube-data-api", "playlist_items", "success", started))
                return raw, "api", attempts
            attempts.append(_attempt("youtube-data-api", "playlist_items", "unavailable", started, "not_found"))
        except (data_api.ApiAuthError, data_api.ApiQuotaError, data_api.ApiCallError) as exc:
            attempts.append(_attempt("youtube-data-api", "playlist_items", "error", started, _api_error_code(exc)))

    started = time.monotonic()
    try:
        raw = isolation.run_isolated(
            "youtube_mcp_v2.adapters.channel_scrape.fetch_playlist_items",
            playlist_id,
            limit,
            timeout_s=30,
        )
    except TimeoutError as exc:
        attempts.append(_attempt("yt-dlp", "flat-playlist", "timeout", started, "timeout"))
        raise BuildFailed("corpus_build_timeout", str(exc), attempts) from exc
    except RuntimeError as exc:
        attempts.append(_attempt("yt-dlp", "flat-playlist", "error", started, "provider_error"))
        raise BuildFailed("corpus_build_failed", str(exc), attempts) from exc
    attempts.append(_attempt("yt-dlp", "flat-playlist", "success", started))
    return raw, "yt-dlp", attempts


def acquire_topic(value: str, limit: int) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    from .skeleton_tools import _build_topic

    started = time.monotonic()
    try:
        raw = _build_topic(value, limit)
    except TimeoutError as exc:
        raise BuildFailed(
            "skeleton_build_timeout", str(exc), [_attempt("search-page", "initial-data", "timeout", started, "timeout")]
        ) from exc
    except RuntimeError as exc:
        raise BuildFailed(
            "skeleton_build_failed",
            str(exc),
            [_attempt("search-page", "initial-data", "error", started, "provider_error")],
        ) from exc
    return raw, "scrape", [_attempt("search-page", "initial-data", "success", started)]


def acquire_videos(
    refs: list[str], *, fetch_metadata: bool
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]], dict[str, str], list[str]]:
    """Explicit video set: cached metadata first, then the watch page (no key)."""
    from .. import cache
    from .corpus_compose import _video_from_cache
    from .inspect import inspect_video

    ids: list[str] = []
    for raw in refs:
        vid = parse_video_id(str(raw))  # ValueError -> caller error
        if vid not in ids:
            ids.append(vid)

    videos: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    attempts: list[dict[str, Any]] = []
    unavailable: list[str] = []
    fetched_any = False
    for vid in ids:
        if cache.get_video_meta(vid, fresh_only=False) is not None:
            sources[vid] = "cache"
        elif fetch_metadata:
            started = time.monotonic()
            env = inspect_video(vid)
            if env.get("error") is None:
                sources[vid] = "watch-page"
                fetched_any = True
                attempts.append({**_attempt("watch-page", "player-response", "success", started), "video_id": vid})
            else:
                sources[vid] = "unavailable"
                unavailable.append(vid)
                attempts.append(
                    {
                        **_attempt("watch-page", "player-response", "error", started, env["error"]["code"]),
                        "video_id": vid,
                    }
                )
        else:
            sources[vid] = "unavailable"
            unavailable.append(vid)
        videos.append(_video_from_cache(vid))
    return videos, ("watch-page" if fetched_any else "cache"), attempts, sources, unavailable


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _fail_with_attempts(exc: BuildFailed) -> dict[str, Any]:
    return envelope.fail(
        exc.code,
        str(exc),
        recoverable=exc.recoverable,
        provenance={"acquisition": {"attempts": exc.attempts}},
    )


def _build_response(payload: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    manifest = payload["manifest"]
    acquisition = manifest["acquisition"]
    envelope_source = {"api": "api", "cache": "cache"}.get(acquisition["provider_class"], "scrape")
    return envelope.ok(
        {
            "handle": payload["handle"],
            "schema_version": payload["schema_version"],
            "target": payload["target"],
            "value": payload["value"],
            "source": payload["source"],
            "count": len(payload["videos"]),
            "captured_at": manifest["captured_at"],
            "previous_handle": manifest["lineage"]["previous_handle"],
            "membership_sha256": manifest["membership"]["sha256"],
            "manifest_sha256": payload["manifest_sha256"],
            "limit_reached": manifest["origin"].get("limit_reached"),
        },
        source=envelope_source,
        warnings=warnings,
        provenance={"acquisition": acquisition, "server_version": manifest["server"]["version"]},
    )


def build_revision(
    target: str,
    value: str | None,
    *,
    videos: list[str] | None = None,
    limit: int = 50,
    fetch_metadata: bool = True,
) -> tuple[dict[str, Any] | None, list[str], dict[str, Any] | None]:
    """Shared implementation for corpus.build and legacy skeleton.build.

    Returns (payload, warnings, failure_envelope).
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    value = (value or "").strip()
    warnings: list[str] = []
    try:
        if target == "channel":
            if not value:
                return None, [], envelope.fail("bad_value", "value is empty", recoverable=False)
            raw, source, attempts = acquire_channel(value, limit)
            members = raw.get("videos", [])
            origin = {"canonical_url": None, "limit": limit, "limit_reached": len(members) >= limit}
            extra = {"channel": raw.get("channel")}
            entry_sources = None
        elif target == "playlist":
            try:
                playlist_id = parse_playlist_id(value)
            except ValueError as exc:
                return None, [], envelope.fail("bad_value", str(exc), recoverable=False)
            value = playlist_id
            raw, source, attempts = acquire_playlist(playlist_id, limit)
            members = raw.get("videos", [])
            origin = {
                "canonical_url": f"https://www.youtube.com/playlist?list={playlist_id}",
                "limit": limit,
                "limit_reached": len(members) >= limit,
            }
            extra = {"playlist": raw.get("playlist")}
            entry_sources = None
        elif target == "topic":
            if not value:
                return None, [], envelope.fail("bad_value", "value is empty", recoverable=False)
            raw, source, attempts = acquire_topic(value, limit)
            members = raw.get("videos", [])
            origin = {"canonical_url": None, "limit": limit, "limit_reached": len(members) >= limit}
            extra = {"channel": raw.get("channel")}
            entry_sources = None
        elif target == "videos":
            refs = list(videos or [])
            if not refs:
                return None, [], envelope.fail("bad_value", "videos must list at least one URL/ID", recoverable=False)
            if len(refs) > MAX_EXPLICIT_BUILD_VIDEOS:
                return None, [], envelope.fail(
                    "too_many_videos",
                    f"videos is capped at {MAX_EXPLICIT_BUILD_VIDEOS} for corpus.build; "
                    "use corpus.compose (cache-only) for larger sets",
                    recoverable=False,
                )
            try:
                members, source, attempts, entry_sources, unavailable = acquire_videos(
                    refs, fetch_metadata=fetch_metadata
                )
            except ValueError as exc:
                return None, [], envelope.fail("bad_video", str(exc), recoverable=False)
            value = value or "videos"
            origin = {"canonical_url": None, "requested": len(refs), "limit_reached": False}
            extra = {}
            if unavailable:
                warnings.append(
                    f"{len(unavailable)} videos kept as ID-only members: metadata was unavailable"
                )
        else:
            return None, [], envelope.fail(
                "bad_target",
                f"target must be 'channel', 'playlist', 'topic', or 'videos', got {target!r}",
                recoverable=False,
            )
    except BuildFailed as exc:
        return None, [], _fail_with_attempts(exc)

    try:
        payload = corpus_revision.create_revision(
            target=target,
            value=value,
            videos=members,
            source=source,
            origin=origin,
            attempts=attempts,
            entry_sources=entry_sources,
            extra=extra,
        )
    except FileExistsError:
        return None, [], envelope.fail(
            "skeleton_handle_collision",
            "refusing to overwrite an existing corpus revision handle",
            recoverable=True,
        )
    duplicates = payload["manifest"]["origin"].get("duplicate_ids_ignored") or []
    if duplicates:
        warnings.append(f"ignored {len(duplicates)} duplicate video IDs from the upstream listing")
    if not payload["videos"]:
        warnings.append("the captured revision has no members")
    return payload, warnings, None


def corpus_build(
    target: CorpusTarget,
    value: str | None = None,
    videos: list[str] | None = None,
    limit: int = 50,
    fetch_metadata: bool = True,
) -> dict[str, Any]:
    """Capture a new immutable corpus revision."""
    payload, warnings, failure = build_revision(
        target, value, videos=videos, limit=limit, fetch_metadata=fetch_metadata
    )
    if failure is not None:
        return failure
    return _build_response(payload, warnings)


def _load(handle: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        return skeleton.load_skeleton(handle), None
    except FileNotFoundError as exc:
        return None, envelope.fail("corpus_not_found", str(exc), recoverable=False)
    except ValueError as exc:
        return None, envelope.fail("bad_handle", str(exc), recoverable=False)
    except OSError as exc:
        return None, envelope.fail("corpus_unreadable", str(exc), recoverable=False)


def corpus_get(handle: str) -> dict[str, Any]:
    """Revision header, acquisition manifest, integrity, lifecycle, index revisions."""
    payload, failure = _load(handle)
    if failure is not None:
        return failure
    legacy = corpus_revision.is_legacy(payload)
    manifest = dict(corpus_revision.manifest_of(payload))
    entries = manifest.pop("entries", [])
    try:
        indexes = [
            {
                k: v
                for k, v in prepared.as_dict().items()
                if k in ("index_revision", "backend", "model_id", "requested_lang", "indexed_videos", "total_videos", "chunk_count")
            }
            for prepared in corpus_index.list_indexes(handle)
        ]
    except corpus_index.CorpusIndexError:
        indexes = []
    data = {
        "handle": payload.get("handle"),
        "schema_version": corpus_revision.LEGACY_SCHEMA_VERSION if legacy else payload["schema_version"],
        "legacy": legacy,
        "target": payload.get("target"),
        "value": payload.get("value"),
        "built_at": payload.get("built_at"),
        "source": payload.get("source"),
        "video_count": len(payload.get("videos") or []),
        "channel": payload.get("channel"),
        "playlist": payload.get("playlist"),
        "composition": payload.get("composition"),
        "parent_handle": payload.get("parent_handle"),
        "included_handles": payload.get("included_handles"),
        "lifecycle": {"expired_at": skeleton.expired_at(handle, payload)},
        "manifest": {**manifest, "entry_count": len(entries)},
        "manifest_sha256": payload.get("manifest_sha256"),
        "integrity": corpus_revision.integrity(payload),
        "index_revisions": indexes,
    }
    warnings = []
    if data["integrity"].get("checked") and not data["integrity"].get("ok"):
        warnings.append("stored hashes do not match the revision file; it was modified after capture")
    return envelope.ok(data, source="cache", warnings=warnings, validated=not warnings)


def corpus_list(
    handle: str,
    cursor: str | None = None,
    page_size: int = corpus_revision.DEFAULT_PAGE_SIZE,
    enrich: bool = False,
) -> dict[str, Any]:
    """One page of frozen membership; optional current enrichment kept beside it."""
    payload, failure = _load(handle)
    if failure is not None:
        return failure
    try:
        page = corpus_revision.membership_page(payload, cursor=cursor, page_size=page_size)
    except corpus_revision.CursorError as exc:
        return envelope.fail("bad_cursor", str(exc), recoverable=False)
    data: dict[str, Any] = {"handle": handle, **page}
    if enrich:
        data["enrichment"] = {
            "note": "current cache view at read time; not part of the frozen revision",
            "videos": corpus_revision.current_enrichment([str(i["id"]) for i in page["items"]]),
        }
    return envelope.ok(data, source="cache")


def corpus_index_tool(
    target: str | None = None,
    cursor: str | None = None,
    page_size: int = corpus_revision.DEFAULT_INDEX_PAGE_SIZE,
) -> dict[str, Any]:
    """Newest-first summaries of every corpus revision (legacy included)."""
    if target is not None and target not in corpus_revision.TARGETS:
        return envelope.fail(
            "bad_target", f"target must be one of {corpus_revision.TARGETS}", recoverable=False
        )
    try:
        page = corpus_revision.index_page(target=target, cursor=cursor, page_size=page_size)
    except corpus_revision.CursorError as exc:
        return envelope.fail("bad_cursor", str(exc), recoverable=False)
    return envelope.ok(page, source="cache")


def corpus_diff(
    head_handle: str,
    base_handle: str | None = None,
    ignore_volatile: bool = True,
    max_items: int = corpus_revision.DEFAULT_DIFF_ITEMS,
) -> dict[str, Any]:
    """Diff two frozen revisions; base defaults to the head's previous revision."""
    head, failure = _load(head_handle)
    if failure is not None:
        return failure
    if base_handle is None:
        base_handle = (
            None
            if corpus_revision.is_legacy(head)
            else head["manifest"].get("lineage", {}).get("previous_handle")
        )
        if base_handle is None:
            return envelope.fail(
                "no_base_revision",
                "head has no recorded previous revision; pass base_handle explicitly",
                recoverable=False,
            )
    base, failure = _load(base_handle)
    if failure is not None:
        return failure
    try:
        result = corpus_revision.diff_revisions(
            base, head, ignore_volatile=ignore_volatile, max_items=max_items
        )
    except corpus_revision.CorpusRevisionError as exc:
        return envelope.fail("corpus_diff_invalid", str(exc), recoverable=False)
    warnings = []
    if not result["scope"]["same_scope"]:
        warnings.append("revisions capture different scopes; added/removed reflect scope, not change over time")
    if result["transcript_changed"]["comparison"] == "unavailable":
        warnings.append(result["transcript_changed"]["reason"])
    if result["truncated"]:
        warnings.append(f"lists truncated to max_items={max_items}; counts are complete")
    return envelope.ok(result, source="cache", warnings=warnings)
