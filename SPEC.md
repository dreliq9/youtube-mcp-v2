# youtube-mcp v0.2 — Spec

A Python MCP server for YouTube research, transcript work, and video review.
Replaces `~/youtube-mcp/` v0.1 (4 tools, no validation, no cache, no isolation).

## 1. Goal

Build a YouTube MCP that applies the discipline learned from CAiD (skeleton-first, subprocess isolation, validation gates, revision discipline) and KiPilot (pre-flight gates, explicit tier model, headless safety, structured tool docs) to research/transcript/frame workflows — primarily for the LitRPG video pipeline and general content research.

Out of scope for v0.2: channel automation (use `eat-pray-ai/yutu` for that — different problem).

## 2. Tier model (explicit, not magic)

Every tool declares its tier in its name. The calling LLM picks knowingly; no silent fallbacks across tiers.

| Tier | Prefix | Auth | Cost |
|---|---|---|---|
| 1 | `scrape.*`, `inspect.*`, `transcript.*`, `frame.*` | none (page scrape + youtube-transcript-api + ffmpeg) | free |
| 2 | `api.*` | `YOUTUBE_API_KEY` env (Data API v3, 10K units/day) | quota |
| 3 | `oauth.*` | OAuth (own channel only) | quota |

v0.2 ships tier 1 + tier 2. Tier 3 is reserved namespace, not implemented.

## 3. Tools

```
# Pre-flight (cheap, run before expensive calls — KiPilot ERC-gate lesson)
inspect.video(url_or_id) → { id, title, duration_s, lang_default,
                              available_caption_langs[], has_transcript,
                              age_gated, embed_allowed, livestream, fetched_at }
inspect.channel(handle_or_id) → { id, handle, title, video_count, subscriber_count? }

# Skeleton — frozen reference object all later tools read from (CAiD skeleton lesson)
build.channel_skeleton(channelId, limit=50) → SkeletonHandle
build.topic_skeleton(query, n=20)           → SkeletonHandle
skeleton.list(handle)                        → [{id, title, duration_s, has_transcript, lang, published_at}]
skeleton.get(handle)                         → full snapshot
skeleton.expire(handle)                      → marks stale; does NOT delete (revision discipline)

# Transcripts (validated; refuse junk)
transcript.full(url_or_id, lang='en')                    → { text, lang, word_count, fetched_at, validated, warnings[] }
transcript.timed(url_or_id, lang='en', cursor=None)      → { segments[], next_cursor }       # paginated like jkawamoto/mcp-youtube-transcript
transcript.chunked(url_or_id, tokens=500, overlap=50)    → { chunks: [{i, n, start_s, end_s, text, token_estimate}] }   # token-aware (kirbah lesson)

# Frames — net-new vs the field (LitRPG video review)
frame.at(url_or_id, timestamp_s, format='png')           → { path, timestamp_s }
frame.contact_sheet(url_or_id, n=12, layout='4x3', size='1280x720') → { path }

# Search
scrape.search(query, n=10)                               → [{id, title, channel, duration, views, published, url}]   # no key
api.search(query, max_results=10, order='relevance', published_after=None, channel_id=None) → tier-2
api.channel_stats(channelId)                             → { subscriber_count, view_count, video_count, published_at, country }
api.trending(region='US', category_id=None, n=20)
api.video_categories(region='US')
```

Tool count: 16 (12 tier-1 + 4 tier-2). Past 12, namespace routing keeps the list legible (mixelpixx pattern from KiPilot survey).

### 3.1 Tool docstring template (ElectroMCP lesson)

Every tool docstring carries three labelled lines so the calling LLM picks correctly:

```
USE WHEN: <one sentence on the right job>
DO NOT USE WHEN: <one sentence on a wrong job + which tool to use instead>
OUTPUT SHAPE: <one line summary of return shape>
```

This single discipline cut wrong-tool calls in the ElectroMCP work and is free to apply.

## 4. Response envelope

Every tool returns this envelope. No exceptions.

```python
{
    "data": <tool-specific payload>,
    "fetched_at": "2026-04-30T14:22:01Z",
    "source": "scrape" | "api" | "cache",
    "cache_age_s": 0,                    # 0 = fresh fetch
    "validated": True,
    "warnings": [],                      # non-fatal: lang mismatch, partial data, etc.
}
```

Errors are not exceptions — they return `{"data": None, "error": {"code": ..., "message": ..., "recoverable": bool}, ...}`. Server never crashes a request because of upstream failure.

## 5. Discipline (the part no other YT MCP has)

### 5.1 Subprocess isolation (CAiD lesson — OCCT segfault killed MCP)

Every scrape and every yt-dlp/ffmpeg call runs in a subprocess with a hard timeout (default 30 s, configurable). If the subprocess dies, segfaults, or hangs, the request returns `{error: {code: "subprocess_failure", recoverable: true}}`. The MCP server does not crash.

Implementation: `concurrent.futures.ProcessPoolExecutor` with `kill_on_timeout=True`. One worker per request, capped at 4 concurrent.

### 5.2 Validation gate (CAiD audit-before-render)

Every transcript output passes a sanity check before return:

- Word count plausible vs duration: `0.3 ≤ words / duration_s ≤ 6.0`. Outside band → `validated=False`, warning logged.
- Language tag matches requested lang (or fallback chain documented).
- No truncation markers (`[...]`, empty trailing segments).
- Segment timestamps monotonic.

Failed validation does not block return — but the envelope says `validated=False` and lists the warnings, so the LLM caller knows.

### 5.3 Versioned SQLite cache (CAiD never-overwrite)

Cache key: `(videoId, captionTrackId, lang, fetched_at_day)`. New fetches do not overwrite old rows — they insert a new row with the new `fetched_at`. `transcript.full(id)` returns the most recent cached row by default; pass `freshness='live'` to force re-fetch. Pass `as_of='2026-03-01'` to query historical row.

Storage: `~/.cache/youtube-mcp/v2.sqlite` — single file, no external services (no MongoDB dependency unlike kirbah). Cache TTL: transcripts 30 days, video meta 7 days, search 1 hour.

### 5.4 Headless safety (KiPilot Freerouting GUI lesson)

When falling back to a browser (page scrape only — never required for transcript-api), use Lightpanda (already in stack) with explicit timeout. No Selenium, no Chromium. No GUI flag possible.

### 5.5 Skeleton freezing (CAiD top-down lesson)

Once a `SkeletonHandle` is built, it is read-only. Re-running `build.channel_skeleton` creates a new handle with a new id. Old handles remain queryable for diff/comparison — never overwritten.

Skeleton storage: `~/.cache/youtube-mcp/skeletons/<handle>.json`. Handle format: `chan-<channelId>-<YYYYMMDD-HHMMSS>` or `topic-<slug>-<YYYYMMDD-HHMMSS>`.

## 6. Architecture

```
youtube_mcp_v2/
├── server.py            # FastMCP entrypoint, tool registry
├── envelope.py          # response envelope, error types
├── isolation.py         # subprocess pool, timeouts
├── validate.py          # transcript validation gates
├── cache.py             # SQLite layer, versioned writes
├── skeleton.py          # build/store/load skeleton handles
├── tools/
│   ├── inspect.py
│   ├── transcript.py    # full / timed / chunked
│   ├── frame.py         # at / contact_sheet (ffmpeg via subprocess)
│   ├── scrape.py        # search via page scrape
│   ├── api.py           # tier-2 Data API v3
│   └── skeleton_tools.py
├── adapters/
│   ├── transcript_api.py     # wraps youtube-transcript-api
│   ├── data_api.py           # Data API v3 client
│   ├── ffmpeg.py             # frame extraction
│   └── ytdlp.py              # last-resort fallback only
└── tests/
```

Dependencies (lean, all pinned):
- `mcp>=1.9` — FastMCP
- `youtube-transcript-api>=1.1` — primary transcript source (already in v0.1)
- `httpx>=0.27` — page scrape, Data API
- `beautifulsoup4>=4.13` — page parse
- `yt-dlp>=2026.1` — fallback only, subprocess
- `google-api-python-client>=2.0` — tier-2 only, optional install group `[api]`
- ffmpeg (system, not pip) — frame extraction

## 7. References borrowed (cited inline in code)

| Borrow | From | Where |
|---|---|---|
| Cursor pagination on long transcripts | jkawamoto/mcp-youtube-transcript | `transcript.timed`, `transcript.full` |
| Token-lean response shape (no eTags, no thumbnails dict bloat) | kirbah/mcp-youtube | All tool outputs |
| `getTranscripts(format='full_text'\|'key_segments')` idea | kirbah/mcp-youtube | future flag on `transcript.full` |
| `findConsistentOutlierChannels` niche analysis | kirbah/mcp-youtube | future tier-2 tool, post-v0.2 |
| `.mcpb` Claude bundle packaging | jkawamoto/mcp-youtube-transcript | release artifact, post-v0.2 |

Skeleton, frame.contact_sheet, subprocess isolation, validation gate, versioned cache: net-new for the YT MCP space.

## 8. Out of scope (v0.2)

- Channel automation (uploads, comment management, playlist edits) — use `yutu`
- Music search/play (`mcp-youtube-music`) — different domain
- Video downloads as a primary feature — yt-dlp is fallback only, not user-facing
- OAuth tier-3 — namespace reserved, not implemented
- MongoDB / external DB cache — SQLite is enough
- LLM-side summarization tools — that's the calling agent's job

## 9. Success criteria

- Drop-in replacement for current `youtube` MCP entry in `~/.claude.json` — same 4 verbs work (`youtube_search`, `get_transcript`, `get_timed_transcript`, `get_video_info`) via aliases that map to new namespaced tools
- A YouTube layout change does not crash the server (subprocess isolation test: kill a worker mid-fetch, server returns clean error)
- A 4-hour video transcript fits a 32K context via `transcript.chunked` with no manual splitting
- `frame.contact_sheet(id, n=12)` produces a single PNG suitable for review in <10 s for a 30-min video
- Validation gate catches a known broken-transcript case (e.g. auto-generated with 50 words for a 60-min video)
- Test coverage ≥ 80 % (mock the network layer)

## 10. Open questions

1. Cache location: `~/.cache/youtube-mcp/v2.sqlite` or `~/Desktop/mcps/youtube-mcp-v2/cache/`? Latter is tidier given your project layout, former is XDG-canonical.
2. Frame extraction: stream-only (httpx range requests + ffmpeg) or download-then-extract via yt-dlp? Stream-only is faster but fails on age-gated; yt-dlp is more reliable but slower.
3. Should the v0.2 server expose v0.1 tool names as aliases for one cycle, or do a hard cut and update `~/.claude.json` in one step?
4. Channel skeleton: tier-1 (page scrape, ~50 most recent uploads) or tier-2 (Data API, full enumeration)? Tier-1 keeps no-key flow but limits to recent.
5. Tool count is 16 — comfortable, but do we want to merge `transcript.full / timed / chunked` into one tool with a `mode` param to drop to 14?
