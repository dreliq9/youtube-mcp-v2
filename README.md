# youtube-mcp-v2

A YouTube MCP server with 14 tools across two tiers. Designed around a few opinions: every tool returns the same envelope, scrapes and external binaries run in subprocesses with hard timeouts, and the SQLite cache is INSERT-only so historical fetches stay queryable.

## Tiers

| Tier | Prefix | Auth | Cost |
|---|---|---|---|
| 1 | `inspect.*`, `transcript.*`, `frame.*`, `scrape.*`, `skeleton.*` | none | free |
| 2 | `api.*` | `YOUTUBE_API_KEY` env (Data API v3) | YouTube quota |

The calling LLM picks tier explicitly. One documented exception: `skeleton.build(target='channel')` upgrades from tier-1 page-scrape (~50 recent uploads) to tier-2 enumeration when an API key is present. The envelope's `source` field reports which tier ran.

## Tools (14)

**Pre-flight**
- `inspect.video(url_or_id)` — id, title, duration, channel, default language, available caption langs, age-gate flag, embed-allowed flag, livestream flag

**Skeleton — frozen reference for multi-step research**
- `skeleton.build(target='channel'|'topic', value, limit=50)` — returns a new handle each call
- `skeleton.list(handle)` — videos in the skeleton
- `skeleton.get(handle)` — full skeleton record
- `skeleton.index(target=None)` — list all skeletons on disk
- `skeleton.expire(handle)` — mark stale (file is preserved; only an `expired_at` field is set)

**Transcripts**
- `transcript.get(url_or_id, mode='text'|'timed'|'chunked', lang='en', ...)` — runs a word-rate plausibility check (0.3–6.0 wps over the video duration); failures appear in `warnings` with `validated: false`, but the data still returns

**Frames**
- `frame.get(url_or_id, mode='single'|'sheet', ...)` — yt-dlp downloads the video to a temp dir, ffmpeg extracts, temp file is deleted on success

**Audio**
- `audio.get(url_or_id, fmt='wav', sample_rate=22050, start_s=None, end_s=None)` — yt-dlp downloads best audio to a temp dir, ffmpeg converts it to cached mono audio for transcription tools such as Basic Pitch and Song Maker audio-to-tab

**Search**
- `scrape.search(query, n=10)` — HTML scrape of YouTube's search results page; runs in a subprocess with a 20s timeout

**Tier-2 (Data API v3)**
- `api.search`, `api.channel_stats`, `api.trending`, `api.video_categories`

## Response envelope

Every tool returns this shape, on success and on failure:

```json
{
  "data": <payload or null>,
  "fetched_at": "2026-04-30T14:22:01Z",
  "source": "scrape" | "api" | "cache",
  "cache_age_s": 0,
  "validated": true,
  "warnings": [],
  "error": null
}
```

On error, `data` is `null` and `error` is `{code, message, recoverable}`. Examples: `bad_url`, `auth_required`, `quota_exceeded`, `subprocess_failure`, `watch_page_fetch_failed`. Subprocess timeouts and adapter exceptions are caught at the tool boundary and translated to envelope errors.

## Subprocess isolation — what's isolated, what isn't

`isolation.py` runs callables in a `ProcessPoolExecutor` with a hard timeout. It's used for paths that could segfault, hang, or stall on layout changes:
- `scrape.search` (HTML parse)
- `skeleton.build(target='channel')` page scrape
- `skeleton.build(target='topic')` page scrape

Pure-Python paths (httpx requests, `youtube-transcript-api`, the Data API client) run in-process and rely on each library's own timeouts. Frame extraction shells out to yt-dlp + ffmpeg via `subprocess`, with timeouts on each call.

## Cache

SQLite at `~/.cache/youtube-mcp/v2.sqlite` (override with `XDG_CACHE_HOME`). Stores transcripts, video metadata, and search results. Schema is INSERT-only with `AUTOINCREMENT` primary keys plus `(target, fetched_at DESC)` indexes — re-fetching adds a new row, lookup hits the most recent. No `UPDATE` or `REPLACE` paths exist.

Skeletons live as JSON files under `~/.cache/youtube-mcp/skeletons/`. Each `skeleton.build` produces a new timestamped handle; old handles stay on disk. `skeleton.expire` mutates the file to add an `expired_at` field but does not delete it.

## Install

Requires Python 3.10+ and `ffmpeg` in `PATH` (only needed for `frame.get` and `audio.get`).

```bash
pip install git+https://github.com/dreliq9/youtube-mcp-v2.git
```

Optional extras:
```bash
pip install "youtube-mcp-v2[api,frame] @ git+https://github.com/dreliq9/youtube-mcp-v2.git"
```

- `api` — pulls in `google-api-python-client` for the four `api.*` tools
- `frame` — pulls in `yt-dlp` for `frame.get`

## Configure your MCP client

For Claude Code or any stdio-MCP client:

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

Without `YOUTUBE_API_KEY`, the four `api.*` tools still register but return `error.code = "auth_required"` when called.

## Design

`SPEC_r3.md` is the canonical spec (rationale, tier model, envelope schema, discipline notes, future direction). `SPEC.md` and `SPEC_r2.md` are kept as design history.

## Status

v0.2. Tier 3 (`oauth.*`, own-channel automation) is a reserved namespace — not implemented. v0.3+ is sketched in `SPEC_r3.md` §12.

## License

MIT. See `LICENSE`.
