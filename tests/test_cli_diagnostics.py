"""doctor / config check / cache status: behavior and the no-secrets guarantee."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from youtube_mcp_v2 import cache_status, cli, config_check, doctor, paths, redaction

CANARIES = {
    "YOUTUBE_API_KEY": "CANARY-api-key-7f3a9b2c1d",
    "YOUTUBE_TRANSCRIPT_PROXY": "http://CANARYuser:CANARYpass@proxy.invalid:8080",
    "YOUTUBE_YTDLP_PROXY": "socks5://CANARYsock:CANARYsockpw@socks.invalid:1080",
    "YOUTUBE_MCP_HTTP_TOKEN": "CANARY-http-token-0123456789abcdef",
}
CANARY_FRAGMENTS = ("CANARY",)


def _set_canaries(monkeypatch, tmp_path: Path) -> dict[str, str]:
    for key in list(os.environ):
        if key.startswith("YOUTUBE_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in CANARIES.items():
        monkeypatch.setenv(key, value)
    cookies = tmp_path / "CANARY-cookies-dir" / "cookies.txt"
    cookies.parent.mkdir()
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(cookies))
    token_file = tmp_path / "CANARY-token-dir" / "tokens"
    token_file.parent.mkdir()
    token_file.write_text("CANARY-second-token-abcdef012345\n")
    monkeypatch.setenv("YOUTUBE_MCP_HTTP_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    return {**CANARIES, "YOUTUBE_COOKIES_FILE": str(cookies)}


def _assert_clean(text: str) -> None:
    for fragment in CANARY_FRAGMENTS:
        assert fragment not in text, f"secret material leaked: {text[:2000]}"


@pytest.mark.parametrize("argv", [["doctor"], ["doctor", "--json"], ["config", "check"], ["config", "check", "--json"], ["cache", "status"], ["cache", "status", "--json"]])
def test_diagnostics_never_print_configured_secrets(argv, monkeypatch, tmp_path, capsys) -> None:
    _set_canaries(monkeypatch, tmp_path)
    monkeypatch.setattr(paths, "CACHE_DIR", tmp_path / "xdg" / "youtube-mcp")
    cli.main(argv)
    captured = capsys.readouterr()
    _assert_clean(captured.out)
    _assert_clean(captured.err)
    if "--json" in argv:
        json.loads(captured.out)


def test_diagnostics_subprocess_never_prints_secrets(monkeypatch, tmp_path) -> None:
    """Real entry point, including logging to stderr and server import."""
    _set_canaries(monkeypatch, tmp_path)
    for argv in (["doctor", "--json"], ["config", "check"], ["cache", "status", "--json"]):
        proc = subprocess.run(
            [sys.executable, "-m", "youtube_mcp_v2", *argv],
            capture_output=True,
            text=True,
            env=dict(os.environ),
            timeout=120,
            check=False,
        )
        _assert_clean(proc.stdout)
        _assert_clean(proc.stderr)


def test_config_check_reports_presence_without_values(monkeypatch, tmp_path) -> None:
    _set_canaries(monkeypatch, tmp_path)
    report = config_check.run_config_check()
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["config:youtube_api_key"]["detail"]["configured"] is True
    assert by_name["config:transcript_proxy"]["detail"] == {"mode": "generic", "http_configured": True, "https_configured": True}
    assert by_name["config:ytdlp_network"]["detail"] == {"proxy_configured": True, "cookies": "file", "cookies_file_exists": True}
    assert by_name["config:http"]["detail"]["token_count"] == 2
    _assert_clean(json.dumps(report))


def test_config_check_fails_on_conflicting_cookie_modes(monkeypatch, tmp_path) -> None:
    _set_canaries(monkeypatch, tmp_path)
    monkeypatch.setenv("YOUTUBE_COOKIES_FROM_BROWSER", "firefox")
    report = config_check.run_config_check()
    assert report["status"] == "fail"
    assert cli.main(["config", "check"]) == 1


def test_config_check_fails_on_partial_webshare_and_short_token(monkeypatch, tmp_path) -> None:
    for key in list(os.environ):
        if key.startswith("YOUTUBE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME", "CANARYwsuser")
    monkeypatch.setenv("YOUTUBE_MCP_HTTP_TOKEN", "CANARYshort")
    report = config_check.run_config_check()
    failed = {c["name"] for c in report["checks"] if c["status"] == "fail"}
    assert failed == {"config:transcript_proxy", "config:http"}
    _assert_clean(json.dumps(report))


def test_config_check_warns_on_unknown_vars_and_bad_limits(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("YOUTUBE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("YOUTUBE_API_KYE", "CANARY-typo-value")
    monkeypatch.setenv("YOUTUBE_MCP_MAX_RESOURCE_BYTES", "lots")
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")
    report = config_check.run_config_check()
    warned = {c["name"]: c for c in report["checks"] if c["status"] == "warn"}
    assert warned["config:unrecognized"]["detail"] == {"names": ["YOUTUBE_API_KYE"]}
    assert "config:YOUTUBE_MCP_MAX_RESOURCE_BYTES" in warned
    assert "config:XDG_CACHE_HOME" in warned
    assert report["status"] == "ok"
    _assert_clean(json.dumps(report))


def test_known_env_vars_cover_everything_the_package_reads() -> None:
    import re

    root = Path(__file__).resolve().parents[1] / "youtube_mcp_v2"
    names = set()
    for path in root.rglob("*.py"):
        names.update(re.findall(r"\"((?:YOUTUBE)_[A-Z0-9_]+)\"", path.read_text(encoding="utf-8")))
    missing = sorted(n for n in names if n not in config_check.KNOWN_ENV_VARS)
    assert missing == []


def test_doctor_reports_backends_and_versions(monkeypatch, tmp_path) -> None:
    for key in list(os.environ):
        if key.startswith("YOUTUBE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(doctor, "_cache_check", lambda: doctor.Check("cache", "pass", "cache ok"))
    monkeypatch.setattr(doctor, "_server_import_check", lambda: doctor.Check("mcp_server", "pass", "server ok"))
    report = doctor.run_doctor()
    names = {c["name"] for c in report["checks"]}
    assert {"package:mcp", "package:yt-dlp", "package:fastembed", "binary:ffmpeg", "binary:yt-dlp"} <= names
    assert {"config:transcript_proxy", "config:ytdlp_network", "config:local_stt", "config:semantic", "disk"} <= names
    optional = {c["name"]: c for c in report["checks"]}["package:google-api-python-client"]
    assert optional["status"] in {"pass", "info"}


def test_doctor_fails_when_a_backend_is_misconfigured(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(tmp_path / "missing-cookies.txt"))
    monkeypatch.setattr(doctor, "_server_import_check", lambda: doctor.Check("mcp_server", "pass", "server ok"))
    report = doctor.run_doctor()
    assert report["status"] == "fail"
    assert str(tmp_path) not in json.dumps(report)


def test_binary_version_is_local_and_bounded(monkeypatch) -> None:
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs["timeout"]))
        return subprocess.CompletedProcess(cmd, 0, stdout="ffmpeg version 7.1 Copyright (c) 2000 the FFmpeg developers\nmore", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    assert doctor._binary_version("/usr/bin/ffmpeg", "-version") == "ffmpeg version 7.1"
    assert calls == [(["/usr/bin/ffmpeg", "-version"], 10)]


def _seed_cache(root: Path) -> None:
    (root / "skeletons").mkdir(parents=True)
    (root / "skeletons" / "a.json").write_bytes(b"x" * 100)
    (root / "frames" / "vid").mkdir(parents=True)
    (root / "frames" / "vid" / "f.png").write_bytes(b"y" * 50)
    conn = sqlite3.connect(root / "v2.sqlite")
    conn.execute("CREATE TABLE video_meta (id INTEGER PRIMARY KEY, video_id TEXT)")
    conn.executemany("INSERT INTO video_meta (video_id) VALUES (?)", [("a",), ("b",)])
    conn.commit()
    conn.close()


def test_cache_status_reports_usage_rows_and_quota(monkeypatch, tmp_path) -> None:
    root = tmp_path / "youtube-mcp"
    _seed_cache(root)
    monkeypatch.setenv("YOUTUBE_MCP_CACHE_QUOTA_BYTES", "120")
    report = cache_status.run_cache_status(root)
    assert report["areas"]["skeletons"] == {"bytes": 100, "files": 1, "description": "frozen corpus revisions"}
    assert report["areas"]["frames"]["bytes"] == 50
    assert report["sqlite"]["tables"] == {"video_meta": 2}
    assert report["sqlite"]["quick_check"] == "ok"
    assert report["quota"]["quota_bytes"] == 120
    assert {c["name"]: c["status"] for c in report["checks"]}["cache:quota"] == "warn"
    assert "quota" in cache_status.render_text(report)


def test_cache_status_is_read_only_on_missing_cache(tmp_path) -> None:
    root = tmp_path / "never-created"
    report = cache_status.run_cache_status(root)
    assert report["status"] == "ok"
    assert report["total_bytes"] == 0
    assert not root.exists()


def test_cache_status_flags_corrupt_sqlite(tmp_path) -> None:
    root = tmp_path / "youtube-mcp"
    root.mkdir()
    (root / "v2.sqlite").write_bytes(b"this is not a database" * 100)
    report = cache_status.run_cache_status(root)
    assert report["status"] == "fail"
    assert cli._write_report(report, as_json=True, title="cache status") == 1


def test_redaction_handles_nested_values_and_url_userinfo(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", "CANARY-nested-key")
    value = {"a": ["x CANARY-nested-key y", {"CANARY-nested-key": 1}], "u": "see https://bob:hunter2@host/x"}
    out = redaction.redact(value)
    assert "CANARY" not in json.dumps(out)
    assert "hunter2" not in json.dumps(out)
    monkeypatch.setenv("YOUTUBE_MCP_HTTP_TOKEN", "1")  # too short to redact meaningfully
    assert redaction.redact_text("1 2 3") == "1 2 3"


def test_version_flag(capsys) -> None:
    from youtube_mcp_v2 import __version__

    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"youtube-mcp-v2 {__version__}"


def test_serve_http_refuses_public_bind_without_token(monkeypatch, capsys) -> None:
    monkeypatch.delenv("YOUTUBE_MCP_HTTP_TOKEN", raising=False)
    monkeypatch.delenv("YOUTUBE_MCP_HTTP_TOKEN_FILE", raising=False)
    assert cli.main(["serve", "--transport", "streamable-http", "--host", "0.0.0.0"]) == 2
    assert "refusing to serve" in capsys.readouterr().err
