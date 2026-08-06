# Changelog

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
