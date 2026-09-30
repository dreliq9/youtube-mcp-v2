"""Immutable corpus revisions with a versioned acquisition manifest.

A corpus revision is a frozen membership list plus the provenance needed to
reproduce and audit it later. It is stored in the same file format and
directory as historical `skeleton.*` snapshots (see `skeleton.py`), so every
existing handle is readable as a *legacy* corpus revision and every
corpus-scoped tool (hydrate/prepare/search/visual/clip-plan/compose) accepts
both kinds without translation.

Layers are kept separate:

* **membership** (`videos`): the captured records, in capture order. Never
  rewritten after the revision file is created.
* **manifest** (`manifest`): server version, capture time, origin, acquisition
  provider/method/attempts, per-video metadata hashes, and the transcript
  revisions that were cached at capture time. Written once, with the
  membership, and hash-verified on read.
* **lifecycle** (expiration): a sidecar file, see `skeleton.expire_skeleton`.
* **enrichment** (current cached metadata/transcript status): computed at read
  time and returned *beside* membership, never merged into it.

Rebuilding a scope always creates a new handle whose manifest links the
previous revision of the same scope (`lineage.previous_handle`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any

from . import __version__, cache, skeleton

SCHEMA_VERSION = "youtube-mcp.corpus/1"
LEGACY_SCHEMA_VERSION = "youtube-mcp.skeleton/legacy"
MANIFEST_VERSION = 1

TARGETS = ("channel", "playlist", "topic", "videos", "collection")
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
DEFAULT_INDEX_PAGE_SIZE = 50
MAX_INDEX_PAGE_SIZE = 200
DEFAULT_DIFF_ITEMS = 200

# Fields that change between captures without the video itself changing.
VOLATILE_FIELDS = frozenset({"view_count", "thumbnail_url"})
_RELATIVE_TIME_RE = re.compile(r"\b(ago|streamed|premiered)\b", re.IGNORECASE)

# Revision-level `source` → (provider, method, provider_class). provider_class
# follows EVIDENCE_MODEL.md (`cache`, `api`, `scrape`, `local`).
ACQUISITION_PROVIDERS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("channel", "api"): ("youtube-data-api", "channels+playlistItems+videos", "api"),
    ("channel", "scrape"): ("channel-page", "initial-data", "scrape"),
    ("channel", "yt-dlp"): ("yt-dlp", "flat-playlist", "scrape"),
    ("playlist", "api"): ("youtube-data-api", "playlists+playlistItems+videos", "api"),
    ("playlist", "yt-dlp"): ("yt-dlp", "flat-playlist", "scrape"),
    ("topic", "scrape"): ("search-page", "initial-data", "scrape"),
    ("videos", "watch-page"): ("watch-page", "player-response", "scrape"),
    ("videos", "cache"): ("cache", "video-meta", "cache"),
    ("collection", "compose"): ("compose", "membership-union", "cache"),
}


class CorpusRevisionError(ValueError):
    """A corpus revision is malformed or cannot be compared/paginated."""


class CursorError(CorpusRevisionError):
    pass


# ---------------------------------------------------------------------------
# Hashing helpers (never Python's randomized hash())
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def is_legacy(payload: dict[str, Any]) -> bool:
    return payload.get("schema_version") != SCHEMA_VERSION or "manifest" not in payload


def acquisition_for(target: str, source: str | None) -> dict[str, Any]:
    provider, method, provider_class = ACQUISITION_PROVIDERS.get(
        (target, source or ""), (source or "unknown", "unknown", "unknown")
    )
    return {"provider": provider, "method": method, "provider_class": provider_class}


# ---------------------------------------------------------------------------
# Capture-time cache observations
# ---------------------------------------------------------------------------


def _transcript_row_sha256(row: dict[str, Any]) -> str:
    # Same identity inputs as corpus_index._transcript_hash, so a manifest's
    # transcript revision can be matched against the index built from it.
    return sha256_json(
        {
            "id": row.get("id"),
            "video_id": row.get("video_id"),
            "requested_lang": row.get("lang"),
            "actual_lang": row.get("actual_lang"),
            "is_generated": row.get("is_generated"),
            "text": row.get("text"),
            "segments_json": row.get("segments_json"),
            "validated": row.get("validated"),
            "warnings_json": row.get("warnings_json"),
            "fetched_at": row.get("fetched_at"),
        }
    )


def _cache_observations(video_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Latest cached metadata/transcript revisions per video at capture time."""
    observed: dict[str, dict[str, Any]] = {vid: {"metadata": None, "transcripts": {}} for vid in video_ids}
    if not video_ids or not cache.CACHE_PATH.exists():
        return observed
    with cache.connect() as conn:
        for vid in video_ids:
            meta = conn.execute(
                "SELECT id, payload_json, fetched_at FROM video_meta WHERE video_id = ? "
                "ORDER BY fetched_at DESC, id DESC LIMIT 1",
                (vid,),
            ).fetchone()
            if meta is not None:
                observed[vid]["metadata"] = {
                    "row_id": meta["id"],
                    "fetched_at": meta["fetched_at"],
                    "sha256": hashlib.sha256(meta["payload_json"].encode("utf-8")).hexdigest(),
                }
            rows = conn.execute(
                "SELECT * FROM transcripts WHERE video_id = ? ORDER BY lang, fetched_at DESC, id DESC",
                (vid,),
            ).fetchall()
            for row in rows:
                row = dict(row)
                lang = row["lang"]
                if lang in observed[vid]["transcripts"]:
                    continue  # rows are newest-first within a lang
                observed[vid]["transcripts"][lang] = {
                    "row_id": row["id"],
                    "fetched_at": row["fetched_at"],
                    "actual_lang": row.get("actual_lang"),
                    "is_generated": None if row.get("is_generated") is None else bool(row["is_generated"]),
                    "provider": row.get("provider"),
                    "validated": bool(row.get("validated")),
                    "sha256": _transcript_row_sha256(row),
                }
    return observed


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _membership_hashes(videos: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(videos),
        "sha256": sha256_json(videos),
        "ids_sha256": sha256_json([v.get("id") for v in videos]),
    }


def build_manifest(
    *,
    target: str,
    value: str,
    built_at: str,
    videos: list[dict[str, Any]],
    source: str,
    origin: dict[str, Any],
    attempts: list[dict[str, Any]],
    entry_sources: dict[str, str] | None = None,
    previous_handle: str | None = None,
) -> dict[str, Any]:
    ids = [str(v["id"]) for v in videos]
    observed = _cache_observations(ids)
    acquisition = acquisition_for(target, source)
    entries = []
    for position, video in enumerate(videos):
        vid = str(video["id"])
        entries.append(
            {
                "position": position,
                "id": vid,
                "url": canonical_url(vid),
                "metadata_sha256": sha256_json(video),
                "metadata_source": (entry_sources or {}).get(vid, acquisition["provider"]),
                "metadata_revision": observed[vid]["metadata"],
                "transcript_revisions": observed[vid]["transcripts"],
            }
        )
    return {
        "manifest_version": MANIFEST_VERSION,
        "server": {"name": "youtube-mcp-v2", "version": __version__},
        "captured_at": built_at,
        "origin": {"target": target, "value": value, **origin},
        "acquisition": {**acquisition, "attempts": attempts},
        "lineage": {"previous_handle": previous_handle},
        "membership": _membership_hashes(videos),
        "entries": entries,
    }


def derive_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    """Read-only manifest view of a historical skeleton file (never written)."""
    videos = payload.get("videos") or []
    target = payload.get("target") or "unknown"
    acquisition = acquisition_for(target, payload.get("source"))
    return {
        "manifest_version": MANIFEST_VERSION,
        "derived": True,
        "server": {"name": "youtube-mcp-v2", "version": None},
        "captured_at": payload.get("built_at"),
        "origin": {"target": target, "value": payload.get("value")},
        "acquisition": {**acquisition, "attempts": []},
        "lineage": {"previous_handle": payload.get("parent_handle")},
        "membership": _membership_hashes(videos),
        "entries": [
            {
                "position": position,
                "id": v.get("id"),
                "url": canonical_url(str(v.get("id"))),
                "metadata_sha256": sha256_json(v),
                "metadata_source": acquisition["provider"],
                "metadata_revision": None,
                # Not recorded by v0.2 skeletons; diff reports "unavailable".
                "transcript_revisions": None,
            }
            for position, v in enumerate(videos)
        ],
    }


def manifest_of(payload: dict[str, Any]) -> dict[str, Any]:
    return derive_manifest(payload) if is_legacy(payload) else payload["manifest"]


def integrity(payload: dict[str, Any]) -> dict[str, Any]:
    """Recompute stored hashes; any mismatch means the file was edited."""
    if is_legacy(payload):
        return {"checked": False, "reason": "legacy skeleton files carry no stored hashes"}
    manifest = payload["manifest"]
    videos = payload.get("videos") or []
    membership_ok = manifest.get("membership") == _membership_hashes(videos)
    entries_ok = [e.get("metadata_sha256") for e in manifest.get("entries", [])] == [
        sha256_json(v) for v in videos
    ]
    manifest_ok = payload.get("manifest_sha256") == sha256_json(manifest)
    return {
        "checked": True,
        "ok": bool(membership_ok and entries_ok and manifest_ok),
        "membership_sha256_ok": membership_ok,
        "entry_hashes_ok": entries_ok,
        "manifest_sha256_ok": manifest_ok,
    }


# ---------------------------------------------------------------------------
# Create / index
# ---------------------------------------------------------------------------


def _dedupe(videos: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    dupes: list[str] = []
    for video in videos:
        vid = video.get("id")
        if not isinstance(vid, str) or not vid:
            continue
        if vid in seen:
            dupes.append(vid)
            continue
        seen.add(vid)
        out.append(video)
    return out, dupes


def create_revision(
    *,
    target: str,
    value: str,
    videos: list[dict[str, Any]],
    source: str,
    origin: dict[str, Any] | None = None,
    attempts: list[dict[str, Any]] | None = None,
    entry_sources: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
    previous_handle: str | None = None,
) -> dict[str, Any]:
    """Write a new immutable revision (exclusive create) and return its payload.

    Lineage defaults to the latest revision of the same (target, value) scope;
    derived revisions (e.g. `corpus.compose(base_handle=...)`) pass their parent.
    """
    if target not in TARGETS:
        raise CorpusRevisionError(f"unsupported corpus target: {target!r}")
    videos, duplicates = _dedupe([dict(v) for v in videos])
    previous = {"handle": previous_handle} if previous_handle else latest_for_scope(target, value)
    handle = skeleton.make_handle(target, value)
    built_at = skeleton._now_iso()
    manifest = build_manifest(
        target=target,
        value=value,
        built_at=built_at,
        videos=videos,
        source=source,
        origin={**(origin or {}), "duplicate_ids_ignored": duplicates},
        attempts=list(attempts or []),
        entry_sources=entry_sources,
        previous_handle=previous["handle"] if previous else None,
    )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "handle": handle,
        "target": target,
        "value": value,
        "built_at": built_at,
        "source": source,
        # Kept for v0.2 readers; lifecycle for this revision lives in a sidecar.
        "expired_at": None,
        "channel": None,
        **(extra or {}),
        "videos": videos,
        "manifest": manifest,
    }
    payload["manifest_sha256"] = sha256_json(manifest)
    skeleton.save_skeleton(payload)
    return payload


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    handle = data.get("handle")
    legacy = is_legacy(data)
    return {
        "handle": handle,
        "schema_version": LEGACY_SCHEMA_VERSION if legacy else data.get("schema_version"),
        "legacy": legacy,
        "target": data.get("target"),
        "value": data.get("value"),
        "built_at": data.get("built_at"),
        "source": data.get("source"),
        "video_count": len(data.get("videos") or []),
        "expired_at": skeleton.expired_at(handle, data) if isinstance(handle, str) else None,
        "previous_handle": None if legacy else data["manifest"].get("lineage", {}).get("previous_handle"),
    }


def _all_revisions() -> tuple[list[dict[str, Any]], int]:
    skeleton._ensure_dirs()
    found: list[dict[str, Any]] = []
    unreadable = 0
    for path in skeleton.SKELETON_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            unreadable += 1
            continue
        if not isinstance(data, dict) or not skeleton.is_valid_handle(str(data.get("handle", ""))):
            unreadable += 1
            continue
        found.append(data)
    return found, unreadable


_HANDLE_STAMP_RE = re.compile(r"(\d{8}-\d{6})(?:-(\d{6}))?$")


def _sort_key(summary: dict[str, Any]) -> tuple[str, str, str]:
    # built_at has second resolution; the handle's microsecond stamp orders
    # revisions captured within the same second, across scopes.
    handle = str(summary.get("handle") or "")
    match = _HANDLE_STAMP_RE.search(handle)
    stamp = f"{match.group(1)}-{match.group(2) or '000000'}" if match else ""
    return (str(summary.get("built_at") or ""), stamp, handle)


def latest_for_scope(target: str, value: str) -> dict[str, Any] | None:
    matches = [
        _summary(d)
        for d in _all_revisions()[0]
        if d.get("target") == target and d.get("value") == value
    ]
    return max(matches, key=_sort_key) if matches else None


# ---------------------------------------------------------------------------
# Cursors (opaque, bound to what they paginate)
# ---------------------------------------------------------------------------


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = canonical_json(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, kind: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError) as exc:
        raise CursorError(f"malformed cursor: {cursor!r}") from exc
    if not isinstance(data, dict) or data.get("k") != kind:
        raise CursorError(f"cursor is not a {kind} cursor")
    return data


def _clamp(value: int, default: int, maximum: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def index_page(
    *, target: str | None = None, cursor: str | None = None, page_size: int = DEFAULT_INDEX_PAGE_SIZE
) -> dict[str, Any]:
    """Newest-first revision summaries with a keyset cursor.

    The cursor records the last (built_at, handle) returned, so revisions
    created while paging never shift or duplicate entries on later pages.
    """
    page_size = _clamp(page_size, DEFAULT_INDEX_PAGE_SIZE, MAX_INDEX_PAGE_SIZE)
    revisions, unreadable = _all_revisions()
    summaries = sorted(
        (_summary(d) for d in revisions if not target or d.get("target") == target),
        key=_sort_key,
        reverse=True,
    )
    total = len(summaries)
    if cursor:
        state = _decode_cursor(cursor, "index")
        if state.get("t") != target:
            raise CursorError("cursor was issued for a different target filter")
        after = _sort_key({"built_at": state.get("b", ""), "handle": state.get("h", "")})
        summaries = [s for s in summaries if _sort_key(s) < after]
    page = summaries[:page_size]
    next_cursor = None
    if len(summaries) > page_size:
        last = page[-1]
        next_cursor = _encode_cursor(
            {"k": "index", "t": target, "b": last["built_at"] or "", "h": last["handle"]}
        )
    return {
        "revisions": page,
        "total": total,
        "page_size": page_size,
        "next_cursor": next_cursor,
        "unreadable_files": unreadable,
    }


def membership_page(
    payload: dict[str, Any], *, cursor: str | None = None, page_size: int = DEFAULT_PAGE_SIZE
) -> dict[str, Any]:
    """One page of frozen membership, each item paired with its capture entry."""
    page_size = _clamp(page_size, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    videos = payload.get("videos") or []
    manifest = manifest_of(payload)
    membership_sha = manifest["membership"]["sha256"]
    offset = 0
    if cursor:
        state = _decode_cursor(cursor, "members")
        if state.get("h") != payload.get("handle") or state.get("m") != membership_sha[:16]:
            raise CursorError("cursor belongs to a different corpus revision")
        offset = int(state.get("o", 0))
        if offset < 0 or offset > len(videos):
            raise CursorError("cursor offset out of range")
    entries = manifest.get("entries") or []
    items = [
        {
            "position": i,
            "id": videos[i].get("id"),
            "url": canonical_url(str(videos[i].get("id"))),
            "record": videos[i],
            "capture": {k: v for k, v in entries[i].items() if k not in ("position", "id", "url")}
            if i < len(entries)
            else None,
        }
        for i in range(offset, min(offset + page_size, len(videos)))
    ]
    end = offset + len(items)
    next_cursor = (
        _encode_cursor({"k": "members", "h": payload.get("handle"), "m": membership_sha[:16], "o": end})
        if end < len(videos)
        else None
    )
    return {
        "items": items,
        "total": len(videos),
        "offset": offset,
        "page_size": page_size,
        "next_cursor": next_cursor,
    }


# ---------------------------------------------------------------------------
# Enrichment (read-time, never persisted into a revision)
# ---------------------------------------------------------------------------

_ENRICH_FIELDS = (
    "title",
    "channel",
    "channel_id",
    "duration_s",
    "has_transcript",
    "lang_default",
    "available_caption_langs",
    "view_count",
)


def current_enrichment(video_ids: list[str]) -> dict[str, Any]:
    observed = _cache_observations(video_ids)
    out: dict[str, Any] = {}
    for vid in video_ids:
        meta_hit = cache.get_video_meta(vid, fresh_only=False) if cache.CACHE_PATH.exists() else None
        meta = meta_hit[0] if meta_hit else None
        out[vid] = {
            "metadata": {f: meta.get(f) for f in _ENRICH_FIELDS if f in meta} if meta else None,
            "metadata_revision": observed[vid]["metadata"],
            "cached_transcripts": {
                lang: {k: rev[k] for k in ("row_id", "fetched_at", "actual_lang", "validated", "sha256")}
                for lang, rev in observed[vid]["transcripts"].items()
            },
        }
    return out


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _is_volatile(field: str, before: Any, after: Any) -> bool:
    if field in VOLATILE_FIELDS:
        return True
    if field == "published":
        # Scraped pages give relative text ("3 days ago") that drifts daily.
        return any(isinstance(v, str) and _RELATIVE_TIME_RE.search(v) for v in (before, after))
    return False


def _index(videos: list[dict[str, Any]], label: str) -> dict[str, tuple[int, dict[str, Any]]]:
    out: dict[str, tuple[int, dict[str, Any]]] = {}
    for position, video in enumerate(videos):
        vid = video.get("id")
        if not isinstance(vid, str) or not vid:
            raise CorpusRevisionError(f"{label} video at index {position} has no usable id")
        if vid in out:
            raise CorpusRevisionError(f"{label} contains duplicate video id {vid}")
        out[vid] = (position, video)
    return out


def _lean(video: dict[str, Any], position: int) -> dict[str, Any]:
    vid = str(video.get("id"))
    return {"id": vid, "position": position, "url": canonical_url(vid), "title": video.get("title")}


def _cap(items: list[Any], limit: int) -> tuple[list[Any], int]:
    return items[:limit], max(0, len(items) - limit)


def _pick_rev(rev: dict[str, Any] | None) -> dict[str, Any] | None:
    if rev is None:
        return None
    return {k: rev.get(k) for k in ("row_id", "fetched_at", "actual_lang", "sha256")}


def _transcript_changes(
    vid: str, before: dict[str, Any] | None, after: dict[str, Any] | None
) -> list[dict[str, Any]]:
    before = before or {}
    after = after or {}
    changes = []
    for lang in sorted(set(before) | set(after)):
        b, a = before.get(lang), after.get(lang)
        if b is not None and a is not None and b.get("sha256") == a.get("sha256"):
            continue
        kind = "added" if b is None else "removed" if a is None else "changed"
        changes.append({"id": vid, "lang": lang, "change": kind, "before": _pick_rev(b), "after": _pick_rev(a)})
    return changes


def diff_revisions(
    base: dict[str, Any],
    head: dict[str, Any],
    *,
    ignore_volatile: bool = True,
    max_items: int = DEFAULT_DIFF_ITEMS,
) -> dict[str, Any]:
    """Compare two frozen revisions using only what each captured.

    Reported separately: added, removed, metadata_changed, transcript_changed
    (capture-time transcript/evidence revisions), and provenance_changed.
    Current cache state is never consulted, so the diff is reproducible.
    """
    max_items = _clamp(max_items, DEFAULT_DIFF_ITEMS, 2000)
    base_m, head_m = manifest_of(base), manifest_of(head)
    base_by = _index(base.get("videos") or [], "base")
    head_by = _index(head.get("videos") or [], "head")
    base_entries = {e["id"]: e for e in base_m["entries"]}
    head_entries = {e["id"]: e for e in head_m["entries"]}

    added_ids = [vid for vid in head_by if vid not in base_by]
    removed_ids = [vid for vid in base_by if vid not in head_by]
    shared = [vid for vid in head_by if vid in base_by]

    base_fields = {k for _p, v in base_by.values() for k in v}
    head_fields = {k for _p, v in head_by.values() for k in v}
    common_fields = base_fields & head_fields

    transcripts_comparable = not base_m.get("derived") and not head_m.get("derived")

    metadata_changed: list[dict[str, Any]] = []
    transcript_changed: list[dict[str, Any]] = []
    per_video_provenance: list[dict[str, Any]] = []
    volatile_only = 0
    unchanged = 0
    for vid in shared:
        _bp, before = base_by[vid]
        hp, after = head_by[vid]
        changes: dict[str, Any] = {}
        volatile: dict[str, Any] = {}
        for field in sorted(common_fields):
            b, a = before.get(field), after.get(field)
            if b == a:
                continue
            (volatile if ignore_volatile and _is_volatile(field, b, a) else changes)[field] = {
                "before": b,
                "after": a,
            }
        if changes:
            metadata_changed.append({"id": vid, "position": hp, "changes": changes})
        elif volatile:
            volatile_only += 1

        t_changes = (
            _transcript_changes(
                vid,
                base_entries.get(vid, {}).get("transcript_revisions"),
                head_entries.get(vid, {}).get("transcript_revisions"),
            )
            if transcripts_comparable
            else []
        )
        transcript_changed.extend(t_changes)

        b_src = base_entries.get(vid, {}).get("metadata_source")
        a_src = head_entries.get(vid, {}).get("metadata_source")
        if b_src != a_src:
            per_video_provenance.append({"id": vid, "before": b_src, "after": a_src})

        if not changes and not t_changes and b_src == a_src:
            unchanged += 1

    def acq(m: dict[str, Any]) -> dict[str, Any]:
        a = m.get("acquisition") or {}
        return {k: a.get(k) for k in ("provider", "method", "provider_class")}

    revision_provenance = {
        "changed": acq(base_m) != acq(head_m),
        "before": acq(base_m),
        "after": acq(head_m),
        "server_version": {
            "before": (base_m.get("server") or {}).get("version"),
            "after": (head_m.get("server") or {}).get("version"),
        },
    }

    added, added_more = _cap([_lean(head_by[v][1], head_by[v][0]) for v in added_ids], max_items)
    removed, removed_more = _cap([_lean(base_by[v][1], base_by[v][0]) for v in removed_ids], max_items)
    meta_out, meta_more = _cap(metadata_changed, max_items)
    tr_out, tr_more = _cap(transcript_changed, max_items)
    prov_out, prov_more = _cap(per_video_provenance, max_items)

    base_scope = (base.get("target"), base.get("value"))
    head_scope = (head.get("target"), head.get("value"))
    return {
        "base_handle": base.get("handle"),
        "head_handle": head.get("handle"),
        "scope": {
            "same_scope": base_scope == head_scope,
            "base": {"target": base_scope[0], "value": base_scope[1]},
            "head": {"target": head_scope[0], "value": head_scope[1]},
        },
        "base_captured_at": base_m.get("captured_at"),
        "head_captured_at": head_m.get("captured_at"),
        "legacy": {"base": is_legacy(base), "head": is_legacy(head)},
        "counts": {
            "base": len(base_by),
            "head": len(head_by),
            "added": len(added_ids),
            "removed": len(removed_ids),
            "metadata_changed": len(metadata_changed),
            "volatile_only": volatile_only,
            "transcript_changed": len({c["id"] for c in transcript_changed}),
            "provenance_changed": len(per_video_provenance),
            "unchanged": unchanged,
        },
        "added": added,
        "removed": removed,
        "metadata_changed": meta_out,
        "transcript_changed": {
            "comparison": "capture-time" if transcripts_comparable else "unavailable",
            "reason": None
            if transcripts_comparable
            else "a legacy skeleton revision did not record capture-time transcript revisions",
            "changes": tr_out,
        },
        "provenance_changed": {"revision": revision_provenance, "videos": prov_out},
        "schema": {
            "fields_only_in_base": sorted(base_fields - head_fields),
            "fields_only_in_head": sorted(head_fields - base_fields),
        },
        "ignored_volatile_fields": sorted(VOLATILE_FIELDS | {"published (relative text)"})
        if ignore_volatile
        else [],
        "truncated": {
            k: n
            for k, n in (
                ("added", added_more),
                ("removed", removed_more),
                ("metadata_changed", meta_more),
                ("transcript_changed", tr_more),
                ("provenance_changed", prov_more),
            )
            if n
        },
    }
