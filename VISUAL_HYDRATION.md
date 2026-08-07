# Visual evidence hydration

`corpus.visual_hydrate` populates trustworthy timestamped frame coverage for a frozen corpus without forcing the calling model to issue one `frame.get` call per video.

The intended multimodal workflow is:

```text
skeleton.build(...)
      ↓
corpus.hydrate(...)          # spoken evidence cache, where needed
      ↓
corpus.visual_hydrate(...)   # repeat with next_cursor
      ↓
corpus.prepare(...)
      ↓
corpus.visual_search(...)
```

Visual hydration is an **acquisition** operation. The managed frame cache it creates remains mutable/rebuildable. `corpus.visual_search` later freezes exact frame bytes by SHA-256 into immutable visual evidence artifacts before indexing them.

## Call shape

```text
corpus.visual_hydrate(
    handle,
    cursor=None,
    batch_size=2,
    max_workers=1,
    n=12,
    layout="4x3",
    size="1280x720",
    fmt="png",
    policy="missing",
)
```

Initial hard bounds are intentionally conservative:

- `batch_size`: 1–6 videos per call
- `max_workers`: 1–2 concurrent video extractions
- `n`: 1–24 timestamped frames per video
- layout dimensions: each 1–8 and must have room for `n`
- frame dimensions: 64–4096 pixels per side
- formats: PNG or JPEG

A visual candidate may require an entire video download plus multiple ffmpeg frame extractions, so visual hydration should not inherit the larger transcript-hydration batch limits.

## Cache-satisfaction rule

The first policy is `missing`.

A corpus member is skipped only when the managed frame cache already contains at least the requested `n` **trustworthy timestamped frame records**. Trusted timestamps come from:

- managed `single_<timestamp-ms>...` frame filenames, or
- managed contact-sheet manifests whose timestamp/path arrays resolve directly inside that video's managed frame directory.

A video with only 3 trusted frames is therefore still a hydration candidate when `n=12`.

## Resume semantics

The cursor is a stable zero-based frozen-corpus position encoded as a string.

A result contains:

- `cursor`
- `next_cursor`
- `complete`
- total/scanned/remaining member counts
- cached-skip count
- attempted/succeeded/failed counts
- requested frame/layout/size/format policy
- compact per-video results

Failures still advance the cursor. One unavailable or slow video cannot pin the entire corpus to a single position.

To retry unresolved members later, restart from cursor `0`. Videos that subsequently have sufficient trustworthy frame coverage are skipped automatically.

## Compact result contract

The hydration response intentionally does **not** contain:

- frame bytes,
- contact-sheet bytes,
- local frame paths,
- local downloaded-video paths, or
- raw yt-dlp / ffmpeg diagnostics.

Each attempted member returns only:

- frozen corpus position
- video ID/title
- `ready` or `failed`
- count of trustworthy frames already cached before the attempt
- whether the contact-sheet extraction itself was a cache hit
- resulting frame count
- first/last timestamp
- compact structured error when acquisition fails

This keeps a multi-video hydration call small enough for agent orchestration and prevents media filesystem details from becoming model context.

## Failure handling

The underlying frame extractor retains its own hard yt-dlp/ffmpeg subprocess deadlines. The hydration layer deliberately does not add fake `Future.cancel()` timeouts around running threads because that would not terminate the actual media process.

Hydration classifies high-level timeout/missing-dependency failures but does not forward raw media subprocess diagnostics. This is important as authenticated/proxied media acquisition becomes integrated, because such diagnostics may contain local or credential-adjacent configuration.

## Why acquisition and indexing stay separate

A visual search request should never unexpectedly mean:

> download 300 videos, decode them, produce thousands of frames, load a vision model, and then search.

Separating hydration from preparation gives the caller explicit control over expensive media work:

```text
bounded acquisition
      ↓
mutable managed frame cache
      ↓
content-addressed freeze
      ↓
immutable visual index
      ↓
query
```

That separation also makes cost, resumability, provenance, and future task scheduling much easier to reason about.

## Future policies

Potential later policies include:

- refresh frames older than a configured acquisition age,
- scene-boundary hydration instead of evenly spaced frames,
- targeted timestamp hydration around transcript/OCR candidates, and
- adaptive frame density based on video duration or scene-change rate.

Those should remain explicit policies rather than silently changing what `missing` means.
