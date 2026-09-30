# Changelog

## 0.2.1 — unreleased

### Distribution and remote operation (issue #7)
- PyPI-ready packaging: version single-sourced from `youtube_mcp_v2.__version__`, SPDX license metadata, classifiers, project URLs, `[all]` and `[release]` extras, and `MANIFEST.in`. `python -m build` + `twine check --strict` pass; `uvx --from <wheel> youtube-mcp-v2` is verified.
- CLI: `--version`, `serve --transport stdio|streamable-http`, an extended `doctor` (package versions, binary versions, disk space, configured proxy/cookie/STT/embedding backends), `config check`, and `cache status`. All output is secret-redacted.
- Streamable HTTP transport with bearer-token auth (plaintext or `sha256:` token files), per-principal token-bucket rate limiting, a request-body bound, DNS-rebinding host allowlist, and `/healthz`. It serves both 2026-07-28 stateless clients and handshake-era (2025-03-26 … 2025-11-25) clients.
- Remote mode withholds server-local paths from tool results, delivers artifacts only via bounded MCP resources (with a new `resource_sha256` field), and does not register `media.materialize`.
- Every tool response and uncaught tool error now passes through a configured-secret redactor, in local mode too. Resource read failures surface as explicit MCP resource errors.
- Added a `Dockerfile` (non-root, ffmpeg included, `/data` volume), MCP Registry `server.json`, `INSTALL.md` (Linux/macOS/Windows, clients, Docker, remote), `RELEASING.md` (release checklist and migration notes), `scripts/smoke_install.py`, and a staged release workflow at `ci/release.yml`.

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
