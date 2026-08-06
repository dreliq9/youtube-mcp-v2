"""Process isolation for risky calls.

The MCP server must not crash or wedge when an upstream parser/tool segfaults or
hangs. Each isolated call owns one child process so a deadline can terminate the
actual running worker rather than merely cancelling a Future.

Pattern: pass a fully-qualified function name + JSON-serializable args.
"""

from __future__ import annotations

import importlib
import json
import multiprocessing as mp
from multiprocessing.connection import wait
from typing import Any

DEFAULT_TIMEOUT_S = 30
_KILL_GRACE_S = 1.0


def _runner(qualified_name: str, args_json: str, kwargs_json: str) -> str:
    """Resolve a fully-qualified function and return its JSON-encoded result."""
    module_name, _, fn_name = qualified_name.rpartition(".")
    if not module_name or not fn_name:
        raise ValueError(f"invalid qualified function name: {qualified_name!r}")
    module = importlib.import_module(module_name)
    fn = getattr(module, fn_name)
    args = json.loads(args_json)
    kwargs = json.loads(kwargs_json)
    result = fn(*args, **kwargs)
    return json.dumps(result)


def _process_entry(send_conn, qualified_name: str, args_json: str, kwargs_json: str) -> None:
    """Child entry point. Never lets a Python exception cross process boundaries."""
    try:
        result_json = _runner(qualified_name, args_json, kwargs_json)
        message = {"ok": True, "result_json": result_json}
    except BaseException as exc:
        message = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }

    try:
        send_conn.send(message)
    except (BrokenPipeError, EOFError, OSError):
        # Parent may have timed out and closed its end already.
        pass
    finally:
        send_conn.close()


def _terminate_process(process: mp.Process) -> None:
    """Best-effort terminate, escalate to kill, and reap the child."""
    if not process.is_alive():
        process.join(timeout=0)
        return

    process.terminate()
    process.join(timeout=_KILL_GRACE_S)
    if process.is_alive():
        kill = getattr(process, "kill", None)
        if kill is not None:
            kill()
            process.join(timeout=_KILL_GRACE_S)


def run_isolated(
    qualified_name: str,
    *args: Any,
    timeout_s: int | float = DEFAULT_TIMEOUT_S,
    **kwargs: Any,
) -> Any:
    """Run a function in a dedicated spawned worker with a hard deadline.

    Raises:
        TimeoutError: worker exceeded timeout_s and was terminated/killed.
        RuntimeError: worker raised, died, or returned non-JSON data.
        ValueError: timeout/arguments are not serializable or otherwise invalid.

    `spawn` is used on every OS so behavior is consistent on macOS, Windows, and
    Linux and child state is not inherited implicitly from the MCP process.
    """
    timeout = float(timeout_s)
    if timeout <= 0:
        raise ValueError("timeout_s must be > 0")

    # Serialize before spawning. Caller mistakes fail in-process and cannot leak
    # a child process.
    args_json = json.dumps(list(args))
    kwargs_json = json.dumps(kwargs)

    ctx = mp.get_context("spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_process_entry,
        args=(send_conn, qualified_name, args_json, kwargs_json),
        daemon=True,
    )

    try:
        process.start()
        # Parent never writes to the pipe; closing its copy is important so EOF
        # accurately reflects child death.
        send_conn.close()

        ready = wait([recv_conn, process.sentinel], timeout=timeout)

        if recv_conn in ready:
            try:
                message = recv_conn.recv()
            except EOFError:
                process.join(timeout=_KILL_GRACE_S)
                raise RuntimeError(
                    f"worker died without a result running {qualified_name} "
                    f"(exitcode={process.exitcode})"
                )

            process.join(timeout=_KILL_GRACE_S)
            if process.is_alive():
                # A worker that sent a result but refuses to exit still violates
                # the isolation contract. Reap it before returning/raising.
                _terminate_process(process)

            if not isinstance(message, dict) or "ok" not in message:
                raise RuntimeError(
                    f"worker returned malformed control data running {qualified_name}"
                )
            if not message["ok"]:
                raise RuntimeError(
                    f"worker failed running {qualified_name}: "
                    f"{message.get('error_type', 'Exception')}: "
                    f"{message.get('error_message', '')}"
                )

            try:
                return json.loads(message["result_json"])
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"worker returned invalid JSON running {qualified_name}: {exc}"
                ) from exc

        if process.sentinel in ready:
            process.join(timeout=_KILL_GRACE_S)
            raise RuntimeError(
                f"worker exited without a result running {qualified_name} "
                f"(exitcode={process.exitcode})"
            )

        # Nothing became ready by the deadline. This path owns the process and
        # actually stops it; cancelling a Future would not be sufficient.
        _terminate_process(process)
        raise TimeoutError(
            f"worker exceeded {timeout:g}s running {qualified_name}"
        )
    finally:
        recv_conn.close()
        try:
            send_conn.close()
        except OSError:
            pass
        if process.is_alive():
            _terminate_process(process)
