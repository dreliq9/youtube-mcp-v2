# Changelog

## Unreleased — v0.3 corpus revisions

### Added
- `corpus.build(target='channel'|'playlist'|'topic'|'videos')` captures immutable corpus revisions with a versioned acquisition manifest (server version, capture time, canonical IDs/URLs, per-video metadata hashes, capture-time metadata/transcript revisions, provider attempts, lineage).
- Playlist corpora (Data API when keyed, otherwise yt-dlp) and explicit video-set corpora (cache, then watch page) work without an API key.
- `corpus.get`, cursor-paginated `corpus.list` and `corpus.index`, and `corpus.diff` with separate added/removed/metadata/transcript/provenance sections.
- `CORPUS_REVISIONS.md` documents the revision and manifest schema.

### Changed
- `skeleton.build` and `corpus.compose` now write manifest-bearing revisions in the same storage; old files are untouched and readable as legacy revisions.
- The `skeleton.*` tools are only registered when `YOUTUBE_MCP_LEGACY_SKELETON_TOOLS=1` (default tool count 22 → 21).
- Expiring a v0.3 revision writes a lifecycle sidecar instead of rewriting the revision file; legacy files keep the v0.2 in-place behavior.
- Data API playlist enumeration preserves playlist order.

## 0.2.1 — unreleased

### Compatibility
- Migrated the server to the MCP Python SDK v2 high-level `MCPServer` API.
- Bounded the `mcp` dependency to the v2 major line.
- Added the previously implicit `python-dotenv` dependency.
- Added Python 3.10–3.13 and Linux/macOS/Windows CI coverage.

### Packaging
- Added `media` and `audio` extras for yt-dlp-backed media extraction while preserving the existing `frame` extra.
- Declared pytest markers for deterministic vs live-network tests.

### Transcript correctness
- Cache now preserves requested language separately from the actual fallback language.
- Cache now preserves generated-caption status when the provider exposes it.
- Existing SQLite caches receive additive column migration; historical rows are not rewritten.
- `transcript.get` resolves duration for the word-rate gate even when `inspect.video` was not called first.
- If duration cannot be resolved, the transcript is returned with `validated=false` rather than claiming a complete validation pass.
- Invalid transcript modes and invalid chunk budgets now return explicit caller errors instead of silent coercion.
- Negative timed cursors are clamped to the beginning rather than indexing from the end.

### Snapshot correctness
- Skeleton handles now include microseconds while remaining backward-compatible with legacy second-resolution handles.
- New skeleton files use exclusive creation, so a handle collision can never silently overwrite an earlier frozen snapshot.
- `skeleton.expire` remains the one documented in-place metadata mutation and does not alter captured membership.

### Documentation
- Updated YouTube `search.list` quota documentation for the June 2026 granular quota model.
- Added `ROADMAP.md` for the acquisition → corpus → semantic → multimodal → distribution sequence.
- Added `EVIDENCE_MODEL.md` for provenance/validation/artifact schema evolution.
- Added `BENCHMARK.md` defining the proposed public YouTube-MCP Bench.

## 0.2.0 — 2026-04-30

- Initial youtube-mcp-v2 release.
- 13-tool skeleton-first research architecture across no-key and Data API tiers.
- Uniform response envelope, append-only SQLite cache, transcript validation, subprocess isolation, frames, search, and frozen skeletons.
- Audio extraction was added in July 2026, bringing the public tool count to 14.
