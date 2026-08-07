# Roadmap — evidence-grade access to knowledge encoded in video

## North star

youtube-mcp-v2 should be the most trustworthy MCP for **researching what video sources actually contain**, regardless of subject matter.

Engineering, cooking, history, religion, sports, products, science, politics, games, philosophy, creative work, and practical tutorials are all workloads—not product identities.

The product should optimize for agent task success on questions such as:

> Across this deliberately mixed set of sources, find the strongest evidence for competing explanations of the same idea, distinguish what was said from what was shown, and give me auditable timestamps.

That requires six capabilities working together:

1. reliable acquisition,
2. frozen and composable research corpora,
3. semantic + exact-string + temporal retrieval,
4. multimodal evidence,
5. explicit provenance and validation,
6. domain-diverse benchmark evidence that the system generalizes.

See `PRODUCT_THESIS.md` for the product framing.

## Architectural invariants

- **Domain-neutral primitives.** Core behavior must not assume the subject being researched.
- **No silent provider fallback.** Automatic fallback is allowed only with visible acquisition provenance.
- **No silent validation downgrade.** `validated` must not imply a check ran when it could not.
- **No destructive cache updates.** New observations append revisions.
- **Frozen corpus revisions are immutable.** Enrichment and later composition do not rewrite old membership.
- **Research universes are composable.** Cross-domain work must not require pretending a curated set is one topic search.
- **The public MCP surface stays small.** Internal primitives can grow; public tools map to useful agent jobs.
- **Local-first remains first class.** Hosted transport must not sacrifice local media/ML workflows.
- **Evidence is addressable.** Results resolve to video + time + source method + supporting text/frame artifact.
- **Exact strings remain first-class evidence.** Names, dates, quotations, citations, quantities, prices, identifiers, scores, notation, UI labels, and codes must survive semantic retrieval.

---

## v0.2.1 — Protocol & provenance hardening

Goal: make the original feature set safe to build on.

Scope includes MCP Python SDK v2 migration, dependency/package correctness, cross-platform CI, requested-vs-actual transcript language, generated-caption provenance, stricter validation, explicit caller errors, and current API quota documentation.

Exit criteria: deterministic Python 3.10–3.13 tests; Linux/macOS/Windows package/import smoke; additive cache migration; stdio client compatibility.

---

## v0.3 — Reliable acquisition + corpus foundation

Goal: make upstream failure boring and make research source selection first-class.

### Provider abstraction

Public transcript requests should not need to select a fragile provider. The acquisition layer records provider/method/outcome history and can fall through captions, alternate caption extraction, authenticated paths, and local STT.

### Network policy

Support explicit proxy/cookie/timeout/offline policy without leaking credentials.

### Corpus model

Historical `skeleton.*` handles remain readable, but the broader model is a frozen **corpus revision**.

Minimum corpus origins:

- channel,
- playlist,
- topic/search snapshot,
- explicit video IDs/URLs,
- heterogeneous composition of multiple existing corpora.

### Heterogeneous composition

`corpus.compose` is the domain-neutral source-selection primitive.

It should:

- create a `collection` revision from direct videos,
- union multiple frozen channel/topic/collection handles,
- derive a new revision from an existing collection,
- apply explicit removals without mutating parents,
- dedupe membership deterministically,
- preserve source/composition provenance,
- perform no live network acquisition.

This makes queries such as "compare how four unrelated disciplines treat the same concept" structurally natural.

### Acquisition manifest

Every frozen corpus records schema/revision, origin/composition provenance, capture time, canonical video IDs/URLs, and server/tool version where possible.

### v0.3 exit criteria

- channel/topic/explicit/mixed corpora can be frozen without reinterpretation,
- a heterogeneous corpus can be derived immutably from earlier revisions,
- provider failure returns structured attempt history,
- proxy/cookie paths do not leak secrets,
- mocked fault coverage includes fallback success.

---

## v0.4 — Evidence search

Goal: turn frozen corpora into timestamp-searchable knowledge bases.

### Local transcription fallback

Use adapter backends (e.g. whisper.cpp portable, MLX-optimized optional path) and store model/audio identity in provenance.

### Transcript indexing

Preserve corpus handle, video ID, timestamp span, transcript revision/hash, text, validation, and retrieval model identity.

### Hybrid retrieval

Combine semantic ranking with lexical/exact signals.

Exact-string benchmark categories must come from multiple domains, including names/dates/quotes, legal/document identifiers, quantities/prices, citations, scientific notation, product/model identifiers, scores, UI/game labels, and codes.

### v0.4 exit criteria

- at least 100 hours searchable without loading the corpus into model context,
- old index revisions remain reproducible,
- benchmark Recall@k/timestamp targets are met across multiple domain strata,
- missing captions can enter the same search workflow through local STT.

---

## v0.5 — Multimodal temporal evidence

Goal: retrieve what was **shown**, not only what was said.

### Pipeline

1. bounded/adaptive frame hydration,
2. scene/change-point detection,
3. representative keyframe selection,
4. OCR/on-screen text extraction,
5. visual embedding/description,
6. temporal alignment with transcript segments,
7. cross-modal retrieval and evidence fusion.

### Domain-neutral visual targets

Visual retrieval must handle more than charts/technical diagrams. Benchmarks should include:

- historical maps and archival imagery,
- lecture slides/quotations,
- cooking consistency/technique demonstrations,
- products and physical failure demonstrations,
- artwork/film/game imagery,
- sports/game state,
- UI/screens,
- diagrams/scientific visuals,
- practical how-to steps,
- text rendered only into pixels.

### OCR target

OCR evidence should retain frozen frame SHA/timestamp, exact text, confidence/bbox, and OCR engine identity. Exact on-screen strings remain searchable without pretending OCR is a semantic-only problem.

### v0.5 exit criteria

- on-screen-only text can be retrieved,
- visual retrieval finds target scenes not named in speech,
- returned media is consumable through MCP resources,
- cross-modal results retain separate speech/visual/OCR provenance,
- domain-diverse benchmark tasks show no single-field dependency.

---

## v0.6 — Distribution and remote operation

Goal: remove installation friction without weakening local-first capabilities.

- PyPI/uvx,
- Docker,
- official MCP Registry metadata,
- signed/versioned releases,
- Streamable HTTP with authentication/rate/resource bounds,
- protocol-native media resources,
- storage/cache health diagnostics,
- doctor/config checks for optional ML/media backends.

---

## v0.7 — Compound research workflows

Goal: make complex evidence retrieval one good call rather than fragile model-side orchestration.

Candidate jobs:

- retrieve evidence for one research question across selected modalities,
- compare sources/entities/claims across explicit dimensions,
- trace position/claim changes across time,
- surface agreement/disagreement while preserving the underlying evidence set.

These tools retrieve and structure evidence; the calling model remains responsible for synthesis and judgment.

Cross-domain compound tasks are mandatory benchmarks here, not edge cases.

---

## Target public surface

The mature surface should remain a compact set around:

- discovery/search,
- inspect/transcript/media,
- corpus build/compose/get/list/diff,
- corpus hydration/index/search,
- one or two compound evidence retrieval jobs,
- health/doctor where appropriate.

`corpus.compose` earns a public tool because heterogeneous source selection is a distinct agent job. Internal embedding/OCR/scene primitives do not automatically earn public tools.

Tool-count growth is not a success metric.

## Explicit non-goals

- Becoming a YouTube Studio/channel-management suite.
- Reimplementing yt-dlp.
- Becoming an engineering-specific research product.
- Encoding one discipline's ontology into the core evidence model.
- Hiding model/API costs behind opaque behavior.
- Returning generated summaries without source evidence.
- Making one vector DB or ML model a permanent public dependency.

## Definition of "best"

Market leadership requires the public benchmark to demonstrate task success, retrieval quality, provenance, efficiency, and resilience **across domains and media conditions**. See `BENCHMARK.md`.
