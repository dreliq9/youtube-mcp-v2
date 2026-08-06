# YouTube-MCP Bench

A market-leading YouTube MCP needs a reproducible definition of "better." This benchmark is intended to measure the tasks an AI agent actually depends on rather than raw endpoint count.

## Principles

- **Public and reproducible.** Store video IDs, expected annotations, hashes, and scoring code — not redistributed copyrighted video/transcript payloads.
- **Upstream-aware.** Distinguish a deterministic server regression from a live YouTube outage/block.
- **Task-level.** Measure whether an agent can retrieve the right evidence, not just whether an HTTP request returned 200.
- **Adversarial.** Include long videos, missing captions, auto captions, multilingual content, Shorts, livestream archives, visual-only evidence, and changing metadata.
- **Frozen benchmark revisions.** Benchmark manifests are versioned; expected answers can evolve only in a new revision.

## Dataset structure

Target mature set: **150–250 videos** across roughly 10 strata.

Initial useful set: **30 videos** is enough to build the harness and prevent architecture-by-anecdote.

Suggested strata:

1. short manual-caption video,
2. short generated-caption video,
3. 1–4 hour podcast/interview,
4. multilingual/manual captions,
5. multilingual/generated captions,
6. captions disabled or unavailable,
7. livestream archive,
8. Shorts,
9. visually dense review/tutorial with charts or slides,
10. videos whose important evidence is on screen but not spoken.

Additional failure fixtures should mock rather than depend on finding a permanently broken public video:

- HTTP 429,
- timeout,
- malformed watch page,
- caption provider blocked,
- missing ffmpeg,
- missing yt-dlp,
- quota exhaustion,
- stale cache,
- corrupt cache row,
- provider A failure + provider B success.

## Manifest proposal

```json
{
  "benchmark_version": "0.1",
  "case_id": "manual-short-001",
  "video_id": "...",
  "strata": ["manual-caption", "short"],
  "expected": {
    "has_transcript": true,
    "language": "en",
    "duration_range_s": [55, 65]
  },
  "queries": [
    {
      "id": "q1",
      "text": "What component value is shown on screen?",
      "evidence_windows": [[121.0, 128.0]],
      "modalities": ["screen"]
    }
  ]
}
```

Use ranges/tolerances where YouTube metadata or caption timing can legitimately vary.

## Benchmark suites

### A. Installation and protocol

Measure:

- clean install success,
- server launch success,
- `tools/list` success,
- first tool call success,
- Linux/macOS/Windows,
- supported Python versions,
- legacy MCP client compatibility through SDK negotiation,
- modern 2026-07-28 client compatibility.

Metrics:

- install success rate,
- launch success rate,
- cold-start latency.

### B. Metadata acquisition

Tasks:

- canonical video ID resolution,
- title/channel/duration extraction,
- caption availability/language detection,
- livestream/age/embed flags when available.

Metrics:

- field accuracy,
- acquisition success rate,
- p50/p95 latency,
- provenance completeness.

### C. Transcript acquisition

Tasks:

- requested-language manual caption,
- language fallback,
- generated captions,
- long transcript,
- caption-disabled video,
- fallback provider path,
- local STT once v0.4 lands.

Metrics:

- acquisition success rate,
- requested-vs-actual language correctness,
- generated/manual provenance correctness,
- word error rate where a benchmark-owned reference excerpt exists,
- timestamp alignment error,
- validation false-positive/false-negative rate,
- provider fallback success rate.

Do not evaluate WER by redistributing full third-party transcripts. Reference small benchmark-owned clips or manually annotated short excerpts where legally appropriate.

### D. Long-context efficiency

Ask each server to make a long video usable for a model.

Metrics:

- response bytes/tokens,
- number of tool calls required,
- whether pagination/chunking loses or duplicates boundary content,
- time-to-first-useful-evidence.

### E. Corpus reproducibility

Tasks:

- capture channel/playlist revision A,
- capture revision B,
- identify additions/removals/metadata changes,
- reproduce a search against revision A after B exists.

Metrics:

- diff precision/recall,
- historical reproducibility,
- accidental mutation count (target: zero).

### F. Semantic evidence retrieval

For each annotated natural-language query, retrieve top-k transcript evidence.

Metrics:

- Recall@1 / Recall@5 / Recall@10,
- MRR,
- timestamp-window hit rate,
- median temporal distance from annotated evidence,
- token cost per successful retrieval.

A hit is successful when the returned time interval overlaps an accepted evidence window by a defined tolerance.

### G. Visual evidence retrieval

Queries target charts, slides, code, labels, physical objects, or numeric values visible on screen.

Metrics:

- Recall@k,
- temporal localization error,
- OCR exact/normalized match where applicable,
- evidence artifact availability,
- percentage of successful hits consumable by a remote MCP client rather than only via a server-local path.

### H. Cross-modal research tasks

Examples:

- "Find where the reviewer verbally criticizes transient response and show the chart being discussed."
- "Across this corpus, find every mention of sodium-ion and every slide containing the term even when it is not spoken."

Metrics:

- evidence-set precision/recall,
- modality coverage,
- timestamp alignment between speech and frame evidence,
- agent task completion rate with a fixed calling model/prompt.

### I. Fault tolerance

Use deterministic mocked provider faults.

Metrics:

- server survival rate,
- structured error correctness,
- fallback selection correctness,
- time to recovery,
- secret leakage count (target: zero).

## Provenance completeness score

Score one point each when an acquired artifact exposes or can resolve:

1. canonical video ID,
2. acquisition provider,
3. acquisition method,
4. fetch timestamp,
5. requested language,
6. actual language,
7. transcript type/manual-generated-local-STT,
8. validation state/checks,
9. corpus revision when corpus-scoped,
10. content/revision hash where supported.

Report the distribution, not only the mean.

## Agent task success benchmark

Raw retrieval metrics do not capture orchestration burden. Run a fixed set of end-user prompts against each MCP using the same model, system prompt, token budget, and maximum tool calls.

Record:

- task completed correctly,
- number of MCP calls,
- tool errors/retries,
- total tool-result tokens,
- wall-clock latency,
- whether the final answer cites timestamped supporting evidence,
- whether any final claim is unsupported by returned evidence.

This is the most important competitive metric.

## Competitive adapters

The harness should support adapters for other MCP servers without encoding their internal architecture. Each adapter maps benchmark tasks to the smallest reasonable public workflow exposed by that server.

Rules:

- use default/recommended installation,
- document optional keys/services enabled,
- report paid external services separately,
- never intentionally cripple a competitor,
- distinguish project-claimed functionality from functionality observed by the harness.

## Reporting

Every benchmark run emits:

- benchmark manifest revision,
- youtube-mcp-v2 git SHA/version,
- competitor versions/SHAs,
- OS/Python/runtime,
- configured optional providers/models,
- timestamp,
- raw per-case JSON,
- summary Markdown/CSV.

Do not publish "best" based on one aggregate score alone. Publish the task matrix so users can see tradeoffs.

## Initial implementation milestone

A first benchmark PR should contain:

- `bench/manifest.schema.json`,
- `bench/cases/*.json` for ~30 cases,
- deterministic mocked failure fixtures,
- scoring library,
- one youtube-mcp-v2 adapter,
- one transcript-focused competitor adapter,
- Markdown report generator,
- CI job for the deterministic subset.

Live network benchmark runs should be scheduled/manual and should never block ordinary PR CI.
