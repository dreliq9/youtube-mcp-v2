# YouTube-MCP Bench harness

This directory implements the executable portion of `../BENCHMARK.md`.

## Run

From the repository root, one command runs the whole deterministic suite (no network) and enforces the per-suite regression thresholds:

```bash
python bench/run.py --check-thresholds
```

Outputs go to the ignored `bench/results/` directory:

- `latest.json`: raw results. Covers run metadata, per-suite and per-stratum summaries, threshold results, and every case's expected/actual/mismatches/metrics.
- `latest.md`: human-readable report with a suite × metric table, domain/media strata, adapters, known issues, and failures.
- `latest.csv`: one row per case.

The command exits nonzero when a scored case fails or a threshold is violated. Known-issue cases that unexpectedly pass (`xpass`) are flagged in the report but don't fail the run.

Other modes:

```bash
python bench/run.py --manifest bench/cases/core.json         # one manifest (repeatable)
python bench/run.py --mode live --no-fail                    # live network cases, report only
python bench/run.py --mode live --adapter mcp_youtube_transcript --no-fail
```

Live mode is manual/scheduled only (`bench/ci/bench-live.yml`) and never runs in PR CI.

## Manifests (`bench/cases/*.json`)

| File | Revision | Cases | Purpose |
|---|---|---:|---|
| `deterministic.json` | 0.1 (frozen) | 30 | URL parsing, validation gates, caller guards, skeleton-handle compatibility |
| `core.json` | 0.2 | 33 | protocol/install over stdio, metadata, transcript provenance + temporal evidence, long-context, fault tolerance |
| `live.json` | 0.2-live | 13 | real public video IDs with annotated metadata and evidence windows |

`manifest.schema.json` is the normative JSON Schema (Draft 2020-12). `bench/loader.py` validates it plus semantic rules: known scoring methods, fixture boundaries, required temporal windows, and case IDs unique across all files. The run records every manifest's SHA-256 and a combined `revision` ID. Annotations are frozen per revision, so changing one means bumping `benchmark_version`.

Manifests store IDs, annotations, and hashes only:

- Live cases name public video IDs and hand-verified evidence windows.
- Deterministic cases reference benchmark-owned synthetic transcripts and watch pages (`bench/fixtures/synthetic.py`) by key, and assert their SHA-256.
- No third-party video, page, or transcript payload is stored in manifests or written to reports. Adapters return hashes, counts, and timestamps, never text.

A case:

```json
{
  "id": "tx-evidence-vtt-hour-timestamps",
  "suite": "transcript",
  "task": "transcript_evidence",
  "mode": "deterministic",
  "video_id": "bnchTxSprt2",
  "domain_strata": ["sports"],
  "media_strata": ["livestream-archive", "long", "vtt"],
  "input": {"url_or_id": "bnchTxSprt2", "query": "final score is three to two"},
  "fixture": {"native": {"raise": "NoTranscriptFound"}, "ytdlp": {"manual": {"en": "sport-final"}, "format": "vtt"}},
  "expected": {"ok": true},
  "scoring": {"method": "temporal_window", "params": {"windows": [[3720.5, 3725.9]], "tolerance_s": 0.25, "k": 1}}
}
```

`expected` is subset-matched: adapters may return extra diagnostics. Supported operators are `$approx`/`$tol`, `$gte`, `$lte`, `$in`, `$contains`, `$present`, `$type`, and `$len`.

A case with `known_issue` documents a real gap. A failure is reported as `xfail`, and a pass as `xpass`, which is the signal to drop the marker.

## Scoring (`bench/scoring.py`)

| Method | Metrics |
|---|---|
| `subset` | none; pass/fail on `expected` |
| `metadata_fields` | `field_accuracy` (fraction of expected fields correct) |
| `provenance` | `provenance_completeness` over video ID, provider, method, fetch time, requested/actual language, transcript type, validation state, attempts, content hash (default minimum 0.8) |
| `temporal_window` | `temporal_hit` (the cited start of a top-k hit is inside a window ± tolerance), `reciprocal_rank`, `temporal_error_s`, `start_offset_s` |
| `fault` | `survived` (no exception escaped), `structured_error` (expected machine code), `fallback_correct` (expected provider served) |

Every case is also checked for:

- **Secret leaks.** Deterministic cases run with synthetic canary credentials: an API key, proxy passwords, a cookie path, and an auth header. They're scanned for in adapter output, full tool envelopes, captured logs, and server stderr.
- **Network use.** Outbound sockets are blocked and recorded.

Either one fails the case.

Summaries are reported per suite, per domain stratum, and per media stratum, and include latency p50/p95. There is deliberately no single aggregate score.

## Regression thresholds (`bench/thresholds.json`)

Each suite has its own gates, for example:

- `cases.min`
- `pass_rate.min`
- `field_accuracy.min`
- `provenance_completeness.min`/`.mean`
- `temporal_hit.mean`
- `start_offset_s.max`
- `survived.min`
- `structured_error.min`
- `xfail.max`

Global gates cover `failed`, `secret_leaks`, and `network_attempts`. A suite missing from the report counts as a violation.

## Deterministic upstream fixtures (`bench/fixtures/upstream.py`)

A case's `fixture` scripts the outermost upstream boundary. The server's own parsing, fallback, validation, and cache code runs unmodified:

- `native`: the youtube-transcript-api client. Serves tracks or raises `IpBlocked`, `TranscriptsDisabled`, `NoTranscriptFound`, `ReadTimeout`, or other errors.
- `native_malformed`: corrupts the first snippet of the native payload.
- `ytdlp`: the yt-dlp subprocess. Serves VTT or JSON3 manual/automatic captions, or raises a timeout or missing-binary error. It can also emit stderr that echoes the canary proxy, cookie path, and auth header.
- `watch_page`: the HTTP transport. Serves a synthetic watch page, an HTTP status such as 429, or raw HTML.
- `data_api`: the googleapiclient boundary. Raises an `HttpError`, for example a 403 `quotaExceeded`.
- `cache`: seeds video metadata or writes a corrupt transcript row.

Each case gets a fresh temporary cache. Any boundary a case doesn't script is caught by the network guard.

## Adapters (`bench/adapters/`)

An adapter module exposes:

```python
run_case(task, inputs, *, case=None, mode="deterministic") -> dict
describe(mode) -> dict           # version, launch command, config, optional providers
supports(task, mode) -> (bool, reason | None)   # False -> "unsupported", not "failed"
reset() -> None                  # optional
```

The v0.1 two-argument `run_case(task, inputs)` is still accepted.

**`youtube_mcp_v2`** (this repo):

- Fixture cases call the registered tool functions in-process.
- Protocol cases launch the real stdio server (`python -m youtube_mcp_v2`) through the MCP SDK client (`bench/mcp_stdio.py`). They measure initialize, `tools/list`, the documented tool surface, schemas, a structured first-call error, and survival after an unknown tool call.
- The install case reads the installed distribution's metadata: version consistency, console script, and `Requires-Python`.
- Live cases also go through stdio with a fresh cache directory.
- Optional providers are disabled unless you name them in `BENCH_PASSTHROUGH_ENV` (for example `BENCH_PASSTHROUGH_ENV=YOUTUBE_API_KEY`). Run metadata records host availability as booleans and records which providers were actually enabled.

**`mcp_youtube_transcript`** ([jkawamoto/mcp-youtube-transcript](https://github.com/jkawamoto/mcp-youtube-transcript)), a transcript-focused competitor:

- Launched with its README default configuration: no flags, proxies, or paid services. The command is `uvx --from git+https://github.com/jkawamoto/mcp-youtube-transcript@<pinned ref> mcp-youtube-transcript`.
- Override the ref with `BENCH_MCP_YOUTUBE_TRANSCRIPT_REF`, or the whole command with `BENCH_MCP_YOUTUBE_TRANSCRIPT_CMD` (a JSON list or shell string).
- The PyPI release (0.3.5) doesn't start under the `mcp` 2.x SDK, so the git install is used.
- Installing it needs network, and its upstream calls can't be scripted from outside its process. Its deterministic cases are therefore `unsupported`. The exception is protocol cases, which run when a local command is configured.
- Its live cases use `get_timed_transcript`, `get_video_info`, and `get_available_languages`.
- Only fields the server itself returns count toward provenance.
- Its `duration` is a humanized string (for example "3 minutes"), which is parsed and scored as-is.

## CI

- `tests/test_benchmark_harness.py` and `tests/test_benchmark_suites.py` run inside the ordinary deterministic pytest job. They execute every deterministic manifest and assert that the per-suite thresholds pass.
- `bench/ci/benchmark.yml` is a dedicated benchmark job: single command, thresholds, and a job summary plus artifacts.
- `bench/ci/bench-live.yml` is the manual/weekly live run over both adapters. It is report-only.
- Both workflows are ready to move into `.github/workflows/`.

## Not yet covered

- Corpus/semantic/visual/OCR/cross-modal suites.
- WER on owned audio excerpts.
- Agent-task success runs.
- Recovery-time metrics.
- A domain-balanced live set of 40–60 videos. The live set here is 4 videos.
