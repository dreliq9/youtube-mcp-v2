from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.run import expected_subset, render_markdown, run_manifest, validate_manifest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "bench" / "cases" / "deterministic.json"


def _load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_first_deterministic_manifest_has_30_unique_cases() -> None:
    manifest = _load_manifest()
    validate_manifest(manifest)
    ids = [case["id"] for case in manifest["cases"]]
    assert len(ids) == 30
    assert len(set(ids)) == 30
    assert all(case["mode"] == "deterministic" for case in manifest["cases"])


def test_deterministic_manifest_passes_against_in_repo_adapter() -> None:
    report = run_manifest(_load_manifest())
    assert report["summary"]["totals"] == {
        "passed": 30,
        "failed": 0,
        "skipped": 0,
    }, [
        (case["id"], case.get("mismatches"))
        for case in report["cases"]
        if case["status"] != "passed"
    ]


def test_expected_subset_allows_extra_adapter_diagnostics() -> None:
    actual = {"status": "ok", "diagnostics": {"provider": "local"}, "extra": 42}
    expected = {"status": "ok", "diagnostics": {"provider": "local"}}
    assert expected_subset(actual, expected) == []


def test_expected_subset_reports_nested_mismatch() -> None:
    mismatches = expected_subset(
        {"outer": {"value": 2}},
        {"outer": {"value": 1}},
    )
    assert mismatches
    assert "$.outer.value" in mismatches[0]


def test_manifest_rejects_duplicate_case_ids() -> None:
    manifest = _load_manifest()
    manifest["cases"][1]["id"] = manifest["cases"][0]["id"]
    with pytest.raises(ValueError, match="duplicate case id"):
        validate_manifest(manifest)


def test_markdown_report_contains_suite_table() -> None:
    report = run_manifest(_load_manifest())
    rendered = render_markdown(report)
    assert "# YouTube-MCP Bench Report" in rendered
    assert "| Suite | Passed | Failed | Skipped |" in rendered
    assert "**Passed:** 30" in rendered
