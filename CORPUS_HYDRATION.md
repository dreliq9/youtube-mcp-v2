# Corpus hydration

`corpus.hydrate` fills the transcript cache for a frozen corpus without forcing the calling agent to issue one `transcript.get` request per video.

The intended research workflow is:

```text
skeleton.build(...)
      ↓
corpus.hydrate(...)  # repeat with next_cursor until complete
      ↓
corpus.prepare(...)
      ↓
corpus.search(...)
```

Hydration changes acquisition orchestration, not evidence semantics. Successful transcript acquisitions still enter the existing append-only transcript cache and later immutable corpus indexes freeze the exact transcript revision IDs/hashes they use.

## Call shape

```text
corpus.hydrate(
    handle,
    lang="en",
    cursor=None,
    batch_size=8,
    max_workers=3,
    policy="missing",
)
```

Bounds:

- `batch_size`: 1–25 acquisition attempts per call
- `max_workers`: 1–4 concurrent transcript acquisitions
- cursor: stable zero-based corpus position encoded as a string

A call scans forward from `cursor`, skipping members that satisfy the selected cache policy, until it has collected `batch_size` acquisition candidates or reaches the end of the frozen membership.

## Cache policies

### `missing`

Any historical transcript revision for the requested language satisfies hydration, regardless of age.

This is the default because frozen research values reproducibility: an older transcript is still useful evidence and `corpus.prepare` can pin it exactly.

### `fresh`

Only a transcript revision within the normal transcript cache TTL satisfies hydration. Stale members are sent through the normal transcript acquisition path again.

Use this when the objective is current acquisition state rather than preserving the cheapest available historical evidence.

## Resume behavior

A result contains:

- `cursor`
- `next_cursor`
- `complete`
- total/scanned/remaining member counts
- cached-skip count
- attempted/succeeded/failed counts
- compact per-attempt results

Pass `next_cursor` into the next call. When `complete=true`, `next_cursor` is `null`.

Failures **still advance the cursor**. One unavailable or throttled video therefore cannot trap the entire corpus at one position.

To retry failures later, restart from cursor `0`. Previously successful members are now cache hits and are skipped, so only unresolved members are attempted again.

## Compact result contract

Hydration intentionally does **not** return transcript text. Each attempted member returns only fields useful for orchestration/audit:

- corpus position
- video ID/title
- `ready` or `failed`
- source
- transcript validation status
- actual language
- generated-caption status
- acquisition timestamp
- warnings
- acquisition provenance when supplied by the transcript backend
- compact structured error on failure

The durable transcript body remains in the cache for `corpus.prepare` and direct `transcript.get` use. This prevents a 25-video hydration batch from dumping 25 transcript bodies into model context.

## Acquisition backend inheritance

Hydration calls the normal `transcript.get` implementation rather than implementing a separate downloader. It therefore inherits whichever acquisition waterfall is active in the integrated product: cache semantics, caption providers, proxy/cookie handling, future local-STT fallback, validation, and provenance all remain centralized.

## Concurrency and failure isolation

Concurrency is intentionally conservative. YouTube acquisition is frequently constrained by upstream throttling, so more parallelism is not automatically better.

The first implementation uses at most four threads and relies on the underlying transcript acquisition providers' bounded network/subprocess behavior. It does not fake a per-thread timeout with `Future.cancel()`, because cancelling a running thread would not stop the underlying request. Provider-level hard deadlines remain the correct enforcement boundary.

## Why not one giant hydration call?

A 500-video operation can take long enough to exceed client/server request budgets and makes failure recovery coarse. Bounded cursor batches give the caller:

- predictable work per call,
- resumability,
- compact progress reporting,
- easy throttling/backoff between calls,
- no dependence on MCP Tasks support, and
- natural compatibility with a future official long-running-task adapter.
