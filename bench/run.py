"""Run a YouTube-MCP Bench manifest and emit reproducible JSON results.

This runner intentionally uses only the Python standard library. Live-network and
competitor adapters can be added later without making the deterministic CI suite
expensive or fragile.
"""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_ALLOWED_MODES = {"deterministic", "live"}
_REQUIRED_CASE_FIELDS = {"id", "suite", "task", "mode", "input", "expected"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_sha() -> str | None:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return cp.stdout.strip() if cp.returncode == 0 else None


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Perform the critical manifest checks without requiring jsonschema."""
    if not isinstance(manifest.get("benchmark_version"), str):
        raise ValueError("manifest benchmark_version must be a string")
    adapter = manifest.get("adapter")
    if not isinstance(adapter, str) or not adapter:
        raise ValueError("manifest adapter must be a non-empty string")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("manifest cases must be a non-empty list")

    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"case {index} must be an object")
        missing = _REQUIRED_CASE_FIELDS - set(case)
        if missing:
            raise ValueError(f"case {index} missing fields: {sorted(missing)}")
        case_id = case["id"]
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"case {index} id must be a non-empty string")
        if case_id in seen:
            raise ValueError(f"duplicate case id: {case_id}")
        seen.add(case_id)
        if case["mode"] not in _ALLOWED_MODES:
            raise ValueError(f"case {case_id} has unsupported mode {case['mode']!r}")
        if not isinstance(case["input"], dict) or not isinstance(case["expected"], dict):
            raise ValueError(f"case {case_id} input/expected must be objects")


def expected_subset(actual: Any, expected: Any, path: str = "$") -> list[str]:
    """Return mismatch descriptions while treating expected dicts as subsets.

    Adapter outputs may contain additional diagnostics; benchmark cases specify
    only the contract they intend to score.
    """
    mismatches: list[str] = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected object, got {type(actual).__name__}"]
        for key, expected_value in expected.items():
            if key not in actual:
                mismatches.append(f"{path}.{key}: missing")
                continue
            mismatches.extend(
                expected_subset(actual[key], expected_value, f"{path}.{key}")
            )
        return mismatches

    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected list, got {type(actual).__name__}"]
        if actual != expected:
            mismatches.append(f"{path}: expected {expected!r}, got {actual!r}")
        return mismatches

    if actual != expected:
        mismatches.append(f"{path}: expected {expected!r}, got {actual!r}")
    return mismatches


def _load_adapter(name: str):
    module_name = f"bench.adapters.{name}"
    module = importlib.import_module(module_name)
    run_case = getattr(module, "run_case", None)
    if not callable(run_case):
        raise ValueError(f"adapter {name!r} does not expose callable run_case")
    return run_case


def run_manifest(
    manifest: dict[str, Any], *, include_live: bool = False
) -> dict[str, Any]:
    validate_manifest(manifest)
    run_case = _load_adapter(manifest["adapter"])

    results: list[dict[str, Any]] = []
    suite_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"passed": 0, "failed": 0, "skipped": 0}
    )

    for case in manifest["cases"]:
        if case["mode"] == "live" and not include_live:
            result = {
                "id": case["id"],
                "suite": case["suite"],
                "status": "skipped",
                "reason": "live case disabled",
            }
            suite_counts[case["suite"]]["skipped"] += 1
            results.append(result)
            continue

        try:
            actual = run_case(case["task"], dict(case["input"]))
            mismatches = expected_subset(actual, case["expected"])
            status = "passed" if not mismatches else "failed"
            result = {
                "id": case["id"],
                "suite": case["suite"],
                "task": case["task"],
                "status": status,
                "expected": case["expected"],
                "actual": actual,
                "mismatches": mismatches,
            }
        except Exception as exc:
            status = "failed"
            result = {
                "id": case["id"],
                "suite": case["suite"],
                "task": case["task"],
                "status": "failed",
                "expected": case["expected"],
                "actual": None,
                "mismatches": [
                    f"adapter exception: {type(exc).__name__}: {exc}"
                ],
            }

        suite_counts[case["suite"]][status] += 1
        results.append(result)

    totals = {"passed": 0, "failed": 0, "skipped": 0}
    for counts in suite_counts.values():
        for key in totals:
            totals[key] += counts[key]

    try:
        from youtube_mcp_v2 import __version__ as server_version
    except Exception:
        server_version = None

    return {
        "benchmark_version": manifest["benchmark_version"],
        "adapter": manifest["adapter"],
        "ran_at": _now_iso(),
        "environment": {
            "git_sha": _git_sha(),
            "server_version": server_version,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "summary": {
            "totals": totals,
            "suites": dict(sorted(suite_counts.items())),
        },
        "cases": results,
    }


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["summary"]["totals"]
    lines = [
        "# YouTube-MCP Bench Report",
        "",
        f"- Benchmark: `{report['benchmark_version']}`",
        f"- Adapter: `{report['adapter']}`",
        f"- Git SHA: `{report['environment'].get('git_sha')}`",
        f"- Server: `{report['environment'].get('server_version')}`",
        f"- Python: `{report['environment'].get('python')}`",
        f"- Platform: `{report['environment'].get('platform')}`",
        "",
        f"**Passed:** {totals['passed']}  **Failed:** {totals['failed']}  **Skipped:** {totals['skipped']}",
        "",
        "| Suite | Passed | Failed | Skipped |",
        "|---|---:|---:|---:|",
    ]
    for suite, counts in report["summary"]["suites"].items():
        lines.append(
            f"| {suite} | {counts['passed']} | {counts['failed']} | {counts['skipped']} |"
        )

    failed = [case for case in report["cases"] if case["status"] == "failed"]
    if failed:
        lines.extend(["", "## Failures", ""])
        for case in failed:
            lines.append(f"### `{case['id']}`")
            for mismatch in case.get("mismatches", []):
                lines.append(f"- {mismatch}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run YouTube-MCP Bench cases")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "bench" / "cases" / "deterministic.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "bench" / "results",
    )
    parser.add_argument("--include-live", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = run_manifest(manifest, include_live=args.include_live)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "latest.json"
    md_path = args.output_dir / "latest.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")

    totals = report["summary"]["totals"]
    print(
        f"YouTube-MCP Bench: {totals['passed']} passed, "
        f"{totals['failed']} failed, {totals['skipped']} skipped"
    )
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
