# youtube-mcp-v2

A disciplined YouTube MCP for evidence-grade video research. It exposes 15 tools across two tiers and is built around frozen research sets, validated transcripts, recoverable upstream failures, media extraction, and append-only local history.

The long-term target is not "the most YouTube API wrappers." It is a reproducible multimodal research instrument: acquire video evidence reliably, preserve provenance, freeze corpora, and let an AI retrieve the exact spoken or visual moment that supports a claim.

## Design principles

- **Evidence before convenience** — tools report provenance and validation state rather than silently hiding fallbacks.
- **Frozen research sets** — skeleton handles make multi-step research reproducible and diffable.
- **Failure containment** — fragile scrapes and external binaries run behind hard timeouts and tool-boundary error envelopes.
- **Never overwrite history** — transcript and metadata cache writes append new revisions; skeleton creation refuses to overwrite an existing handle.
- **Small public tool surface** — compound research capabilities should not require the calling model to orchestrate dozens of low-level wrappers.

## Tiers

| Tier | Prefix | Auth | Cost |
|---|---|---|---|
| 1 | `inspect.*`, `transcript.*`, `frame.*`, `audio.*`, `scrape.*`, `skeleton.*` | none | free/local compute |
| 2 | `api.*` | `YOUTUBE_API_KEY` env (Data API v3) | YouTube quota |

The calling LLM picks tier explicitly. One documented exception: `skeleton.build(target='channel')` upgrades from tier-1 page scraping to tier-2 enumeration when an API key is present. The response envelope reports which source ran.

## Tools (15)

**Pre-flight**
- `inspect.video(url_or_id)` — id, title, duration, channel, caption languages, age-gate flag, embed flag, livestream flag

**Skeletons — frozen reference objects for multi-step research**
- `skeleton.build(target='channel'|'topic', value, limit=50)` — creates a new immutable research snapshot
- `skeleton.list(handle)` — videos in the snapshot
- `skeleton.get(handle)` — full snapshot + provenance
- `skeleton.diff(base_handle, head_handle)` — deterministic add/remove/change comparison between two frozen revisions of the same scope
- `skeleton.index(target=None)` — discover snapshots on disk
- `skeleton.expire(handle)` — mark stale without deleting captured membership

`skeleton.diff` compares only the two captured JSON snapshots. It never consults current cache enrichment, so the result remains reproducible later. It also reports when the acquisition source changed between revisions.

**Transcripts**
- `transcript.get(url_or_id, mode='text'|'timed'|'chunked', lang='en', ...)`
- Runs timestamp, language, truncation, and word-rate checks.
- v0.2.1 preserves both requested and actual caption language plus generated-caption status when known.
- If the word-rate gate cannot run because duration cannot be resolved, the transcript is returned but is **not** reported as fully validated.

**Frames**
- `frame.get(url_or_id, mode='single'|'sheet', ...)` — yt-dlp + ffmpeg frame or contact-sheet extraction with caching

**Audio**
- `audio.get(url_or_id, fmt='wav', sample_rate=22050, start_s=None, end_s=None)` — cached mono audio extraction for downstream local transcription or audio analysis

**Search**
- `scrape.search(query, n=10)` — no-key YouTube search behind subprocess isolation

**Tier-2 — Data API v3**
- `api.search` — 1 unit in YouTube's Search Queries bucket; default allocation is currently 100 calls/day
- `api.channel_stats`
- `api.trending`
- `api.video_categories`

## Response envelope

Every tool returns the same top-level contract on success and failure:

```json
{
  "data": "<tool-specific payload or null>",
  "fetched_at": "2026-08-06T12:00:00Z",
  "source": "scrape | api | cache",
  "cache_age_s": 0,
  "validated": true,
  "warnings": [],
  "error": null
}
```

On error, `data` is `null` and `error` is `{code, message, recoverable}`. Upstream failures are translated at the tool boundary rather than crashing the MCP process.

The next major schema evolution is an **Evidence Envelope** that makes acquisition method, source revision, content hashes, transcript type, and timestamped evidence first-class. See `EVIDENCE_MODEL.md`.

## Isolation

`isolation.py` runs fragile Python scrape paths in a `ProcessPoolExecutor` with hard timeouts. yt-dlp and ffmpeg use bounded subprocess calls. Pure-Python HTTP/API paths rely on explicit library timeouts and tool-boundary exception handling.

Current isolated paths include:
- `scrape.search`
- `skeleton.build(target='channel')` scrape path
- `skeleton.build(target='topic')`
- yt-dlp / ffmpeg media operations

## Cache and reproducibility

SQLite lives at `~/.cache/youtube-mcp/v2.sqlite`. Transcript and video metadata writes are append-only; lookups select the newest matching revision. v0.2.1 performs additive schema migration for transcript provenance and does not rewrite historical rows.

Skeletons live under `~/.cache/youtube-mcp/skeletons/`. New handles include microseconds and files are created exclusively, so even a handle collision cannot overwrite history. Legacy second-resolution handles remain readable. Old handles remain queryable and can be compared with `skeleton.diff`.

## Install

Requires Python 3.10+. `ffmpeg` must be available in `PATH` for `frame.get` and `audio.get`.

```bash
pip install git+https://github.com/dreliq9/youtube-mcp-v2.git
```

Optional extras:

```bash
pip install "youtube-mcp-v2[api,media] @ git+https://github.com/dreliq9/youtube-mcp-v2.git"
```

- `api` — `google-api-python-client` for `api.*`
- `media` — `yt-dlp` for both `frame.get` and `audio.get`
- `frame` and `audio` remain compatibility aliases for the same yt-dlp dependency

## MCP compatibility

v0.2.1 targets the stable **MCP Python SDK v2** line and the 2026-07-28 protocol generation while retaining compatibility with older clients through the SDK's protocol negotiation.

For a stdio MCP client:

```json
{
  "mcpServers": {
    "youtube": {
      "command": "youtube-mcp-v2",
      "env": {
        "YOUTUBE_API_KEY": "<optional — enables tier-2 tools>"
      }
    }
  }
}
```

Without `YOUTUBE_API_KEY`, `api.*` tools still register and return `error.code = "auth_required"` if invoked.

## Quality gates

GitHub Actions is configured to run:
- non-network unit tests on Python 3.10, 3.11, 3.12, and 3.13
- import/package smoke tests on Linux, macOS, and Windows
- bytecode compilation before the unit suite

Live YouTube tests remain explicitly marked `network`/`slow` so CI does not confuse upstream throttling with a deterministic code regression.

## Product roadmap

The next sequence is intentionally acquisition-first:

1. **v0.3 — Reliable acquisition:** provider abstraction, transcript fallback waterfall, proxy/cookie support, acquisition provenance, playlist/corpus ingestion.
2. **v0.4 — Evidence search:** local transcription fallback, embeddings, semantic search over frozen corpora, timestamped supporting excerpts.
3. **v0.5 — Multimodal temporal search:** scene detection, OCR, visual embeddings, and cross-modal spoken + on-screen evidence retrieval.
4. **v0.6 — Distribution:** PyPI/uvx, Docker, official MCP Registry, and Streamable HTTP/remote-safe artifact delivery.
5. **v0.7 — Research workflows:** a small number of strong compound retrieval tools rather than a 50-tool orchestration burden.

See `ROADMAP.md` for acceptance criteria and `BENCHMARK.md` for the proposed public YouTube-MCP benchmark.

## Design history

`SPEC_r3.md` remains the canonical v0.2 design rationale. `SPEC.md` and `SPEC_r2.md` are preserved as design history rather than overwritten.

## Status

**v0.2.1 stabilization:** MCP SDK v2 migration, packaging/CI hardening, transcript provenance, strict never-overwrite skeleton creation, and reproducible skeleton revision diffing.

Tier 3 (`oauth.*`, own-channel automation) remains reserved and is not a near-term product priority; creator automation is a different product axis from evidence-grade research.

## License

MIT. See `LICENSE`.
