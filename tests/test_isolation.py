from __future__ import annotations

import multiprocessing as mp
import time

import pytest

from youtube_mcp_v2.isolation import run_isolated


def test_isolated_success_round_trips_json_data() -> None:
    payload = {"hello": ["world", 1], "ok": True}
    assert run_isolated("tests.isolation_fixtures.echo", payload, timeout_s=5) == payload


def test_isolated_python_exception_becomes_runtime_error() -> None:
    with pytest.raises(RuntimeError, match="fixture boom"):
        run_isolated("tests.isolation_fixtures.explode", timeout_s=5)


def test_non_json_worker_result_becomes_runtime_error() -> None:
    with pytest.raises(RuntimeError, match="worker failed"):
        run_isolated("tests.isolation_fixtures.return_non_json_value", timeout_s=5)


def test_timeout_terminates_and_reaps_worker() -> None:
    before = {child.pid for child in mp.active_children()}
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="worker exceeded"):
        run_isolated(
            "tests.isolation_fixtures.sleep_then_return",
            5.0,
            timeout_s=0.2,
        )

    elapsed = time.monotonic() - started
    # Allow generous spawn/CI overhead while still proving we did not wait for
    # the five-second worker body to finish naturally.
    assert elapsed < 4.0

    leaked = [
        child
        for child in mp.active_children()
        if child.pid not in before and child.is_alive()
    ]
    assert leaked == []


def test_non_positive_timeout_is_rejected_before_spawn() -> None:
    with pytest.raises(ValueError, match="timeout_s must be > 0"):
        run_isolated("tests.isolation_fixtures.echo", "x", timeout_s=0)


def test_unserializable_arguments_fail_before_spawn() -> None:
    with pytest.raises(TypeError):
        run_isolated("tests.isolation_fixtures.echo", {"bad": {1, 2}}, timeout_s=1)
