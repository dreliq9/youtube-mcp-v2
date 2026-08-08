# Evidence model — draft for v0.3+

The current v0.2 envelope is intentionally simple:

```text
data + fetched_at + source + cache_age_s + validated + warnings + error
```

That remains useful, but `source="scrape"` is not enough once one result can pass through caption APIs, yt-dlp, cookies/proxies, local transcription, OCR, embeddings, and frozen corpus revisions.

This document proposes a compatible evolution toward an **Evidence Envelope**.

## Goals

The calling model should be able to answer four questions without guessing:

1. **What is the result?**
2. **Where did it come from?**
3. **What integrity checks actually ran?**
4. **What exact video/time/artifact supports it?**

## Compatibility strategy

Do not break every v0.2 consumer at once.

Recommended migration:

1. Keep all current top-level fields through v0.x.
2. Add optional `provenance`, `validation`, `evidence`, and `artifacts` objects.
3. Define `validated` as a compatibility projection of `validation.status == "pass"`.
4. Define current `source` as a coarse projection of `provenance.acquisition.provider_class` (`cache`, `api`, `scrape`, `local`).
5. Version the envelope schema explicitly before 1.0.

## Proposed envelope

```json
{
  "schema_version": "0.3",
  "data": {},
  "fetched_at": "2026-08-06T12:00:00Z",
  "source": "cache",
  "cache_age_s": 42,
  "validated": true,
  "warnings": [],
  "error": null,

  "provenance": {
    "server_version": "0.3.0",
    "acquisition": {
      "provider": "youtube-transcript-api",
      "method": "captions",
      "provider_class": "scrape",
      "attempts": []
    },
    "source_identity": {
      "video_id": "...",
      "canonical_url": "https://www.youtube.com/watch?v=..."
    },
    "language": {
      "requested": "fr",
      "actual": "en"
    },
    "transcript": {
      "type": "generated",
      "track_id": null
    },
    "revision": {
      "artifact_id": "...",
      "content_sha256": "...",
      "corpus_handle": null
    }
  },

  "validation": {
    "status": "pass",
    "checks": []
  },

  "evidence": [],
  "artifacts": []
}
```

## Acquisition attempts

Fallback should be observable without forcing the model to parse logs.

```json
{
  "provider": "native-captions",
  "method": "youtube_transcript_api",
  "started_at": "2026-08-06T12:00:00Z",
  "duration_ms": 412,
  "outcome": "blocked",
  "detail_code": "ip_blocked"
}
```

Followed by:

```json
{
  "provider": "yt-dlp",
  "method": "caption_track",
  "started_at": "2026-08-06T12:00:01Z",
  "duration_ms": 890,
  "outcome": "success",
  "detail_code": null
}
```

### Rules

- Do not include proxy credentials, cookie values, API keys, raw auth headers, or full exception traces.
- `detail_code` should be stable/machine-readable; human text belongs in warnings/error messages.
- A successful cache hit should still be able to resolve the provenance of the cached artifact's original acquisition.

## Validation model

Boolean `validated` is too coarse to explain partial validation. Keep it for compatibility, but record checks.

```json
{
  "status": "partial",
  "checks": [
    {
      "name": "non_empty",
      "status": "pass"
    },
    {
      "name": "timestamp_monotonicity",
      "status": "pass"
    },
    {
      "name": "word_rate",
      "status": "not_run",
      "reason": "duration_unavailable"
    }
  ]
}
```

Recommended statuses:

- `pass` — all required checks ran and passed,
- `fail` — one or more integrity checks failed,
- `partial` — required checks could not all run,
- `not_applicable` — artifact type has no defined validation gate yet.

Compatibility `validated` should be `true` only for `pass`.

## Evidence item

Evidence is a claim-supporting temporal region, not merely a search hit.

```json
{
  "evidence_id": "ev_...",
  "video_id": "...",
  "canonical_url": "https://www.youtube.com/watch?v=...&t=123s",
  "start_s": 123.2,
  "end_s": 131.8,
  "modalities": ["speech", "screen"],
  "text": "supporting transcript excerpt",
  "ocr_text": "TRANSIENT 412 W",
  "frame_artifact_id": "art_...",
  "scores": {
    "retrieval": 0.86,
    "lexical": 0.41,
    "visual": 0.92
  },
  "source_revision_id": "tr_...",
  "validation_status": "pass"
}
```

### Evidence rules

- Evidence always has a canonical source identity.
- Time-bounded media evidence always has `start_s`; use `end_s` where meaningful.
- Scores are namespaced by retrieval method. Do not collapse unlike scores into a fake universal probability.
- Search results may contain multiple evidence items from the same video.
- Generated summaries are never themselves evidence unless linked to underlying evidence items.

## Artifact model

A mature MCP must work both locally and remotely. A filesystem path alone is not a portable artifact contract.

```json
{
  "artifact_id": "art_...",
  "kind": "image",
  "mime_type": "image/png",
  "local_path": "/optional/local/path.png",
  "resource_uri": "youtube-mcp://artifact/art_...",
  "sha256": "...",
  "size_bytes": 182341,
  "created_at": "2026-08-06T12:00:00Z"
}
```

Rules:

- Local deployments may expose `local_path` as a convenience.
- Remote-safe clients consume MCP content/resource URIs rather than assuming access to the server filesystem.
- Every persisted artifact has a content hash.
- Artifact IDs are immutable references to bytes; regeneration creates a new artifact ID if bytes change.

## Revision identity

Suggested stored objects:

- `VideoMetadataRevision`
- `TranscriptRevision`
- `MediaArtifact`
- `CorpusRevision`
- `EmbeddingIndexRevision`

Each should have:

- stable object type,
- unique revision ID,
- capture/build timestamp,
- upstream identity,
- content/config hash,
- producer version,
- parent/source revision references where relevant.

This lets a search result answer not just "video X at 12:03" but "video X at 12:03 as represented by transcript revision Y in corpus revision Z."

## Content hashes

Hashes should answer reproducibility questions, not pretend YouTube itself provides immutable media IDs.

Recommended hashes:

- normalized transcript JSON SHA-256,
- metadata payload SHA-256,
- extracted frame/audio bytes SHA-256,
- corpus manifest SHA-256,
- embedding-index config hash (model + revision + chunker + source revision IDs).

Never use Python's process-randomized `hash()` as persistent identity.

## Search provenance

A `corpus.search` result should identify:

- corpus handle/revision,
- index revision,
- embedding model + revision,
- lexical/vector/hybrid retrieval mode,
- source transcript/frame revision,
- query filters,
- top-k requested.

This does not all need to be shown in natural-language UI, but it must be available to an agent or audit workflow.

## Confidence semantics

Avoid a single `confidence` field unless its meaning is formally defined.

Prefer method-specific scores:

- `retrieval_score`,
- `lexical_score`,
- `embedding_similarity`,
- `ocr_confidence`,
- `stt_segment_confidence`,
- `visual_similarity`.

A future learned fusion score may be added only when calibrated against `BENCHMARK.md`.

## Recommended implementation order

1. Add structured validation checks while keeping current boolean/warnings.
2. Persist acquisition attempts and transcript type in cache.
3. Add content hashes/revision IDs.
4. Attach corpus revision identity to corpus-scoped operations.
5. Introduce portable media artifact/resource references.
6. Use the same evidence item across transcript, OCR, visual, and hybrid search.

The goal is one coherent evidence model, not a different ad-hoc result shape for every new ML feature.
