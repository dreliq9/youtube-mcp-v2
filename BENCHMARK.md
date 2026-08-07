# YouTube-MCP Bench

A market-leading YouTube MCP needs a reproducible definition of "better." The benchmark measures whether an AI can recover trustworthy evidence from video efficiently—not raw endpoint count and not performance in one favored subject area.

## Core benchmark rule: two independent axes

The dataset must vary along **both**:

1. **media/acquisition difficulty**, and
2. **knowledge domain / research task**.

A benchmark that contains many caption types but mostly one subject is not domain-neutral. A benchmark that contains many subjects but only easy captioned talking-head videos does not test the product architecture.

## Principles

- **Public and reproducible.** Store video IDs, expected annotations, hashes, and scoring code—not redistributed copyrighted payloads.
- **Domain-diverse.** No single field should dominate the reference set or examples.
- **Upstream-aware.** Separate deterministic server regressions from live YouTube outages/blocks.
- **Task-level.** Measure whether an agent retrieves the right evidence, not only HTTP success.
- **Adversarial.** Include long videos, missing/auto captions, multilingual content, Shorts, livestream archives, visual-only evidence, and changing metadata.
- **Frozen revisions.** Benchmark manifests are versioned; changed annotations require a new revision.
- **Evidence-first.** Correctness is scored against timestamped supporting source evidence.

## Dataset target

Mature set: **180–300 videos**.

Initial useful set: **40–60 videos** is enough to avoid architecture-by-anecdote while keeping annotation practical.

### Axis A — media/acquisition strata

Every mature benchmark should cover:

1. short manual-caption video,
2. short generated-caption video,
3. 1–4 hour podcast/interview,
4. multilingual/manual captions,
5. multilingual/generated captions,
6. captions disabled/unavailable,
7. livestream archive,
8. Shorts,
9. visually dense material with charts/slides/maps/UI,
10. important evidence visible but not spoken,
11. low-quality/noisy speech appropriate for local-STT fallback,
12. videos whose metadata/caption availability changes across revisions.

### Axis B — domain/task strata

Target roughly balanced representation across at least these classes:

1. **history / documentary / archival** — dates, maps, causal claims, source disagreement,
2. **religion / philosophy / humanities** — interpretation, quotation recovery, conceptual comparison,
3. **science / mathematics / technology** — explanations, notation, diagrams, measurements, claims,
4. **politics / current-affairs source material** — who said what and when; primary-source recovery,
5. **cooking / craft / making** — procedural order, quantities, demonstrated techniques,
6. **product / consumer research** — independent demonstrations, failures, comparisons, pricing/spec claims,
7. **creative / culture** — literature, film, music, art, storytelling analysis,
8. **practical tutorials / repair / how-to** — visually grounded procedural evidence,
9. **sports / games / performance** — state changes, scores, tactics, demonstrations,
10. **long-form interviews / podcasts** — position tracking, recurring themes, disagreement,
11. **cross-domain synthesis** — one abstract question intentionally researched across unrelated fields.

Engineering/technical examples are welcome inside science/technology and practical-task strata, but should not dominate either the dataset or documentation.

## Exact-string evidence is domain-neutral

The lexical benchmark must include exact evidence from many domains, for example:

- person/place names,
- historical dates,
- quotations,
- statute/case/bill numbers,
- citations and book/paper titles,
- ingredient quantities and temperatures,
- prices and percentages,
- equations/scientific notation,
- product/model identifiers,
- sports scores,
- game/UI labels,
- error codes and configuration strings.

This tests the reason hybrid lexical+semantic retrieval exists without equating exact identifiers with engineering.

## Manifest proposal

```json
{
  "benchmark_version": "0.2",
  "case_id": "history-map-001",
  "video_id": "...",
  "media_strata": ["manual-caption", "visual-only-evidence"],
  "domain_strata": ["history-documentary"],
  "expected": {
    "has_transcript": true,
    "language": "en"
  },
  "queries": [
    {
      "id": "q1",
      "text": "Which border is highlighted when the narrator discusses the treaty?",
      "evidence_windows": [[121.0, 128.0]],
      "modalities": ["speech", "screen"]
    }
  ]
}
```

Use ranges/tolerances where metadata or caption timing can legitimately vary.

## Composed-corpus benchmark

At least one suite must require building a heterogeneous frozen corpus rather than using a single channel/topic result.

Example workflow:

1. freeze several independent topic/channel sets,
2. compose them with `corpus.compose`,
3. add curated direct videos,
4. remove known irrelevant sources,
5. retrieve evidence across the resulting mixed corpus,
6. derive revision B and prove revision A remains reproducible.

Metrics:

- membership/order correctness,
- duplicate handling,
- source-handle provenance,
- immutable derivation correctness,
- cross-domain retrieval Recall@k,
- tool calls/tokens needed to construct the research universe.

## Benchmark suites

### A. Installation and protocol

Measure clean install, launch, `tools/list`, first call, supported Python versions, Linux/macOS/Windows, legacy compatibility, and current MCP compatibility.

Metrics: install/launch success, cold-start latency.

### B. Metadata acquisition

Tasks: canonical ID, title/channel/duration, caption/language availability, livestream/age/embed signals where available.

Metrics: field accuracy, acquisition success, p50/p95 latency, provenance completeness.

### C. Transcript acquisition

Tasks: manual/generated captions, requested-language fallback, long video, unavailable captions, independent provider fallback, local STT.

Metrics: success rate, language correctness, transcript-type provenance, WER on benchmark-owned excerpts, timestamp alignment, validation error rate, fallback success.

### D. Long-context efficiency

Measure response tokens/bytes, calls required, chunk boundary loss/duplication, and time-to-first-useful-evidence.

### E. Corpus reproducibility and composition

Tasks: capture revisions, compose heterogeneous sets, derive revised sets, diff revisions, reproduce old searches.

Metrics: membership/diff precision, historical reproducibility, accidental mutation count (target zero).

### F. Semantic + exact transcript retrieval

Use both conceptual paraphrase queries and exact-string queries across different domains.

Metrics: Recall@1/5/10, MRR, timestamp-window hit rate, temporal distance, exact-string recall, token cost per successful retrieval.

### G. Visual evidence retrieval

Queries target maps, slides, diagrams, objects, demonstrations, artwork, charts, products, game/UI states, numeric values, or other imagery visible on screen.

Metrics: Recall@k, temporal error, evidence-resource availability, remote-client consumability.

### H. OCR / on-screen text (when implemented)

Use exact visible text across domains: dates on maps, quotations on slides, ingredient quantities, prices, scoreboard values, UI labels, citations, model identifiers, etc.

Metrics: normalized/exact text match, bbox localization, frame/timestamp identity, provenance completeness.

### I. Cross-modal research

Examples should deliberately vary by domain:

- "Find where the historian explains the territorial change and return the map being discussed."
- "Find where the cook says the sauce is ready and show the visual consistency at that moment."
- "Find where the reviewer criticizes durability and return the demonstrated failure."
- "Find where the coach discusses the defensive adjustment and return the game state shown."
- "Across several disciplines, retrieve evidence for competing definitions of productive failure."

Metrics: evidence-set precision/recall, modality coverage, speech/frame alignment, agent task completion.

### J. Fault tolerance

Mock 429s, timeout, malformed pages, blocked providers, missing binaries, quota exhaustion, stale/corrupt cache, and fallback success.

Metrics: server survival, structured-error correctness, fallback correctness, recovery time, secret leakage (target zero).

## Domain-balance reporting

Every benchmark report must include results **by domain stratum**, not just an aggregate score. This prevents a server from appearing strong because the reference set happens to resemble its favored use cases.

Report at minimum:

- task success per domain,
- Recall@k per domain,
- evidence-token cost per domain,
- modality success per domain,
- acquisition failure rate per domain/media stratum.

No "best" claim should be published if one or two domain classes are absent or dramatically underrepresented.

## Provenance completeness score

Score whether evidence exposes/resolves canonical video ID, provider, method, fetch time, requested/actual language when applicable, evidence/transcript type, validation state, corpus/index revision, and content/revision hash.

Report the distribution, not only the mean.

## Agent task success benchmark

Run fixed end-user prompts with the same calling model, system prompt, token budget, and tool-call budget.

Record:

- correct completion,
- MCP calls,
- errors/retries,
- tool-result tokens,
- wall-clock latency,
- timestamped evidence support,
- unsupported final claims.

This remains the most important competitive metric.

## Competitive adapters

Map each competitor's public workflow to benchmark tasks without intentionally crippling it. Document optional keys/services and separate paid external services. Distinguish project claims from observed behavior.

## Reporting

Every run emits benchmark manifest revision, server/competitor versions, runtime/OS, optional provider/model configuration, timestamp, raw case JSON, and summary Markdown/CSV.

Publish the task/domain matrix rather than hiding tradeoffs in one aggregate score.
