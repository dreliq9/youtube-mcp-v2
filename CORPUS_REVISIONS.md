# Corpus revisions

A **corpus revision** is a frozen, ordered membership list of YouTube videos plus the provenance needed to reproduce and audit it later. It is the v0.3 generalization of the v0.2 `skeleton.*` snapshot.

## Rules

1. **Immutable.** A revision file is created once (exclusive create) and never rewritten. Rebuilding a scope creates a new handle.
2. **Lineage.** Each new revision records the previous revision of the same scope (`manifest.lineage.previous_handle`), or its parent for `corpus.compose(base_handle=...)`, so `corpus.diff(head)` needs no base argument.
3. **Layers stay separate.**
   - *membership* — `videos`, the captured records in capture order;
   - *manifest* — how and when they were captured (written once with the membership and hash-checked on read);
   - *lifecycle* — expiration, stored in a sidecar `skeletons/_lifecycle/<handle>.json`;
   - *enrichment* — the current cache view, computed at read time by `corpus.list(enrich=true)` and returned beside the membership, never merged into it.
4. **Reproducible queries.** Index revisions (`corpus.prepare`) are content-addressed per handle. `corpus.search(handle_A, ..., index_revision=idx_A)` returns the same evidence after revision B of the same scope exists and transcripts have been refreshed.
5. **Legacy data is not orphaned.** Every historical `chan-*`, `topic-*`, `set-*` skeleton file is a valid *legacy* corpus revision for `corpus.get/list/index/diff` and every corpus-scoped tool. A read-only manifest is derived for it (`derived: true`); nothing is migrated or rewritten.

## Targets

| target | handle prefix | `value` | no-key path | keyed path |
|---|---|---|---|---|
| `channel` | `chan-` | channel ID, `@handle`, or URL | channel page → yt-dlp | Data API uploads playlist |
| `playlist` | `plist-` | playlist ID or any YouTube URL with `list=` | yt-dlp `--flat-playlist` | Data API `playlists` + `playlistItems` |
| `topic` | `topic-` | search query | search page | — |
| `videos` | `vids-` | optional label; members via `videos=[...]` (≤100) | cached metadata → watch page; ID-only member if unavailable | — |
| `collection` | `set-` | label (via `corpus.compose`) | cache only | — |

`fetch_metadata=false` makes an explicit set fully offline (cache or ID-only).

## File schema (`youtube-mcp.corpus/1`)

```jsonc
{
  "schema_version": "youtube-mcp.corpus/1",
  "handle": "plist-PL...-20260930-051000-123456",
  "target": "playlist",
  "value": "PL...",
  "built_at": "2026-09-30T05:10:00Z",
  "source": "yt-dlp",                 // v0.2-compatible acquisition label
  "expired_at": null,                 // always null; lifecycle lives in the sidecar
  "channel": null, "playlist": {...}, // target-specific header fields
  "videos": [ {...}, ... ],           // frozen membership, capture order
  "manifest": {
    "manifest_version": 1,
    "server": {"name": "youtube-mcp-v2", "version": "0.2.1"},
    "captured_at": "2026-09-30T05:10:00Z",
    "origin": {"target": "playlist", "value": "PL...", "canonical_url": "https://www.youtube.com/playlist?list=PL...",
               "limit": 50, "limit_reached": false, "duplicate_ids_ignored": []},
    "acquisition": {"provider": "yt-dlp", "method": "flat-playlist", "provider_class": "scrape",
                    "attempts": [{"provider": "youtube-data-api", "method": "playlist_items", "outcome": "error",
                                  "detail_code": "quota_exceeded", "duration_ms": 120}, ...]},
    "lineage": {"previous_handle": "plist-PL...-20260901-..."},
    "membership": {"count": 2, "sha256": "<sha256 of canonical videos JSON>", "ids_sha256": "..."},
    "entries": [
      {"position": 0, "id": "...", "url": "https://www.youtube.com/watch?v=...",
       "metadata_sha256": "<sha256 of this member record>",
       "metadata_source": "yt-dlp",
       "metadata_revision": {"row_id": 12, "fetched_at": "...", "sha256": "..."} ,   // latest cached video_meta row, or null
       "transcript_revisions": {"en": {"row_id": 40, "fetched_at": "...", "actual_lang": "en",
                                        "is_generated": false, "provider": "...", "validated": true,
                                        "sha256": "<same identity hash corpus.prepare records>"}}}
    ]
  },
  "manifest_sha256": "<sha256 of canonical manifest JSON>"
}
```

Attempt records deliberately omit upstream error messages (they can echo proxy URLs). Hashes use canonical JSON (sorted keys, compact separators) and SHA-256, never Python's `hash()`.

`corpus.get` recomputes the membership, per-entry, and manifest hashes; a mismatch is reported in `integrity` with `validated=false`.

## Pagination

`corpus.list` (default 100, max 500 per page) and `corpus.index` (default 50, max 200, newest first) return an opaque `next_cursor`. Cursors are bound to what they paginate: a membership cursor only works for the revision (handle + membership hash) that issued it, and an index cursor only for the same `target` filter. The index cursor is a keyset position, so revisions created while paging never shift or duplicate later pages.

## Diff

`corpus.diff(head_handle, base_handle=None, ignore_volatile=True, max_items=200)` compares only what each revision captured (never the current cache), so the result is reproducible. Sections:

- `added` / `removed` — members by id with position;
- `metadata_changed` — per-field before/after for fields both revisions captured (schema differences are listed separately in `schema`);
- `volatile_only` count — view counts, thumbnails, and relative publish text ("3 days ago") are ignored by default;
- `transcript_changed` — per-language capture-time transcript revision added/removed/changed; `comparison: "unavailable"` when either side is a legacy revision;
- `provenance_changed` — revision-level provider/method/class and server version, plus per-member metadata source changes;
- `counts` are always complete; lists are capped at `max_items` with `truncated` counts.

Cross-scope diffs are allowed (useful for derived collections) and flagged with a warning.

## Legacy compatibility

`skeleton.build` writes the same manifest-bearing revisions and keeps its v0.2 response shape. The `skeleton.*` tools are only registered when `YOUTUBE_MCP_LEGACY_SKELETON_TOOLS=1`; see the mapping table in `README.md`. `skeleton.expire` keeps rewriting `expired_at` in place for legacy files only; for v0.3 revisions it writes the lifecycle sidecar.
