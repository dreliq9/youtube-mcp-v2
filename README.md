# youtube-mcp-v2

A disciplined YouTube MCP for **evidence-grade, domain-neutral video research**.

The product goal is not "more YouTube API wrappers" and it is not an engineering-specific workflow. It is a trustworthy interface to **knowledge that happens to be encoded in video**: discover sources, freeze or compose a research corpus, acquire evidence, and retrieve the exact spoken or visual moments needed by a reasoning model.

See `PRODUCT_THESIS.md` for the full product framing.

## What kinds of research?

The architecture is intentionally subject-agnostic. The same primitives should support:

- history, religion, philosophy, and source interpretation,
- science, mathematics, technology, and technical explanations,
- politics/current-affairs primary-source recovery,
- cooking, craft, and procedural comparison,
- product/reviewer consensus and disagreement,
- literature, film, music, games, art, and creative research,
- practical tutorials and repair/how-to evidence,
- sports and performance analysis,
- podcasts/interviews and position tracking,
- cross-domain synthesis around abstract concepts.

Engineering examples are useful stress tests, but engineering has no privileged status in the data model, retrieval stack, or benchmark.

## Design principles

- **Evidence before convenience** — source moments and provenance matter more than generated summaries.
- **Domain-neutral primitives** — core behavior does not assume what the videos are about.
- **Frozen and composable research sets** — multi-step research should be reproducible even when the source universe mixes unrelated domains.
- **Pinned retrieval inputs** — indexes record exact evidence revisions/hashes and model identity.
- **Hybrid exact + semantic retrieval** — paraphrases matter, but so do exact names, dates, quotations, citations, quantities, prices, scores, notation, UI labels, model numbers, and codes.
- **Failure containment** — fragile upstreams and binaries are bounded and errors are structured.
- **Never overwrite history** — new observations/research revisions are appended rather than destructively rewritten.
- **Small public tool surface** — agent jobs, not internal implementation details, earn tools.

## Tiers

| Tier | Prefix | Auth | Cost |
|---|---|---|---|
| 1 | `inspect.*`, `transcript.*`, `frame.*`, `audio.*`, `scrape.*`, `skeleton.*`, `corpus.*` | none | free/local compute |
| 2 | `api.*` | `YOUTUBE_API_KEY` | YouTube quota |

One documented exception: `skeleton.build(target='channel')` upgrades to Data API enumeration when an API key is present. Provenance reports which path ran.

## Tools (18)

### Pre-flight

- `inspect.video(url_or_id)` — canonical identity, metadata, caption/language capability, duration and availability signals.

### Discovery

- `scrape.search(query, n=10)` — no-key discovery.
- `api.search(...)` — structured Data API search when a key is configured.
- `api.channel_stats(...)`
- `api.trending(...)`
- `api.video_categories(...)`

### Frozen source sets

- `skeleton.build(target='channel'|'topic', value, limit=50)` — freeze one channel/topic snapshot.
- `skeleton.list(handle)`
- `skeleton.get(handle)`
- `skeleton.index(target=None)`
- `skeleton.expire(handle)`

Historical `skeleton.*` handles are valid corpus revision identifiers.

### Heterogeneous corpus composition

- `corpus.compose(label, videos=None, include_handles=None, base_handle=None, remove=None)`

`corpus.compose` creates a first-class immutable `collection` revision from arbitrary video URLs/IDs and/or multiple already-frozen corpora. It can also derive a revised set from an existing corpus and apply removals without mutating the parent.

This is the cross-domain source-selection primitive. For example, an AI can intentionally combine a documentary corpus, several interview corpora, a cooking demonstration, a sports analysis, and curated direct videos into one research universe.

Composition performs **no live YouTube acquisition**. New direct videos use cached metadata when available and can be inspected/hydrated later. See `CORPUS_COMPOSITION.md`.

### Transcript evidence

- `transcript.get(url_or_id, mode='text'|'timed'|'chunked', lang='en', ...)`
- `corpus.prepare(handle, ..., semantic='auto')`
- `corpus.search(handle, query, ..., semantic='auto')`

The lexical layer preserves exact strings and the optional semantic layer adds FastEmbed/ONNX dense retrieval over the **same frozen timestamped chunks**. Lexical and semantic ranks are fused rather than replacing one with the other.

Semantic policy:

- `off` — dependency-free lexical retrieval,
- `auto` — use an already-local semantic model/index; never download unexpectedly,
- `required` — explicitly permit/require semantic model initialization unless local-only policy forbids it.

Pinned index revisions preserve exact transcript rows/hashes and retrieval model identity.

### Visual evidence

- `frame.get(url_or_id, mode='single'|'sheet', ...)`
- `corpus.visual_search(handle, query, ..., visual='auto')`

Visual search is for evidence that may never be spoken: maps, slides, diagrams, objects, demonstrations, artwork, products, game/UI states, charts, and other scenes. Frozen visual hits expose protocol-native content-addressed image resources for remote clients.

### Audio

- `audio.get(url_or_id, ...)` — cached audio for downstream speech/music/acoustic analysis.

## Example cross-domain workflow

```text
history = skeleton.build("topic", "productive failure in history")
biology = skeleton.build("topic", "failure adaptation biology")
coaching = skeleton.build("topic", "learning from mistakes coaching")

mixed = corpus.compose(
  label="productive failure across domains",
  include_handles=[history, biology, coaching],
  videos=[one_curated_interview]
)

corpus.prepare(mixed, semantic="auto")
corpus.search(mixed, "when does failure become useful rather than destructive?")
```

The calling model can synthesize the returned evidence; the MCP's job is to make the source set and evidence auditable.

## Response envelope

Tools use a common top-level success/failure shape:

```json
{
  "data": "<tool-specific payload or null>",
  "fetched_at": "...",
  "source": "scrape | api | cache",
  "cache_age_s": 0,
  "validated": true,
  "warnings": [],
  "error": null
}
```

Acquisition/search tools add provenance where known. Errors are structured rather than crashing the MCP process.

## Cache and reproducibility

Storage is XDG-aware: `$XDG_CACHE_HOME/youtube-mcp` when a valid absolute XDG cache path is configured, otherwise `~/.cache/youtube-mcp`.

- transcript/video metadata observations are append-only,
- channel/topic/collection snapshots live under `skeletons/`,
- `chan-*`, `topic-*`, and `set-*` handles remain independently queryable,
- corpus search indexes are content-addressed immutable SQLite artifacts,
- visual evidence is frozen by SHA-256 before indexing.

## Install

Requires Python 3.10+. Media extraction also requires `ffmpeg` in `PATH`.

```bash
pip install git+https://github.com/dreliq9/youtube-mcp-v2.git
```

Optional extras:

```bash
pip install "youtube-mcp-v2[api,media,semantic] @ git+https://github.com/dreliq9/youtube-mcp-v2.git"
```

- `api` — YouTube Data API adapter,
- `media` — yt-dlp frame/audio acquisition,
- `semantic` — FastEmbed/ONNX semantic transcript and paired visual embeddings.

## Quality gates

GitHub Actions runs deterministic unit tests on Python 3.10–3.13, package/server import smoke on Linux/macOS/Windows, bytecode compilation, and an optional semantic-extra API smoke without downloading model weights.

Live YouTube tests remain separate so upstream throttling is not mistaken for a deterministic regression.

## Benchmark philosophy

The public benchmark varies across **two axes**:

1. difficult media/acquisition conditions, and
2. deliberate domain/task diversity.

A server should not claim to be "best" because it performs well on one subject class. Benchmark reporting is broken out by history/documentary, humanities, science/technology, current-affairs source material, cooking/craft, products, creative/culture, tutorials, sports/games, long-form interviews, and cross-domain synthesis.

See `BENCHMARK.md`.

## Roadmap

- **v0.3** — reliable acquisition + first-class corpus composition,
- **v0.4** — reproducible transcript evidence retrieval,
- **v0.5** — multimodal temporal evidence (visual/OCR/scene-aware),
- **v0.6** — distribution and remote operation,
- **v0.7** — compact compound research workflows.

See `ROADMAP.md`.

## Design history

`SPEC_r3.md` remains the canonical v0.2 historical design rationale. `SPEC.md` and `SPEC_r2.md` are preserved as design history rather than rewritten to pretend the original scope was different.

## License

MIT. See `LICENSE`.
