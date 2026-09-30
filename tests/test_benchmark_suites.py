"""Deterministic tests for the YouTube-MCP Bench harness (issue #4).

No live network: every case runs behind the benchmark socket guard, and the
competitor adapter is exercised through fake MCP tool-call results.
"""

from __future__ import annotations

import copy
import json
import socket
import sys
import types
from pathlib import Path

import pytest

from bench import evidence, loader, run, scoring
from bench.adapters import _common
from bench.adapters import mcp_youtube_transcript as competitor
from bench.fixtures import synthetic
from bench.isolation import (
    CANARY_ENV,
    CaseCapture,
    NetworkBlocked,
    block_network,
    scrubbed_env,
)
from bench.mcp_stdio import ToolCall

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "bench" / "cases" / "core.json"
LIVE = ROOT / "bench" / "cases" / "live.json"
THRESHOLDS = ROOT / "bench" / "thresholds.json"
EXPECTED_XFAIL = {"fault-corrupt-cache-row-recovers", "fault-malformed-watch-page-structured"}


@pytest.fixture(scope="module")
def full_report() -> dict:
    report = run.run_manifests(loader.load_manifests(), mode="deterministic")
    report["thresholds"] = run.check_thresholds(
        report, json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    )
    return report


def _manifest(*cases: dict, adapter: str = "youtube_mcp_v2") -> dict:
    return {"benchmark_version": "test", "adapter": adapter, "cases": list(cases)}


def _case(**overrides) -> dict:
    case = {
        "id": "t-case",
        "suite": "transcript",
        "task": "parse_video_id",
        "mode": "deterministic",
        "input": {"value": "jNQXAC9IVRw"},
        "expected": {"ok": True},
    }
    case.update(overrides)
    return case


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


def test_all_manifests_validate_against_schema_with_unique_ids() -> None:
    manifests = loader.load_manifests()
    names = {Path(m.path).name for m in manifests}
    assert {"deterministic.json", "core.json", "live.json"} <= names
    ids = [c["id"] for m in manifests for c in m.data["cases"]]
    assert len(ids) == len(set(ids))
    for m in manifests:
        assert len(m.sha256) == 64


def test_core_manifest_covers_required_suites() -> None:
    core = loader.load_manifest(CORE).data
    suites = {c["suite"] for c in core["cases"]}
    assert {"protocol", "metadata", "transcript", "fault_tolerance"} <= suites
    assert len(core["cases"]) >= 30
    assert all(c["mode"] == "deterministic" for c in core["cases"])
    live = loader.load_manifest(LIVE).data
    assert live["cases"] and all(c["mode"] == "live" for c in live["cases"])


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def test_manifests_store_ids_annotations_and_hashes_only() -> None:
    corpus_texts = [s.text for t in synthetic.TRANSCRIPTS.values() for s in t.segments]
    for m in loader.load_manifests():
        raw = json.dumps(m.data)
        assert not any(text in raw for text in corpus_texts), m.path
        # Tiny benchmark-authored validator inputs are fine; payload-sized
        # strings (a transcript, a page) are not.
        assert max(len(s) for s in _strings(m.data["cases"])) < 200, m.path
    for path in (CORE, LIVE):
        raw = path.read_text(encoding="utf-8")
        for key in ('"segments"', '"transcript_text"', '"snippets"', '"description"'):
            assert key not in raw.split('"cases"', 1)[1], (path, key)


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"scoring": {"method": "vibes"}}, "scoring"),
        ({"fixture": {"telepathy": {}}}, "fixture"),
        ({"video_id": "too-short"}, "video_id"),
        ({"mode": "sometimes"}, "mode"),
        ({"scoring": {"method": "temporal_window", "params": {}}}, "windows"),
    ],
)
def test_loader_rejects_invalid_cases(mutation: dict, message: str) -> None:
    with pytest.raises(loader.ManifestError, match=message):
        loader.validate_manifest(_manifest(_case(**mutation)))


def test_loader_rejects_duplicate_ids_across_files(tmp_path: Path) -> None:
    for name in ("a.json", "b.json"):
        (tmp_path / name).write_text(json.dumps(_manifest(_case())), encoding="utf-8")
    with pytest.raises(loader.ManifestError, match="duplicate case id"):
        loader.load_manifests(sorted(tmp_path.glob("*.json")))


def test_revision_id_changes_with_manifest_bytes() -> None:
    a = loader.from_dict(_manifest(_case()))
    b = loader.from_dict(_manifest(_case(id="other")))
    assert loader.revision_id([a]) != loader.revision_id([b])
    assert loader.revision_id([a]) == loader.revision_id([loader.from_dict(_manifest(_case()))])


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "actual, expected, ok",
    [
        ({"d": 19.4}, {"d": {"$approx": 19, "$tol": 0.5}}, True),
        ({"d": 21}, {"d": {"$approx": 19, "$tol": 1}}, False),
        ({"n": 5}, {"n": {"$gte": 5, "$lte": 5}}, True),
        ({"n": True}, {"n": {"$gte": 0}}, False),
        ({"o": "blocked"}, {"o": {"$in": ["blocked", "timeout"]}}, True),
        ({"w": ["a", "lang_fallback"]}, {"w": {"$contains": "lang_fallback"}}, True),
        ({"t": "Rick Astley - Never Gonna"}, {"t": {"$contains": "Never Gonna"}}, True),
        ({}, {"x": {"$present": False}}, True),
        ({"x": None}, {"x": {"$present": True}}, False),
        ({"l": [1, 2, 3]}, {"l": {"$len": {"$gte": 2}}}, True),
        ({"b": 1}, {"b": {"$type": "boolean"}}, False),
        ({"a": [{"p": "x", "extra": 1}]}, {"a": [{"p": "x"}]}, True),
    ],
)
def test_match_operators(actual, expected, ok) -> None:
    mismatches, _ = scoring.match(actual, expected)
    assert (not mismatches) is ok, mismatches


def test_temporal_window_scoring_hit_rank_and_miss() -> None:
    case = {
        "expected": {"ok": True},
        "scoring": {"method": "temporal_window", "params": {"windows": [[10, 12]], "tolerance_s": 1, "k": 2}},
    }
    hit_second = scoring.score_case(case, {"ok": True, "hits": [{"start_s": 50, "end_s": 52}, {"start_s": 9.5, "end_s": 14}]})
    assert hit_second.passed and hit_second.metrics["reciprocal_rank"] == 0.5
    assert hit_second.metrics["temporal_error_s"] == pytest.approx(37.0)
    late = scoring.score_case(case, {"ok": True, "hits": [{"start_s": 13.5, "end_s": 14}]})
    assert not late.passed, "a hit that starts after the evidence window must not count"
    miss = scoring.score_case(case, {"ok": True, "hits": [{"start_s": 30, "end_s": 31}]})
    assert not miss.passed and miss.metrics["temporal_hit"] == 0.0
    empty = scoring.score_case(case, {"ok": True, "hits": []})
    assert not empty.passed and "no hits" in empty.mismatches[-1]


def test_provenance_scoring_enforces_completeness() -> None:
    case = {"expected": {"ok": True}, "scoring": {"method": "provenance", "params": {"min_completeness": 0.8}}}
    full = {name: "x" for name in scoring.PROVENANCE_FIELDS}
    assert scoring.score_case(case, {"ok": True, "provenance": full}).passed
    sparse = dict.fromkeys(scoring.PROVENANCE_FIELDS)
    sparse["video_id"] = "abc"
    result = scoring.score_case(case, {"ok": True, "provenance": sparse})
    assert not result.passed and result.metrics["provenance_completeness"] == pytest.approx(0.1)


def test_fault_scoring_survival_structured_error_and_fallback() -> None:
    case = {"expected": {}, "scoring": {"method": "fault", "params": {"expect_error_code": "quota_exceeded"}}}
    assert scoring.score_case(case, {"exception": None, "error_code": "quota_exceeded"}).passed
    wrong = scoring.score_case(case, {"exception": None, "error_code": "internal"})
    assert not wrong.passed and "error_code" in wrong.mismatches[0]
    crashed = scoring.score_case(case, {"exception": "JSONDecodeError: x"})
    assert not crashed.passed and crashed.metrics["survived"] == 0.0
    fallback = {"expected": {}, "scoring": {"method": "fault", "params": {"expect_provider": "yt-dlp"}}}
    assert not scoring.score_case(fallback, {"exception": None, "provider": None}).passed


def test_aggregate_excludes_xfail_from_pass_rate() -> None:
    summary = scoring.aggregate(
        [
            {"status": "passed", "score": 1.0, "duration_ms": 1},
            {"status": "xfail", "score": 0.0, "duration_ms": 1},
            {"status": "unsupported"},
        ]
    )
    assert summary["pass_rate"] == 1.0
    assert (summary["xfail"], summary["unsupported"]) == (1, 1)


def test_evidence_locate_spans_segments_and_respects_word_boundaries() -> None:
    segments = [
        {"start_s": 0.0, "end_s": 2.0, "text": "we concatenate strings"},
        {"start_s": 2.0, "end_s": 4.0, "text": "the final score is"},
        {"start_s": 4.0, "end_s": 6.0, "text": "Three-to-two, a cat!"},
    ]
    assert evidence.locate(segments, "cat") == [{"start_s": 4.0, "end_s": 6.0}]
    assert evidence.locate(segments, "score is three to two") == [{"start_s": 2.0, "end_s": 6.0}]
    assert evidence.locate(segments, "absent phrase") == []


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_network_guard_blocks_and_restores() -> None:
    original = socket.create_connection
    capture = CaseCapture()
    with block_network(capture):
        with pytest.raises(NetworkBlocked):
            socket.create_connection(("www.youtube.com", 443), timeout=1)
        with pytest.raises(NetworkBlocked):
            socket.getaddrinfo("www.youtube.com", 443)
    assert socket.create_connection is original
    assert len(capture.network_attempts) == 2


def test_scrubbed_env_injects_canaries_and_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", "real-key-must-not-leak-into-cases")
    with scrubbed_env(CANARY_ENV):
        import os

        assert os.environ["YOUTUBE_API_KEY"] == CANARY_ENV["YOUTUBE_API_KEY"]
    import os

    assert os.environ["YOUTUBE_API_KEY"] == "real-key-must-not-leak-into-cases"


def _fake_adapter(monkeypatch: pytest.MonkeyPatch, name: str, **attrs) -> None:
    module = types.ModuleType(f"bench.adapters.{name}")
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, f"bench.adapters.{name}", module)


def test_runner_fails_cases_that_leak_canaries(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    def run_case(task, inputs, *, case=None, mode="deterministic"):
        if task == "leak_output":
            return {"ok": True, "detail": os.environ["YOUTUBE_TRANSCRIPT_PROXY"]}
        if task == "leak_log":
            import logging

            logging.getLogger("bench.test").warning("proxy=%s", os.environ["YOUTUBE_YTDLP_PROXY"])
            return {"ok": True}
        return {"ok": True, "_scan": ["stderr " + os.environ["YOUTUBE_API_KEY"]]}

    _fake_adapter(monkeypatch, "leaky_fake", run_case=run_case)
    report = run.run_manifest(
        _manifest(
            _case(id="a", task="leak_output"),
            _case(id="b", task="leak_log"),
            _case(id="c", task="leak_scan"),
            adapter="leaky_fake",
        )
    )
    by_id = {c["id"]: c for c in report["cases"]}
    assert by_id["a"]["secret_leaks"] == ["YOUTUBE_TRANSCRIPT_PROXY credentials"]
    assert by_id["b"]["secret_leaks"] == ["YOUTUBE_YTDLP_PROXY credentials"]
    assert by_id["c"]["secret_leaks"] == ["YOUTUBE_API_KEY"]
    assert all(c["status"] == "failed" for c in report["cases"])
    assert "_scan" not in by_id["c"]["actual"]
    assert report["summary"]["overall"]["secret_leaks"] == 3


def test_runner_fails_cases_that_touch_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def run_case(task, inputs):  # v0.1 two-argument adapter signature
        try:
            socket.create_connection(("www.youtube.com", 443), timeout=1)
        except OSError:
            pass
        return {"ok": True}

    _fake_adapter(monkeypatch, "net_fake", run_case=run_case)
    report = run.run_manifest(_manifest(_case(), adapter="net_fake"))
    [case] = report["cases"]
    assert case["status"] == "failed"
    assert case["network_attempts"] and "network access attempted" in case["mismatches"][-1]


def test_unscripted_upstream_boundary_is_caught_by_guard() -> None:
    # No cached duration -> transcript.get tries to fetch the watch page.
    case = _case(
        task="transcript_fetch",
        input={"url_or_id": "bnchGuard01"},
        video_id="bnchGuard01",
        fixture={"native": {"tracks": [{"lang": "en", "transcript": "hist-treaty"}]}},
    )
    [result] = run.run_manifest(_manifest(case))["cases"]
    assert result["status"] == "failed"
    assert result["network_attempts"]


def test_runner_statuses_known_issue_unsupported_and_live(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_adapter(
        monkeypatch,
        "status_fake",
        run_case=lambda task, inputs, *, case=None, mode="deterministic": {"ok": task == "good"},
        supports=lambda task, mode: (task != "nope", "not offered"),
    )
    report = run.run_manifest(
        _manifest(
            _case(id="xf", task="bad", known_issue={"reason": "tracked"}),
            _case(id="xp", task="good", known_issue={"reason": "fixed?"}),
            _case(id="un", task="nope"),
            _case(id="lv", task="good", mode="live"),
            adapter="status_fake",
        )
    )
    statuses = {c["id"]: c["status"] for c in report["cases"]}
    assert statuses == {"xf": "xfail", "xp": "xpass", "un": "unsupported", "lv": "skipped"}


# ---------------------------------------------------------------------------
# Full deterministic suite, thresholds, reports
# ---------------------------------------------------------------------------


def test_full_deterministic_suite_passes_with_only_known_xfails(full_report: dict) -> None:
    totals = full_report["summary"]["totals"]
    problems = [(c["id"], c.get("mismatches")) for c in full_report["cases"] if c["status"] in ("failed", "xpass")]
    assert totals["failed"] == 0 and totals["xpass"] == 0, problems
    assert {c["id"] for c in full_report["cases"] if c["status"] == "xfail"} == EXPECTED_XFAIL
    assert totals["unsupported"] == 0
    assert full_report["summary"]["overall"]["secret_leaks"] == 0
    assert full_report["summary"]["overall"]["network_attempts"] == 0


def test_full_deterministic_suite_meets_per_suite_thresholds(full_report: dict) -> None:
    assert full_report["thresholds"]["passed"], full_report["thresholds"]["violations"]
    thresholds = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    assert set(thresholds["suites"]) <= set(full_report["summary"]["suites"])


def test_thresholds_flag_missing_suites_and_regressions(full_report: dict) -> None:
    report = copy.deepcopy(full_report)
    report["summary"]["suites"]["metadata"]["pass_rate"] = 0.5
    del report["summary"]["suites"]["protocol"]
    result = run.check_thresholds(report, json.loads(THRESHOLDS.read_text(encoding="utf-8")))
    scopes = {(v["scope"], v["metric"]) for v in result["violations"]}
    assert ("metadata", "pass_rate") in scopes
    assert ("protocol", "cases") in scopes
    assert not result["passed"]


def test_report_records_run_metadata(full_report: dict) -> None:
    env = full_report["environment"]
    assert {"git_sha", "git_dirty", "server_version", "python", "os", "platform"} <= set(env)
    bench_meta = full_report["benchmark"]
    assert bench_meta["revision"] and all(m["sha256"] for m in bench_meta["manifests"])
    info = full_report["adapters"]["youtube_mcp_v2"]
    assert info["version"] == env["server_version"]
    assert {"mcp", "youtube-transcript-api", "yt-dlp"} <= set(info["dependencies"])
    assert all(isinstance(v, (bool, str)) for v in info["host_optional_providers"].values())
    assert info["enabled_optional_providers"]
    assert set(full_report["summary"]["strata"]) == {"domain", "media"}


def test_reports_contain_no_transcript_text_or_canaries(full_report: dict, tmp_path: Path) -> None:
    paths = run.write_outputs(full_report, tmp_path)
    blobs = [p.read_text(encoding="utf-8") for p in paths.values()]
    corpus_texts = [s.text for t in synthetic.TRANSCRIPTS.values() for s in t.segments]
    for blob in blobs:
        assert not any(text in blob for text in corpus_texts)
        assert not any(value in blob for value in CANARY_ENV.values())
    markdown = blobs[1]
    assert "# YouTube-MCP Bench Report" in markdown
    assert "no single aggregate score" in markdown
    assert "Known issues (xfail)" in markdown
    assert blobs[2].splitlines()[0].startswith("id,suite,task,mode,status")


def test_protocol_probe_runs_real_stdio_server(full_report: dict) -> None:
    probe = next(c for c in full_report["cases"] if c["id"] == "proto-stdio-initialize")["actual"]
    assert probe["launched"] and probe["tool_count"] >= 20
    assert probe["invalid_input"]["machine_code"] == "bad_url"


def test_cli_single_command_with_thresholds(tmp_path: Path) -> None:
    code = run.main(["--manifest", str(CORE), "--output-dir", str(tmp_path), "--check-thresholds",
                     str(_core_only_thresholds(tmp_path))])
    assert code == 0
    assert {p.name for p in tmp_path.iterdir()} >= {"latest.json", "latest.md", "latest.csv"}


def _core_only_thresholds(tmp_path: Path) -> Path:
    data = {"global": {"failed": {"max": 0}}, "suites": {"protocol": {"pass_rate": {"min": 1.0}}}}
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Competitor adapter (offline: fake tool results only)
# ---------------------------------------------------------------------------


def test_competitor_default_command_is_pinned_readme_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(competitor.CMD_ENV, raising=False)
    monkeypatch.delenv(competitor.REF_ENV, raising=False)
    cmd = competitor.command()
    assert cmd[0] == "uvx" and cmd[-1] == "mcp-youtube-transcript"
    assert cmd[2].endswith("@" + competitor.PINNED_REF)
    assert competitor.describe("deterministic")["enabled_optional_providers"] == []
    monkeypatch.setenv(competitor.CMD_ENV, '["mcp-youtube-transcript", "--response-limit", "0"]')
    assert competitor.command() == ["mcp-youtube-transcript", "--response-limit", "0"]


def test_competitor_describe_does_not_install_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(competitor.CMD_ENV, raising=False)

    def boom(*_a, **_k):
        raise AssertionError("describe() must not launch uvx in deterministic mode")

    monkeypatch.setattr(competitor.subprocess, "run", boom)
    assert competitor.describe("deterministic")["version"] is None


def test_competitor_support_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(competitor.CMD_ENV, raising=False)
    assert competitor.supports("transcript_fetch", "deterministic")[0] is False
    assert competitor.supports("protocol_probe", "deterministic")[0] is False
    assert competitor.supports("live_evidence", "live") == (True, None)
    monkeypatch.setenv(competitor.CMD_ENV, "mcp-youtube-transcript")
    assert competitor.supports("protocol_probe", "deterministic") == (True, None)


def test_competitor_core_run_reports_unsupported_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(competitor.CMD_ENV, raising=False)
    report = run.run_manifests([loader.load_manifest(CORE)], adapter_override="mcp_youtube_transcript")
    totals = report["summary"]["totals"]
    assert totals["unsupported"] == len(report["cases"]) and totals["failed"] == 0


def test_competitor_video_info_mapping() -> None:
    info = ToolCall(False, {"title": "Me at the zoo", "uploader": "jawed", "duration": "19 seconds",
                            "description": "d", "upload_date": "2005-04-23"}, "")
    langs = ToolCall(False, {"result": ['en ("English")', 'de ("German (auto-generated)")[TRANSLATABLE]']}, "")
    out = competitor.normalize_video_info(info, langs)
    assert out["ok"] and out["title"] == "Me at the zoo" and out["channel"] == "jawed"
    assert out["duration_s"] == 19 and out["available_caption_langs"] == ["en", "de"]
    assert out["channel_id"] is None
    failed = competitor.normalize_video_info(ToolCall(True, None, "Error executing tool get_video_info"))
    assert failed["ok"] is False and failed["error_code"] is None
    assert failed["error_message"].startswith("Error executing tool")


@pytest.mark.parametrize(
    "text, seconds",
    [("19 seconds", 19), ("3 minutes", 180), ("an hour", 3600), ("2 hours", 7200), ("a moment", None), (None, None)],
)
def test_humanized_duration_parsing_is_lossy_but_bounded(text, seconds) -> None:
    assert _common.parse_humanized_duration(text) == seconds


def test_competitor_transcript_mapping_hashes_and_scores_provenance() -> None:
    pages = [
        ToolCall(False, {"title": "t", "snippets": [{"text": "hello there", "start": 1.0, "duration": 2.0}], "next_cursor": "c1"}, ""),
        ToolCall(False, {"title": "t", "snippets": [{"text": "general kenobi", "start": 3.0, "duration": 2.5}], "next_cursor": None}, ""),
    ]
    out = competitor.normalize_transcript(pages, "en")
    assert out["ok"] and out["segment_count"] == 2
    assert out["text_sha256"] == _common.sha256_text("hello there general kenobi")
    assert evidence.locate(out["segments"], "there general") == [{"start_s": 1.0, "end_s": 5.5}]
    assert scoring.provenance_completeness(out) == 0.0
    assert competitor.normalize_transcript([ToolCall(True, None, "Error")], "en")["ok"] is False


def test_competitor_invalid_input_has_no_machine_code() -> None:
    call = ToolCall(True, None, "Error executing tool get_transcript")
    assert competitor._machine_code(call) == (None, None)
