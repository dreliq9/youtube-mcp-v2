"""Run YouTube-MCP Bench manifests and emit reproducible JSON/Markdown/CSV.

Single command for the deterministic suite (what CI runs):

    python bench/run.py --check-thresholds

Live cases are opt-in (`--mode live`) and are meant for the manual/scheduled
workflow only; they never run in PR CI.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import inspect
import io
import json
import os
import platform
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench import loader, scoring  # noqa: E402
from bench.isolation import CaseCapture, deterministic_case, find_leaks  # noqa: E402
from bench.loader import LoadedManifest, ManifestError  # noqa: E402

HARNESS_VERSION = "0.2"
DEFAULT_THRESHOLDS = ROOT / "bench" / "thresholds.json"
MODES = ("deterministic", "live", "all")

# Backward-compatible public API (tests and older callers import these).
expected_subset = scoring.expected_subset


def validate_manifest(manifest: dict[str, Any]) -> None:
    loader.validate_manifest(manifest)


_CANARY_LABELS = {
    "AIzaBenchCanaryApiKey": "YOUTUBE_API_KEY",
    "benchCanaryProxyPw1": "YOUTUBE_TRANSCRIPT_PROXY credentials",
    "benchCanaryYtdlpPw2": "YOUTUBE_YTDLP_PROXY credentials",
    "benchCanaryCookieJar": "YOUTUBE_COOKIES_FILE path",
    "benchCanaryAuthHeader": "upstream auth header",
}


def _leak_labels(tokens: list[str]) -> list[str]:
    labels: list[str] = []
    for token in tokens:
        label = next((v for k, v in _CANARY_LABELS.items() if k in token), "canary")
        if label not in labels:
            labels.append(label)
    return labels


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(*args: str) -> str | None:
    try:
        cp = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return cp.stdout.strip() if cp.returncode == 0 else None


def environment() -> dict[str, Any]:
    try:
        from youtube_mcp_v2 import __version__ as server_version
    except Exception:  # noqa: BLE001
        server_version = None
    status = _git("status", "--porcelain")
    return {
        "git_sha": os.environ.get("GITHUB_SHA") or _git("rev-parse", "HEAD"),
        "git_dirty": bool(status) if status is not None else None,
        "server_version": server_version,
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "os": platform.system(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "ci": bool(os.environ.get("CI")),
    }


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class Adapter:
    """Uniform wrapper over adapter modules (supports the v0.1 2-arg API)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.module = importlib.import_module(f"bench.adapters.{name}")
        run_case = getattr(self.module, "run_case", None)
        if not callable(run_case):
            raise ValueError(f"adapter {name!r} does not expose callable run_case")
        self._run_case = run_case
        params = inspect.signature(run_case).parameters
        self._extended = "case" in params and "mode" in params

    def describe(self, mode: str) -> dict[str, Any]:
        fn = getattr(self.module, "describe", None)
        if callable(fn):
            return fn(mode)
        return {"name": self.name, "version": None}

    def supports(self, task: str, mode: str) -> tuple[bool, str | None]:
        fn = getattr(self.module, "supports", None)
        return fn(task, mode) if callable(fn) else (True, None)

    def run(self, case: dict[str, Any]) -> Any:
        inputs = dict(case["input"])
        if self._extended:
            return self._run_case(case["task"], inputs, case=case, mode=case["mode"])
        return self._run_case(case["task"], inputs)

    def reset(self) -> None:
        fn = getattr(self.module, "reset", None)
        if callable(fn):
            fn()


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _selected(case_mode: str, mode: str) -> bool:
    return mode == "all" or case_mode == mode


def _base_result(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": case["id"],
        "suite": case["suite"],
        "task": case["task"],
        "mode": case["mode"],
        "video_id": case.get("video_id"),
        "domain_strata": case.get("domain_strata", []),
        "media_strata": case.get("media_strata", []),
        "scoring": (case.get("scoring") or {}).get("method", "subset"),
    }


def run_case(adapter: Adapter, case: dict[str, Any]) -> dict[str, Any]:
    result = _base_result(case)
    ok, reason = adapter.supports(case["task"], case["mode"])
    if not ok:
        result.update(status="unsupported", reason=reason)
        return result

    capture = CaseCapture()
    started = time.perf_counter()
    actual: Any = None
    error: str | None = None
    try:
        if case["mode"] == "deterministic":
            with deterministic_case(capture):
                actual = adapter.run(case)
        else:
            actual = adapter.run(case)
    except Exception as exc:  # noqa: BLE001 - adapter/harness failures are results
        error = f"adapter exception: {type(exc).__name__}: {exc}"
    duration_ms = round((time.perf_counter() - started) * 1000, 1)

    scan_blobs: list[str] = [capture.log_text()]
    if isinstance(actual, dict):
        scan_blobs.extend(str(b) for b in actual.pop("_scan", []) or [])
    scan_blobs.append(json.dumps(actual, default=str))
    leaks = _leak_labels(find_leaks(*scan_blobs)) if case["mode"] == "deterministic" else []

    if error is not None:
        mismatches, score, metrics, passed = [error], 0.0, {}, False
    else:
        scored = scoring.score_case(case, actual)
        mismatches, score, metrics, passed = (
            scored.mismatches,
            scored.score,
            scored.metrics,
            scored.passed,
        )
    if capture.network_attempts:
        passed = False
        mismatches.append(f"network access attempted (blocked): {capture.network_attempts[:3]}")
    if leaks:
        passed = False
        mismatches.append(f"secret canary leaked: {leaks}")

    status = "passed" if passed else "failed"
    if case.get("known_issue"):
        status = "xpass" if passed else "xfail"
        result["known_issue"] = case["known_issue"]

    result.update(
        status=status,
        score=score,
        metrics=metrics,
        duration_ms=duration_ms,
        expected=case["expected"],
        actual=actual,
        mismatches=mismatches,
        secret_leaks=leaks,
        network_attempts=list(capture.network_attempts),
    )
    return result


def _strata(results: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        if result["status"] == "skipped":
            continue
        for label in result.get(key) or []:
            groups[label].append(result)
    return {
        label: {
            name: value
            for name, value in scoring.aggregate(items).items()
            if name in ("cases", "passed", "failed", "xfail", "pass_rate")
        }
        for label, items in sorted(groups.items())
    }


def run_manifests(
    manifests: list[LoadedManifest],
    *,
    mode: str = "deterministic",
    adapter_override: str | None = None,
) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    adapters: dict[str, Adapter] = {}
    results: list[dict[str, Any]] = []
    for manifest in manifests:
        name = adapter_override or manifest.data["adapter"]
        if name not in adapters:
            adapters[name] = Adapter(name)
            adapters[name].reset()
        adapter = adapters[name]
        for case in manifest.data["cases"]:
            if not _selected(case["mode"], mode):
                if mode == "deterministic":
                    result = _base_result(case)
                    result.update(status="skipped", reason="live case disabled")
                    results.append(result)
                continue
            results.append(run_case(adapter, case))

    by_suite: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_suite[result["suite"]].append(result)
    overall = scoring.aggregate(results)
    totals = {status: overall[status] for status in scoring.STATUSES}

    adapter_info = {
        name: adapter.describe("deterministic" if mode == "deterministic" else "live")
        for name, adapter in adapters.items()
    }
    versions = sorted({m.data["benchmark_version"] for m in manifests})
    return {
        "harness_version": HARNESS_VERSION,
        "benchmark_version": "+".join(versions),
        "adapter": ",".join(adapters),
        "mode": mode,
        "ran_at": _now_iso(),
        "benchmark": {
            "revision": loader.revision_id(manifests),
            "manifests": [m.describe() for m in manifests],
        },
        "environment": environment(),
        "adapters": adapter_info,
        "summary": {
            "totals": totals,
            "overall": overall,
            "suites": {suite: scoring.aggregate(items) for suite, items in sorted(by_suite.items())},
            "strata": {
                "domain": _strata(results, "domain_strata"),
                "media": _strata(results, "media_strata"),
            },
        },
        "thresholds": None,
        "cases": results,
    }


def run_manifest(
    manifest: dict[str, Any],
    *,
    include_live: bool = False,
    adapter: str | None = None,
) -> dict[str, Any]:
    """Run one in-memory manifest (compatibility entry point used by tests)."""
    return run_manifests(
        [loader.from_dict(manifest)],
        mode="all" if include_live else "deterministic",
        adapter_override=adapter,
    )


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def _check_bounds(scope: str, flat: dict[str, float], rules: dict[str, Any]) -> list[dict[str, Any]]:
    violations = []
    for metric, bound in rules.items():
        value = flat.get(metric)
        for kind in ("min", "max"):
            if kind not in bound:
                continue
            limit = bound[kind]
            bad = value is None or (value < limit if kind == "min" else value > limit)
            if bad:
                violations.append(
                    {"scope": scope, "metric": metric, "bound": kind, "limit": limit, "value": value}
                )
    return violations


def check_thresholds(report: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Per-suite regression gates. A suite missing from the report is a violation."""
    violations: list[dict[str, Any]] = []
    summary = report["summary"]
    violations += _check_bounds("global", scoring.flatten(summary["overall"]), thresholds.get("global", {}))
    for suite, rules in thresholds.get("suites", {}).items():
        if suite not in summary["suites"]:
            violations.append(
                {"scope": suite, "metric": "cases", "bound": "present", "limit": True, "value": None}
            )
            continue
        violations += _check_bounds(suite, scoring.flatten(summary["suites"][suite]), rules)
    return {"passed": not violations, "violations": violations}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".") if value != int(value) else f"{value:.0f}"
    return str(value)


def _key_metrics(summary: dict[str, Any]) -> str:
    parts = [f"{name} {_fmt(dist.get('mean'))}" for name, dist in summary.get("metrics", {}).items()]
    return ", ".join(parts) or "—"


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["summary"]["totals"]
    env = report["environment"]
    lines = [
        "# YouTube-MCP Bench Report",
        "",
        f"- Harness: `{report.get('harness_version')}` · Benchmark: `{report['benchmark_version']}`"
        f" · Revision: `{report['benchmark']['revision']}` · Mode: `{report['mode']}`",
        f"- Adapter(s): `{report['adapter']}`",
        f"- Git SHA: `{env.get('git_sha')}`" + (" (dirty)" if env.get("git_dirty") else ""),
        f"- Server: `{env.get('server_version')}`",
        f"- Python: `{env.get('python')}` ({env.get('python_implementation')})",
        f"- Platform: `{env.get('platform')}`",
        f"- Ran at: `{report['ran_at']}`",
        "",
        f"**Passed:** {totals['passed']}  **Failed:** {totals['failed']}  "
        f"**XFail:** {totals['xfail']}  **XPass:** {totals['xpass']}  "
        f"**Skipped:** {totals['skipped']}  **Unsupported:** {totals['unsupported']}",
        "",
        "Scores are reported per suite and per metric; there is deliberately no single aggregate score.",
        "",
        "| Suite | Cases | Passed | Failed | XFail | Skipped | Unsupported | Pass rate | Metrics (mean) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for suite, s in report["summary"]["suites"].items():
        lines.append(
            f"| {suite} | {s['cases']} | {s['passed']} | {s['failed']} | {s['xfail']} | "
            f"{s['skipped']} | {s['unsupported']} | {_fmt(s['pass_rate'])} | {_key_metrics(s)} |"
        )

    overall = report["summary"]["overall"]
    lines += [
        "",
        f"Secret-canary leaks: **{overall['secret_leaks']}** · Blocked network attempts: "
        f"**{overall['network_attempts']}** · Latency p50/p95 (ms): "
        + (
            f"{overall['latency_ms']['p50']} / {overall['latency_ms']['p95']}"
            if overall.get("latency_ms")
            else "—"
        ),
    ]

    for strata_name, strata in report["summary"]["strata"].items():
        if not strata:
            continue
        lines += ["", f"### {strata_name.title()} strata", "", "| Stratum | Cases | Passed | Failed | XFail |", "|---|---:|---:|---:|---:|"]
        for label, s in strata.items():
            lines.append(f"| {label} | {s['cases']} | {s['passed']} | {s['failed']} | {s['xfail']} |")

    lines += ["", "## Adapters", ""]
    for name, info in report["adapters"].items():
        lines.append(f"- **{name}** `{info.get('version')}` — {info.get('kind', '')}")
        if info.get("launch"):
            lines.append(f"  - launch: `{info['launch']}`")
        if info.get("config"):
            lines.append(f"  - config: {info['config']}")
        enabled = info.get("enabled_optional_providers")
        lines.append(f"  - enabled optional providers: {', '.join(enabled) if enabled else 'none'}")

    thresholds = report.get("thresholds")
    if thresholds is not None:
        lines += ["", "## Regression thresholds", ""]
        if thresholds["passed"]:
            lines.append("All per-suite thresholds met.")
        for v in thresholds["violations"]:
            lines.append(
                f"- ❌ `{v['scope']}` `{v['metric']}` {v['bound']} {v['limit']} (got {_fmt(v['value'])})"
            )

    failed = [c for c in report["cases"] if c["status"] in ("failed", "xpass")]
    if failed:
        lines += ["", "## Failures", ""]
        for case in failed:
            lines.append(f"### `{case['id']}` ({case['status']})")
            for mismatch in case.get("mismatches", []):
                lines.append(f"- {mismatch}")
            lines.append("")

    known = [c for c in report["cases"] if c["status"] == "xfail"]
    if known:
        lines += ["", "## Known issues (xfail)", ""]
        for case in known:
            issue = case.get("known_issue") or {}
            ref = f" ({issue['reference']})" if issue.get("reference") else ""
            lines.append(f"- `{case['id']}`: {issue.get('reason', '')}{ref}")

    unsupported = [c for c in report["cases"] if c["status"] == "unsupported"]
    if unsupported:
        reasons: dict[str, int] = defaultdict(int)
        for case in unsupported:
            reasons[case.get("reason") or ""] += 1
        lines += ["", "## Unsupported", ""]
        lines += [f"- {count} case(s): {reason}" for reason, count in sorted(reasons.items())]
    return "\n".join(lines).rstrip() + "\n"


CSV_FIELDS = (
    "id",
    "suite",
    "task",
    "mode",
    "status",
    "score",
    "duration_ms",
    "video_id",
    "domain_strata",
    "media_strata",
    "metrics",
)


def render_csv(report: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for case in report["cases"]:
        row = {name: case.get(name) for name in CSV_FIELDS}
        row["domain_strata"] = ";".join(case.get("domain_strata") or [])
        row["media_strata"] = ";".join(case.get("media_strata") or [])
        row["metrics"] = json.dumps(case.get("metrics") or {}, sort_keys=True)
        writer.writerow(row)
    return buffer.getvalue()


def write_outputs(report: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_dir / "latest.json",
        "markdown": output_dir / "latest.md",
        "csv": output_dir / "latest.csv",
    }
    paths["json"].write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    paths["markdown"].write_text(render_markdown(report), encoding="utf-8")
    paths["csv"].write_text(render_csv(report), encoding="utf-8")
    return paths


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run YouTube-MCP Bench cases")
    parser.add_argument(
        "--manifest",
        type=Path,
        action="append",
        help="manifest path (repeatable); default: every bench/cases/*.json",
    )
    parser.add_argument("--mode", choices=MODES, default="deterministic")
    parser.add_argument(
        "--include-live", action="store_true", help="alias for --mode all (v0.1 compatibility)"
    )
    parser.add_argument("--adapter", help="run every manifest against this adapter instead")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "bench" / "results")
    parser.add_argument(
        "--check-thresholds",
        nargs="?",
        const=DEFAULT_THRESHOLDS,
        type=Path,
        help=f"enforce per-suite regression thresholds (default file: {DEFAULT_THRESHOLDS.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="always exit 0 (live/competitor runs report results instead of gating)",
    )
    args = parser.parse_args(argv)
    mode = "all" if args.include_live else args.mode

    try:
        manifests = loader.load_manifests(args.manifest)
    except ManifestError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 2

    report = run_manifests(manifests, mode=mode, adapter_override=args.adapter)
    if args.check_thresholds:
        thresholds = json.loads(args.check_thresholds.read_text(encoding="utf-8"))
        report["thresholds"] = check_thresholds(report, thresholds)
    paths = write_outputs(report, args.output_dir)

    totals = report["summary"]["totals"]
    print(
        f"YouTube-MCP Bench ({mode}, revision {report['benchmark']['revision']}): "
        + ", ".join(f"{totals[s]} {s}" for s in scoring.STATUSES)
    )
    for suite, s in report["summary"]["suites"].items():
        print(f"  {suite:<16} {s['passed']}/{s['passed'] + s['failed']} passed  {_key_metrics(s)}")
    if report["thresholds"] is not None:
        for v in report["thresholds"]["violations"]:
            print(f"  THRESHOLD {v['scope']}.{v['metric']} {v['bound']} {v['limit']} got {v['value']}")
        print("Thresholds: " + ("met" if report["thresholds"]["passed"] else "VIOLATED"))
    for kind, path in paths.items():
        print(f"{kind}: {path}")

    if args.no_fail:
        return 0
    failed = totals["failed"] > 0  # xpass is flagged in the report, not gated
    if report["thresholds"] is not None and not report["thresholds"]["passed"]:
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
