# youtube-mcp-v2

A disciplined YouTube MCP for **evidence-grade, domain-neutral video research**.

The product goal is not "more YouTube API wrappers" and it is not an engineering-specific workflow. It is a trustworthy interface to **knowledge that happens to be encoded in video**: discover sources, freeze or compose a research corpus, acquire evidence, retrieve the exact spoken or visual moments needed by a reasoning model, and hand selected moments to downstream tools without turning the research MCP into an editor.

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
- **Explicit heavy-work boundaries** — planning/search never quietly becomes source-video download or rendering.
- **Editor-neutral handoff** — source selection and evidence stay independent of Declip, Final Cut Pro, or any other editing system.
- **Failure containment** — fragile upstreams and binaries are bounded and errors are structured.
- **Never overwrite history** — new observations/research revisions are appended rather than destructively rewritten.
- **Small public tool surface** — agent jobs, not internal implementation details, earn tools.

## Tiers

| Tier | Prefix | Auth | Cost |
|---|---|---|---|
| 1 | `inspect.*`, `transcript.*`, `frame.*`, `audio.*`, `media.*`, `scrape.*`, `skeleton.*`, `corpus.*` | none | free/local compute plus explicit media acquisition |
| 2 | `api.*` | `YOUTUBE_API_KEY` | YouTube quota |

One documented exception: `skeleton.build(target='channel')` upgrades to Data API enumeration when an API key is present. Provenance reports which path ran.

## Tools (21)

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
- `skeleton.diff(base_handle, head_handle)` — deterministic add/remove/change comparison between frozen revisions of the same scope.
- `skeleton.index(target=None)`
- `skeleton.expire(handle)`

Historical `skeleton.*` handles are valid corpus revision identifiers.

### Heterogeneous corpus composition

- `corpus.compose(label, videos=None, include_handles=None, base_handle=None, remove=None)`

`corpus.compose` creates a first-class immutable `collection` revision from arbitrary video URLs/IDs and/or multiple already-frozen corpora. It can also derive a revised set from an existing corpus and apply removals without mutating the parent.

Composition performs **no live YouTube acquisition**. New direct videos use cached metadata when available and can be inspected/hydrated later. See `CORPUS_COMPOSITION.md`.

### Transcript evidence

- `corpus.hydrate(handle, lang='en', cursor=None, batch_size=8, max_workers=3, policy='missing'|'fresh')` — acquires one bounded, resumable batch of missing or stale transcripts. It returns compact status/provenance, not transcript bodies; continue with `next_cursor`.

Hydration failures advance the cursor, so one unavailable video cannot stall a corpus. Restarting at cursor `0` later retries only unresolved members because cached successes are skipped. See `CORPUS_HYDRATION.md`.

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

### Editor handoff

- `corpus.clip_plan(handle, query, target_duration_s=60, max_clips=8, ...)`
- `media.materialize(plan_revision, max_height=720, clip_ids=None)`

`corpus.clip_plan` converts ranked frozen evidence into a content-addressed `youtube-mcp.clip-plan/v1` artifact. It selects bounded source ranges, preserves the supporting evidence and source/index revisions, deduplicates heavily overlapping candidates, and writes **no source media**.

`media.materialize` is the explicit heavy/network step. It loads a clip-plan revision, groups ranges by source video, downloads each required source at most once during the call, and emits editor-ready H.264/AAC MP4s plus SHA-256/source provenance in a `youtube-mcp.materialized-clip-plan/v1` manifest.

The resulting asset paths are deliberately editor-neutral. Declip can map each asset directly to `{asset, start:'auto', trim_in:0, trim_out:duration_s}`. FCP-MCP can map the same assets to its montage/rough-cut source list and continue through its normal reviewable FCPXML workflow.

See `EDITOR_HANDOFF.md` for the exact contract and example Declip/FCP-MCP mappings.

## Example cross-domain research workflow

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

## Example remix workflow

```text
mixed = corpus.compose(...)
plan = corpus.clip_plan(
  mixed,
  "how do different people describe a frontier?",
  target_duration_s=90,
  max_clips=8
)
assets = media.materialize(plan.plan_revision, max_height=720)

# hand assets.manifest_path / assets[] to Declip or FCP-MCP
```

The calling model remains responsible for creative intent, ordering decisions, transitions, titles, music, commentary, and publication judgment. youtube-mcp provides the traceable source moments and media handoff.

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

`corpus.hydrate` and `corpus.search` prevent corpus size from becoming model-context size: hydration returns status/provenance, while retrieval returns only leading timestamped evidence chunks.

## Cache and reproducibility

Storage is XDG-aware: `$XDG_CACHE_HOME/youtube-mcp` when a valid absolute XDG cache path is configured, otherwise `~/.cache/youtube-mcp`.

- transcript/video metadata observations are append-only,
- channel/topic/collection snapshots live under `skeletons/`,
- `chan-*`, `topic-*`, and `set-*` handles remain independently queryable,
- corpus search indexes are content-addressed immutable SQLite artifacts,
- visual evidence is frozen by SHA-256 before indexing,
- clip plans live under `edit-plans/`,
- materialized editor clips live under `editor-clips/` and are SHA-256 identified in materialized manifests.

Skeleton handles include microseconds and are created exclusively, so a collision cannot overwrite history. Legacy second-resolution handles remain readable.

## Install

Requires Python 3.10+. Media extraction/materialization also requires `ffmpeg` in `PATH`.

```bash
pip install git+https://github.com/dreliq9/youtube-mcp-v2.git
```

Optional extras:

```bash
pip install "youtube-mcp-v2[api,media,semantic] @ git+https://github.com/dreliq9/youtube-mcp-v2.git"
```

- `api` — YouTube Data API adapter,
- `media` — yt-dlp frame/audio/editor-clip acquisition,
- `semantic` — FastEmbed/ONNX semantic transcript and paired visual embeddings.

## Quality gates

GitHub Actions runs deterministic unit tests on Python 3.10–3.13, package/server import smoke on Linux/macOS/Windows, bytecode compilation, and an optional semantic-extra API smoke without downloading model weights.

Live YouTube tests remain separate so upstream throttling is not mistaken for a deterministic regression.

## Benchmark philosophy

The public benchmark varies across **two axes**:

1. difficult media/acquisition conditions, and
2. deliberate domain/task diversity.

A server should not claim to be "best" because it performs well on one subject class. Benchmark reporting is broken out by history/documentary, humanities, science/technology, current-affairs source material, cooking/craft, products, creative/culture, tutorials, sports/games, long-form interviews, and cross-domain synthesis.

Editor-handoff benchmarks should additionally measure source-range precision, plan reproducibility, materialization success, number of downloads per source, and whether downstream editor adapters can consume the manifest without losing source provenance.

See `BENCHMARK.md`.

## Roadmap

- **v0.3** — reliable acquisition + first-class corpus composition,
- **v0.4** — reproducible transcript evidence retrieval,
- **v0.5** — multimodal temporal evidence plus editor-neutral clip handoff,
- **v0.6** — distribution and remote operation,
- **v0.7** — compact compound research workflows.

See `ROADMAP.md`.

## Design history

`SPEC_r3.md` remains the canonical v0.2 historical design rationale. `SPEC.md` and `SPEC_r2.md` are preserved as design history rather than rewritten to pretend the original scope was different.

## License

MIT. See `LICENSE`.
