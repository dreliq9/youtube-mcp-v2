from __future__ import annotations

import json

from youtube_mcp_v2 import cli, doctor


def test_cache_check_verifies_real_writability_without_leaving_probe(tmp_path) -> None:
    check = doctor._cache_check(tmp_path / "cache")
    assert check.status == "pass"
    assert (tmp_path / "cache").is_dir()
    assert list((tmp_path / "cache").glob("doctor-*.tmp")) == []


def test_api_key_check_reports_presence_without_value(monkeypatch) -> None:
    secret = "super-secret-api-key-value"
    monkeypatch.setenv("YOUTUBE_API_KEY", secret)
    check = doctor._api_key_check()
    assert check.detail == {"configured": True}
    assert secret not in check.message
    assert secret not in json.dumps(check.as_dict())


def test_doctor_renderers_do_not_emit_configured_api_key(monkeypatch) -> None:
    secret = "do-not-print-this-key"
    monkeypatch.setenv("YOUTUBE_API_KEY", secret)

    monkeypatch.setattr(doctor, "_python_check", lambda: doctor.Check("python", "pass", "python ok"))
    monkeypatch.setattr(
        doctor,
        "_package_check",
        lambda name, **_kwargs: doctor.Check(f"package:{name}", "pass", f"{name} ok"),
    )
    monkeypatch.setattr(
        doctor,
        "_binary_check",
        lambda name, **_kwargs: doctor.Check(f"binary:{name}", "pass", f"{name} ok"),
    )
    monkeypatch.setattr(doctor, "_cache_check", lambda: doctor.Check("cache", "pass", "cache ok"))
    monkeypatch.setattr(
        doctor,
        "_server_import_check",
        lambda: doctor.Check("mcp_server", "pass", "server ok"),
    )

    report = doctor.run_doctor()
    assert report["status"] == "ok"
    assert secret not in doctor.render_text(report)
    assert secret not in doctor.render_json(report)


def test_doctor_failure_drives_nonzero_cli_exit(monkeypatch, capsys) -> None:
    report = {
        "youtube_mcp_version": "test",
        "status": "fail",
        "platform": "test",
        "python": "3.x",
        "summary": {"failures": 1, "warnings": 0},
        "checks": [
            {
                "name": "cache",
                "status": "fail",
                "message": "cache failed",
                "detail": None,
            }
        ],
    }
    monkeypatch.setattr(doctor, "run_doctor", lambda: report)

    # cli imports the functions lazily from the same module object.
    assert cli.main(["doctor", "--json"]) == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == "fail"


def test_no_argument_cli_still_dispatches_to_server(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_serve", lambda: 23)
    assert cli.main([]) == 23


def test_explicit_serve_dispatches_to_server(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_serve", lambda: 24)
    assert cli.main(["serve"]) == 24


def test_doctor_json_dispatches_machine_readable_mode(monkeypatch) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(cli, "_doctor", lambda *, as_json: seen.append(as_json) or 0)
    assert cli.main(["doctor", "--json"]) == 0
    assert seen == [True]
