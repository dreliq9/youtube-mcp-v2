# YouTube-MCP Bench harness

This directory implements the executable portion of `../BENCHMARK.md`.

The first revision is intentionally small and deterministic: 30 no-network cases that exercise contracts the server should never regress because of YouTube throttling or layout changes.

## Run

From the repository root:

```bash
python bench/run.py
```

Outputs are written to the ignored `bench/results/` directory:

- `latest.json` — machine-readable environment, per-suite totals, expected/actual values, and mismatches
- `latest.md` — compact human-readable report

A nonzero exit code means at least one scored case failed.

## First 30 cases

The initial manifest covers:

- common YouTube video URL forms and invalid-input rejection,
- transcript validation gates including empty, sparse, duplicated/high-rate, language fallback, truncation, timestamp ordering, missing duration, and boundary values,
- transcript caller guards that must fail before any network access,
- backward compatibility for legacy skeleton handles and the new microsecond handle form.

`tests/test_benchmark_harness.py` executes the entire manifest as part of the ordinary deterministic pytest suite and requires 30/30 cases to pass.

## Manifest contract

`manifest.schema.json` is the normative JSON Schema. The runner also performs the critical validation itself using only the standard library so benchmark CI does not need another package.

Every case has:

```json
{
  "id": "stable-case-id",
  "suite": "transcript",
  "task": "adapter-task-name",
  "mode": "deterministic",
  "input": {},
  "expected": {}
}
```

The scorer treats expected objects as subsets of adapter output. This lets adapters return extra diagnostics without changing case semantics.

## Adapter model

The manifest selects an adapter by name. An adapter exposes:

```python
run_case(task: str, inputs: dict) -> dict
```

The first adapter, `bench.adapters.youtube_mcp_v2`, calls the in-repo implementation directly and normalizes results for scoring.

Future competitor adapters should map the same benchmark intent to the smallest reasonable public workflow exposed by that server. Do not encode competitor-specific advantages away; report configuration and external services explicitly.

## What comes next

The next benchmark increments should add:

1. deterministic mocked acquisition failures and fallback-attempt scoring,
2. protocol/stdio client-server tests rather than only direct Python calls,
3. frozen corpus-diff fixtures,
4. live metadata/transcript cases behind an explicit live flag,
5. competitor adapters,
6. semantic/visual temporal-retrieval scoring as those product capabilities land.

Live cases must remain opt-in and must never block ordinary PR CI solely because YouTube throttled a runner IP.
