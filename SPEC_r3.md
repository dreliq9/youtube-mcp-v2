# youtube-mcp v0.2 — Spec (r3)

Revision of `SPEC_r2.md` adding a documented future direction: local-ML video intelligence (not in v0.2 scope, but architecturally load-bearing — it justifies the Python language choice and shapes how skeletons evolve).

**Diff from r2:**
- Added §12 "Future direction: local-ML video intelligence" — out of scope for v0.2 but on the roadmap. The skeleton primitive will evolve into a vector index in v0.3+; the v0.2 SQLite schema must not preclude that.
- Language choice (Python) now explicitly justified by the local-ML axis — Whisper, CLIP, sentence-transformers, OCR all live in Python with mature libs. ML via subprocess from TS/Go is the pattern we are deliberately avoiding.

**Diff from r1 (still applies):**
- Cache path → `~/.cache/youtube-mcp/v2.sqlite` (XDG-canonical)
- Frame extraction → yt-dlp download-then-extract (reliable over fast)
- Migration → hard cut, update `~/.claude.json`, no aliases
- Channel skeleton → tier-1 default (~50 recent uploads), auto-upgrade to tier-2 when `YOUTUBE_API_KEY` is set
- Tool count 16 → 13 via mode-merging (`transcript.get`, `frame.get`, `skeleton.build`)

## 1. Goal

Build a YouTube MCP that applies the discipline learned from CAiD (skeleton-first, subprocess isolation, validation gates, revision discipline) and KiPilot (pre-flight gates, explicit tier model, headless safety, structured tool docs) to research/transcript/frame workflows — primarily for the LitRPG video pipeline and general content research.

Long term (see §12), this MCP grows into a video-intelligence platform: YouTube is one data source; local ML (Whisper, CLIP, embeddings, OCR, scene detection) does the analysis. v0.2 lays the foundation; v0.3+ adds the ML namespace.

Out of scope for v0.2: channel automation (use `eat-pray-ai/yutu` for that — different problem). Local ML tooling (see §12).

## 2. Tier model

Every tool declares its tier in its name. The calling LLM picks knowingly; no silent fallbacks across tiers.

| Tier | Prefix | Auth | Cost |
|---|---|---|---|
| 1 | `inspect.*`, `transcript.*`, `frame.*`, `scrape.*`, `skeleton.*` | none (page scrape + youtube-transcript-api + yt-dlp) | free |
| 2 | `api.*` | `YOUTUBE_API_KEY` env (Data API v3, 10K units/day) | quota |
| 3 | `oauth.*` | OAuth (own channel only) | quota |

v0.2 ships tier 1 + tier 2. Tier 3 is reserved namespace, not implemented.

**Tier auto-upgrade exception:** `skeleton.build(target='channel')` defaults to tier-1 (page scrape, last ~50 uploads). When `YOUTUBE_API_KEY` is set in the environment, it transparently upgrades to tier-2 for full enumeration. The envelope's `source` field reports which tier ran. This is the only tool with cross-tier behaviour and it is documented prominently.

## 3. Tools (13)

### Pre-flight

```python
inspect.video(url_or_id: str)
    → { id, title, duration_s, lang_default, available_caption_langs[],
        has_transcript, age_gated, embed_allowed, livestream }

inspect.channel(handle_or_id: str)
    → { id, handle, title, video_count, subscriber_count? }
```

Cheap. Run before any expensive call. (KiPilot ERC-gate lesson.)

### Skeleton — frozen reference for multi-step research

```python
skeleton.build(
    target: Literal['channel', 'topic'],
    value: str,                  # channelId / handle for 'channel'; query string for 'topic'
    limit: int = 50,
) → { handle: str, source: 'scrape'|'api', count: int }

skeleton.list(handle: str)
    → [{ id, title, duration_s, has_transcript, lang, published_at }]

skeleton.get(handle: str)
    → { handle, target, value, built_at, source, videos: [...] }

skeleton.expire(handle: str)
    → { handle, expired_at }   # marks stale; does NOT delete (revision discipline)
```

Once built, a handle is read-only. Re-running `skeleton.build` produces a new handle with a new id. Old handles remain queryable for diff/comparison — never overwritten. (CAiD never-overwrite.)

### Transcripts

```python
transcript.get(
    url_or_id: str,
    mode: Literal['text', 'timed', 'chunked'] = 'text',
    lang: str = 'en',
    cursor: str | None = None,        # mode='timed' only — pagination
    chunk_tokens: int = 500,          # mode='chunked' only
    chunk_overlap: int = 50,          # mode='chunked' only
)
```

Output by mode:

| Mode | Returns |
|---|---|
| `text` | `{ text, lang, word_count }` |
| `timed` | `{ segments: [{start_s, end_s, text}], next_cursor }` |
| `chunked` | `{ chunks: [{i, n, start_s, end_s, text, token_estimate}] }` |

All three pass through the validation gate (§5.2) and return the standard envelope (§4).

### Frames

```python
frame.get(
    url_or_id: str,
    mode: Literal['single', 'sheet'] = 'single',
    timestamp_s: float | None = None,    # mode='single' only — required
    n: int = 12,                          # mode='sheet' only — frame count
    layout: str = '4x3',                  # mode='sheet' only
    size: str = '1280x720',
    format: Literal['png', 'jpg'] = 'png',
)
```

Output by mode:

| Mode | Returns |
|---|---|
| `single` | `{ path, timestamp_s }` |
| `sheet` | `{ path, layout, frame_timestamps: [...] }` |

Implementation: yt-dlp downloads to temp dir → ffmpeg extracts → temp file deleted on success. Reliable across age-gated content; ~5–10× slower than streaming. (Adam's call: reliability over speed.)

### Search

```python
scrape.search(query: str, n: int = 10)
    → [{ id, title, channel, duration, views, published, url }]
```

No key. HTML scrape of search results page. Fragile against YouTube layout changes — but isolated in a subprocess (§5.1) so failure is recoverable, not fatal.

### Tier-2 (Data API v3, requires `YOUTUBE_API_KEY`)

```python
api.search(query, max_results=10, order='relevance', published_after=None, channel_id=None)
api.channel_stats(channelId)
api.trending(region='US', category_id=None, n=20)
api.video_categories(region='US')
```

### 3.1 Tool docstring template (ElectroMCP lesson)

Every tool docstring carries three labelled lines so the calling LLM picks correctly:

```
USE WHEN: <one sentence on the right job>
DO NOT USE WHEN: <one sentence on a wrong job + which tool to use instead>
OUTPUT SHAPE: <one line summary of return shape>
```

For mode-parameterised tools, the docstring lists USE WHEN / DO NOT USE WHEN per mode. Example for `transcript.get`:

```
USE WHEN mode='text': consumer just needs the words, no timing.
USE WHEN mode='timed': consumer needs to jump to specific moments or build a clip list.
USE WHEN mode='chunked': transcript is too long for a single LLM call (>~5K tokens).
DO NOT USE: when you don't yet have a video id — call inspect.video or scrape.search first.
OUTPUT SHAPE: see mode table.
```

## 4. Response envelope

Every tool returns this envelope. No exceptions.

```python
{
    "data": <tool-specific payload or None>,
    "fetched_at": "2026-04-30T14:22:01Z",
    "source": "scrape" | "api" | "cache",
    "cache_age_s": 0,                    # 0 = fresh fetch
    "validated": True,
    "warnings": [],                      # non-fatal: lang mismatch, partial data, etc.
    "error": None,                       # or { code, message, recoverable: bool }
}
```

Server never crashes a request because of upstream failure. Errors return cleanly; `data` is None and `error` is populated.

## 5. Discipline

### 5.1 Subprocess isolation (CAiD lesson — OCCT segfault killed MCP)

Every scrape and every yt-dlp/ffmpeg call runs in a subprocess with a hard timeout (default 30 s, configurable per tool). If the subprocess dies, segfaults, or hangs, the request returns `{error: {code: "subprocess_failure", recoverable: true}}`. The MCP server does not crash.

Implementation: `concurrent.futures.ProcessPoolExecutor` with `kill_on_timeout=True`. One worker per request, capped at 4 concurrent.

### 5.2 Validation gate (CAiD audit-before-render)

Every transcript output passes a sanity check before return:

- Word count plausible vs duration: `0.3 ≤ words / duration_s ≤ 6.0`. Outside band → `validated=False`, warning logged.
- Language tag matches requested lang (or fallback chain documented in warnings).
- No truncation markers (`[...]`, empty trailing segments).
- Segment timestamps monotonic.

Failed validation does not block return — but the envelope says `validated=False` and lists the warnings, so the LLM caller knows.

### 5.3 Versioned SQLite cache (CAiD never-overwrite)

Cache key: `(videoId, captionTrackId, lang, fetched_at_day)`. New fetches insert a new row — never overwrite. `transcript.get(id)` returns the most recent cached row by default; pass `freshness='live'` to force re-fetch. Pass `as_of='2026-03-01'` to query a historical row.

Storage: `~/.cache/youtube-mcp/v2.sqlite` (XDG-canonical). Single file, no external services. Cache TTL: transcripts 30 days, video meta 7 days, search 1 hour.

### 5.4 Headless safety (KiPilot Freerouting GUI lesson)

When falling back to a browser (page scrape only — never required for transcript-api), use Lightpanda (already in stack) with explicit timeout. No Selenium, no Chromium. No GUI flag possible.

### 5.5 Skeleton freezing (CAiD top-down lesson)

Once built, a `SkeletonHandle` is read-only. Re-running `skeleton.build` creates a new handle. Old handles remain queryable — never overwritten.

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
│   ├── inspect.py       # inspect.video, inspect.channel
│   ├── skeleton_tools.py # skeleton.build/list/get/expire
│   ├── transcript.py    # transcript.get (3 modes)
│   ├── frame.py         # frame.get (2 modes)
│   ├── scrape.py        # scrape.search
│   └── api.py           # tier-2 Data API tools
├── adapters/
│   ├── transcript_api.py     # wraps youtube-transcript-api
│   ├── data_api.py           # Data API v3 client
│   ├── ffmpeg.py             # frame extraction
│   └── ytdlp.py              # video download for frames
└── tests/
```

Dependencies (lean, all pinned):
- `mcp>=1.9` — FastMCP
- `youtube-transcript-api>=1.1` — primary transcript source (already in v0.1)
- `httpx>=0.27` — page scrape, Data API
- `beautifulsoup4>=4.13` — page parse
- `yt-dlp>=2026.1` — frame extraction (subprocess) and scrape fallback
- `google-api-python-client>=2.0` — tier-2 only, optional install group `[api]`
- ffmpeg (system, not pip) — frame extraction

## 7. References borrowed (cited inline in code)

| Borrow | From | Where |
|---|---|---|
| Cursor pagination on long transcripts | jkawamoto/mcp-youtube-transcript | `transcript.get(mode='timed')` |
| Token-lean response shape (no eTags, no thumbnails dict bloat) | kirbah/mcp-youtube | All tool outputs |
| `format='full_text'\|'key_segments'` idea | kirbah/mcp-youtube | future flag on `transcript.get`, post-v0.2 |
| `findConsistentOutlierChannels` niche analysis | kirbah/mcp-youtube | future tier-2 tool, post-v0.2 |
| `.mcpb` Claude bundle packaging | jkawamoto/mcp-youtube-transcript | release artifact, post-v0.2 |

Skeleton, frame.get(mode='sheet'), subprocess isolation, validation gate, versioned cache: net-new for the YT MCP space.

## 8. Out of scope (v0.2)

- Channel automation (uploads, comment management, playlist edits) — use `yutu`
- Music search/play — different domain
- Video downloads as a primary feature — yt-dlp is internal, not user-facing
- OAuth tier-3 — namespace reserved, not implemented
- MongoDB / external DB cache — SQLite is enough
- LLM-side summarization tools — that's the calling agent's job
- v0.1 backwards-compat aliases — hard cut migration

## 9. Success criteria

- Hard-cut migration: `~/.claude.json` `youtube` entry repointed to new server. Old `~/youtube-mcp/` archived (not deleted).
- A YouTube layout change does not crash the server (subprocess isolation test: kill a worker mid-fetch, server returns clean error).
- A 4-hour video transcript fits a 32K context via `transcript.get(mode='chunked')` with no manual splitting.
- `frame.get(mode='sheet', n=12)` produces a single PNG suitable for review of a 30-min video.
- Validation gate catches a known broken-transcript case (e.g. auto-generated with 50 words for a 60-min video).
- Test coverage ≥ 80 % (mock the network layer).

## 10. Resolved questions (from r1)

1. Cache location: `~/.cache/youtube-mcp/v2.sqlite` — XDG-canonical. ✓
2. Frame extraction: yt-dlp download-then-extract — reliable over fast. ✓
3. Migration: hard cut. Update `~/.claude.json` in one step, archive `~/youtube-mcp/`. ✓
4. Channel skeleton: tier-1 default (~50 recent), auto-upgrade to tier-2 when `YOUTUBE_API_KEY` is set. ✓
5. Tool merging: `transcript.get`, `frame.get`, `skeleton.build` use `mode`/`target` params. Count 16 → 13. ✓

## 11. Next step

Implementation: scaffold at `~/Desktop/mcps/youtube-mcp-v2/` with FastMCP boilerplate, SQLite schema, and a smoke test. **Done as of 2026-04-30** — `inspect.video` and `transcript.get` (3 modes) wired up, 9 unit tests pass, network smokes skip cleanly when upstream throttles. From here, build tool by tool with tests at each step. (Chunked builds with verification — CAiD lesson.)

Build order:
1. ✅ Scaffold + envelope + cache + validate + `inspect.video` + `transcript.get`
2. `scrape.search` (page-scrape with subprocess isolation)
3. `skeleton.build` + `skeleton.list/get/expire` — design the storage schema **with §12 vector-index extension in mind**
4. `frame.get` — yt-dlp subprocess + ffmpeg
5. `api.*` tier-2 (Data API v3 behind `[api]` extra)
6. Hard-cut migration of `~/.claude.json`

## 12. Future direction: local-ML video intelligence (v0.3+)

Out of scope for v0.2. Documented here so the v0.2 architecture does not preclude it.

### Why this exists

Most of the value of YouTube content is locked inside the audio and video, not the metadata. A YouTube MCP that only fetches transcripts is a thin wrapper. A YouTube MCP that runs local ML on the audio/video — Whisper, CLIP, embeddings, OCR, scene detection — turns YouTube into a queryable knowledge surface. This is the SOTA target.

This direction also justifies the language choice: Python's ML library ecosystem (mlx, whisper.cpp, sentence-transformers, open_clip, PaddleOCR, PySceneDetect, librosa, panns-inference) is decisively better than TS/Go for local ML. ML-via-subprocess from another language is the pattern we are deliberately avoiding — it forfeits the in-process predictability that the v0.2 envelope discipline buys us.

### Candidate tools (proposed namespace `ml.*`, all tier-1)

| Tool | Backend | Use |
|---|---|---|
| `ml.transcribe(url_or_id, model, diarize)` | mlx-whisper or whisper.cpp + Core ML | Fallback when YouTube blocks captions; bypass auto-generated garbage; foreign-language transcription. |
| `ml.embed_transcripts(handle)` | sentence-transformers / mlx | Populate vector index for a skeleton; enables semantic search across a channel. |
| `ml.search_skeleton(handle, query, top_k)` | SQLite VSS or FAISS | "Find every moment where this creator discusses X" across a frozen skeleton. |
| `ml.scenes(url_or_id, threshold)` | PySceneDetect | Auto-chapter generation, clip extraction. |
| `ml.ocr_frame(frame_path)` | PaddleOCR / easyocr | Extract on-screen text — slides, code, captions. |
| `ml.visual_search(handle, image_query, top_k)` | CLIP via mlx | "Find moments visually similar to this image" across a skeleton's contact sheets. |
| `ml.classify_audio(url_or_id, kinds)` | YAMNet / panns-inference | Segment by content type (speech vs music vs silence) — enables silence-aware clip cutting. |

### Architectural implications for v0.2

The v0.2 SQLite schema and skeleton storage **must not preclude** the v0.3 vector index. Specific constraints:

- **Skeleton schema** stores `videoId`, `caption_track_id`, `lang` per video — sufficient to later hang vectors off these rows.
- **Cache directory layout** reserves `~/.cache/youtube-mcp/vectors/<handle>.faiss` and `~/.cache/youtube-mcp/models/` for v0.3+. Don't repurpose those paths in v0.2.
- **Subprocess isolation pattern** in `isolation.py` is designed to also handle ML model crashes (CUDA/MPS OOM, segfault) — same pattern, different worker functions.
- **Envelope `source` field** will gain `'ml'` as a valid value in v0.3. The current code path-matches on the existing values; adding one is non-breaking.

### Apple Silicon path

Target Apple Silicon (M-series) primary — Adam's machine. Specific library choices:
- Whisper → `mlx-whisper` (preferred, fastest on M-series) or `whisper.cpp` with Core ML conversion
- CLIP → `mlx-clip` or `open_clip` via MPS backend
- Embeddings → `sentence-transformers` with MPS device
- OCR → PaddleOCR (CPU is fine for batch frame OCR)
- Scene detection → PySceneDetect (pure Python + ffmpeg)
- Audio classification → panns-inference or YAMNet via TFLite

No CUDA, no torch+CUDA install pain. Models live in `~/.cache/youtube-mcp/models/`, downloaded on first use, shared across calls.

### Not committing to scope here

Tool list is illustrative, not contractual. v0.3 will pick a minimal subset (likely `ml.transcribe` + `ml.embed_transcripts` + `ml.search_skeleton` first — they unblock the highest-leverage workflow: turn a channel into a searchable knowledge base). Other tools follow as concrete needs land.
