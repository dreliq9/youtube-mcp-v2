# Changelog

## Unreleased — v0.4 evidence search (issue #5)

### Local STT
- Local transcription is now a backend interface (`adapters/local_stt.py`) with an optional **mlx-whisper** backend for Apple Silicon (`stt-mlx` extra) beside the existing portable whisper.cpp backend. `YOUTUBE_MCP_STT_BACKEND=auto|mlx-whisper|whisper.cpp|off`; `YOUTUBE_MCP_STT_LANGUAGE` hint.
- Both backends persist the same `youtube-mcp.local-stt/1` provenance: model identity + revision, decode config, source-audio hash/duration, and segment timing summary. Paths are never recorded.

### Corpus search
- `corpus.search` gains `filters` (video/channel/time window/validation/acquisition method/generated status).
- Hits add `evidence_id`, `anchor` (exact matching segment timestamp + URL), `exact_match`, `segment_range`, `validation_status`, frozen transcript `acquisition` provenance, and per-hit corpus/index revisions; responses add a `retrieval` provenance record.
- Hybrid fusion `rrf-exact-v1` ranks exact identifiers/quoted phrases as their own list so dense similarity cannot bury a part number; older indexes keep `rrf-v1`.
- Boundary-aware chunker (`timestamp-boundary-v1`) and index schema 2. New indexes get new revisions; pinned older indexes still search identically.
- Vector search moved behind an internal replaceable seam (`vector_search.py`, `exact-scan-v1`). Hybrid search no longer loads all chunk text to rank.
- Corpus revisions that carry `manifest_sha256` have it echoed into index metadata and results.

### Benchmark
- `bench/retrieval_suite.py`: synthetic 12-stratum retrieval benchmark (Recall@k, success@k, MRR, temporal-hit@k, temporal error, window IoU, latency) with thresholds; metric functions in `bench/retrieval_metrics.py`.

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
