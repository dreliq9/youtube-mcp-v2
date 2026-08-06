# Roadmap — from YouTube MCP to evidence-grade video intelligence

## North star

youtube-mcp-v2 should be the most trustworthy MCP for **researching what a video corpus actually contains**.

The product should not optimize for raw tool count. It should optimize for agent task success on questions such as:

> Across this channel, find every place the creator discusses sodium-ion batteries, distinguish spoken claims from values shown on screen, and give me auditable timestamps.

That requires five capabilities working together:

1. reliable acquisition,
2. frozen/reproducible corpora,
3. semantic + temporal retrieval,
4. multimodal evidence,
5. explicit provenance and validation.

## Architectural invariants

These remain true across every milestone:

- **No silent provider fallback.** A fallback may be automatic, but its acquisition path is recorded.
- **No silent validation downgrade.** If a required integrity check cannot run, `validated` cannot imply that it did.
- **No destructive cache updates.** New observations append revisions.
- **Frozen corpus revisions are immutable.** Enrichment lives alongside a revision; it does not mutate what was originally captured.
- **The public MCP surface stays small.** Internal primitives can grow freely; public tools should map to useful agent jobs.
- **Local-first remains first class.** A hosted deployment is an additional transport, not a reason to sacrifice local media/ML workflows.
- **Evidence is addressable.** Research results should resolve to a video plus time range, source method, and supporting text/frame artifact.

---

## v0.2.1 — Protocol & provenance hardening

Goal: make the existing v0.2 feature set safe to build on.

### Scope

- MCP Python SDK v2 migration.
- Explicit dependency bounds and `python-dotenv` declaration.
- Media extras that correctly cover both frames and audio.
- Cross-version/cross-platform CI.
- Requested-vs-actual transcript language preservation.
- Generated-caption provenance when available.
- No `validated=True` when the duration-dependent word-rate gate could not run.
- Explicit errors for invalid transcript modes/chunk budgets.
- Correct current `search.list` quota documentation.

### Exit criteria

- All deterministic tests green on Python 3.10–3.13.
- Import/package smoke green on Linux, macOS, Windows.
- Existing cache migrates additively.
- Existing MCP clients can launch the stdio server through SDK v2 compatibility negotiation.

---

## v0.3 — Reliable acquisition + corpus foundation

Goal: make upstream failure boring and make research sets first-class.

### 1. Provider abstraction

Introduce internal interfaces rather than embedding fallback policy directly into tools.

Suggested shape:

```python
@dataclass(frozen=True)
class AcquisitionAttempt:
    provider: str
    method: str
    started_at: str
    duration_ms: int
    outcome: Literal["success", "unavailable", "blocked", "timeout", "error"]
    detail: str | None = None

@dataclass(frozen=True)
class TranscriptArtifact:
    video_id: str
    requested_lang: str
    actual_lang: str
    transcript_type: Literal["manual", "generated", "local_stt", "unknown"]
    segments: list[Segment]
    attempts: list[AcquisitionAttempt]
```

Initial transcript waterfall:

1. fresh validated cache,
2. native caption acquisition,
3. alternate yt-dlp/InnerTube caption path,
4. optional cookie-authenticated acquisition,
5. optional local STT fallback (implemented fully in v0.4, interface reserved now).

The public tool returns one result; the provenance tells the caller how it was obtained.

### 2. Network policy

Add configuration rather than hard-coded network behavior:

- HTTP proxy / SOCKS proxy URI.
- yt-dlp proxy propagation.
- optional YouTube cookie file / browser-cookie import path.
- request timeout and retry budget.
- explicit no-proxy/offline mode.
- per-provider circuit breaker after repeated blocking/rate-limit failures.

Secrets and cookies must never be returned in tool results or logs.

### 3. Corpus model

Evolve the internal skeleton primitive without discarding it. Public naming can become `corpus.*`; historical skeleton handles remain readable.

Minimum corpus targets:

- channel,
- playlist,
- topic/search snapshot,
- explicit video-id/url set.

Suggested public surface:

- `corpus.build(target, value, limit, ...)`
- `corpus.get(handle)`
- `corpus.list(handle, cursor=...)`
- `corpus.diff(base_handle, head_handle)`
- `corpus.index(target=None)`

`corpus.diff` should report additions, removals, metadata changes, and content/provenance changes separately.

### 4. Acquisition manifest

Every frozen corpus gets a machine-readable manifest containing:

- schema version,
- corpus handle/revision,
- query/channel/playlist identity,
- capture timestamp,
- video IDs and canonical URLs,
- metadata revision IDs/hashes,
- acquisition providers attempted,
- tool/server version.

### v0.3 exit criteria

- A channel or playlist can be captured and later diffed without reinterpreting the original revision.
- Proxy/cookie configuration is supported without leaking secrets.
- Provider failure produces a structured attempt history rather than a generic opaque exception.
- A blocked primary transcript path can succeed through an alternate acquisition path and the fallback is visible in provenance.
- Network tests include mocked 429, timeout, malformed-page, disabled-caption, and fallback-success cases.

---

## v0.4 — Evidence search

Goal: turn frozen corpora into timestamp-searchable knowledge bases.

### 1. Local transcription fallback

Public capability:

- `transcript.get(..., acquisition="auto"|"captions"|"local_stt")`

Backends should be adapters so Apple Silicon, portable CPU, and future accelerator paths are swappable.

Initial candidates:

- `mlx-whisper` on Apple Silicon,
- `whisper.cpp` portable fallback.

Store model identifier, model revision/hash, decode settings, source-audio hash, and timing resolution in provenance.

### 2. Transcript indexing

Chunk by **semantic/timestamp boundaries**, not arbitrary fixed characters alone.

Index row should preserve:

- corpus handle,
- video ID,
- start/end seconds,
- transcript revision ID,
- chunk text,
- embedding model/revision,
- vector,
- validation state.

Avoid making FAISS file format part of the public contract. The index backend is replaceable.

### 3. Evidence retrieval

Suggested public tool:

```text
corpus.search(handle, query, top_k=10, filters=None)
```

Each hit returns:

- canonical video identity,
- start/end timestamp,
- supporting excerpt,
- similarity/retrieval score,
- transcript acquisition method,
- validation state,
- corpus revision,
- direct timestamp URL.

### 4. Hybrid retrieval

Combine vector retrieval with lexical signals for exact names, part numbers, quoted phrases, and numbers. Pure vector search is not enough for engineering/product research.

### v0.4 exit criteria

- Search a corpus containing at least 100 hours of transcript data without loading it all into model context.
- Retrieval results are reproducible against a frozen corpus revision.
- Benchmark recall@10 and timestamp precision targets are defined in `BENCHMARK.md` and met on the reference set.
- Missing native captions can be indexed through local STT without changing the public search workflow.

---

## v0.5 — Multimodal temporal evidence

Goal: retrieve what was **shown**, not only what was said.

### Pipeline

1. scene/change-point detection,
2. representative keyframe selection,
3. OCR,
4. visual description/embedding,
5. temporal alignment with transcript segments,
6. cross-modal retrieval and evidence fusion.

### Internal primitives

- scene detection,
- keyframe extraction,
- OCR,
- image embedding,
- optional frame description,
- image-to-image similarity.

These do not all need to be public MCP tools.

### Suggested public capability

```text
corpus.search(handle, query, modalities=["speech", "screen"], top_k=10)
```

A result can contain multiple aligned evidence items:

- transcript excerpt,
- OCR text,
- frame/resource URI,
- exact timestamp/time range,
- modality-specific confidence.

### Important target query

> Find the chart where the reviewer shows transient power above rated TGP and return both the spoken interpretation and the chart frame.

### v0.5 exit criteria

- OCR retrieval finds text present only on screen.
- visual/temporal retrieval finds a target scene even when the transcript never names it exactly.
- returned media is consumable through MCP content/resources, not only host-local filesystem paths.
- cross-modal results retain separate provenance for speech and visual evidence.

---

## v0.6 — Distribution and remote operation

Goal: remove installation friction without weakening local-first capabilities.

### Distribution

- publish to PyPI,
- `uvx youtube-mcp-v2` launch path,
- Docker image,
- official MCP Registry metadata,
- signed/versioned release artifacts,
- semantic release notes and migration notes.

### Remote transport

Add Streamable HTTP deployment with:

- stateless modern MCP operation,
- authentication boundary,
- rate limits,
- bounded media/artifact responses,
- resource URIs instead of server-local paths,
- storage quotas and cache health diagnostics.

### Install/doctor UX

Add CLI commands such as:

- `youtube-mcp-v2 doctor`
- `youtube-mcp-v2 config check`
- `youtube-mcp-v2 cache status`

Doctor should verify Python package versions, ffmpeg/yt-dlp availability, writable cache paths, API key presence (without printing it), and configured model backends.

---

## v0.7 — Compound research workflows

Goal: make complex research one good MCP call rather than eight fragile agent-orchestration steps.

Candidate public tools:

- `research.retrieve(question, corpus=..., modalities=...)`
- `research.compare(corpus, entities, dimensions=...)`

These tools should **retrieve and structure evidence**, not become an embedded general-purpose LLM. The calling model remains responsible for synthesis and judgment.

A compound result should include the exact evidence set needed to support or challenge the model's answer.

---

## Target public MCP surface

The internal package can contain many adapters and primitives. The mature public surface should remain approximately 8–15 high-value tools:

- discovery/search,
- video inspect,
- transcript retrieval,
- media/evidence retrieval,
- corpus build/get/list/diff/search,
- health/doctor where appropriate,
- one or two compound research retrieval operations.

Tool-count growth is not a success metric.

---

## Explicit non-goals

- Becoming a full YouTube Studio/channel-management automation suite.
- Reimplementing yt-dlp as a downloader.
- Hiding third-party model/API costs behind opaque behavior.
- Returning generated summaries without the underlying evidence.
- Making one vector database or one ML model a permanent public dependency.

## Definition of "best"

The project should only claim market leadership when the public benchmark demonstrates it. See `BENCHMARK.md`.
