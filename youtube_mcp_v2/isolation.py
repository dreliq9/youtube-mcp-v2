"""Subprocess isolation for risky calls (yt-dlp, ffmpeg, page scrapes).

The MCP server must not crash when an upstream tool segfaults or hangs.
Run risky work in a worker process with a hard timeout.

For pure-Python paths that have their own timeouts (httpx, youtube-transcript-api),
in-process is fine — those can't take the server down. We only subprocess when:
  - calling a C-extension or external binary that might segfault
  - parsing untrusted HTML that could trigger pathological regex
  - any code path with unbounded latency risk

Pattern: pass a fully-qualified function name + JSON-serializable args.
"""

from __future__ import annotations

import importlib
import json
from concurrent.futures import ProcessPoolExecutor, TimeoutError as PoolTimeout
from typing import Any

DEFAULT_TIMEOUT_S = 30


def _runner(qualified_name: str, args_json: str, kwargs_json: str) -> str:
    """Resolve a fully-qualified function and run it. Returns JSON-encoded result."""
    module_name, _, fn_name = qualified_name.rpartition(".")
    module = importlib.import_module(module_name)
    fn = getattr(module, fn_name)
    args = json.loads(args_json)
    kwargs = json.loads(kwargs_json)
    result = fn(*args, **kwargs)
    return json.dumps(result)


def run_isolated(
    qualified_name: str,
    *args: Any,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    **kwargs: Any,
) -> Any:
    """Run a function in a worker process with a hard timeout.

    Raises:
        TimeoutError: worker exceeded timeout_s.
        RuntimeError: worker died (segfault, OOM, etc.) or returned bad data.
    """
    args_json = json.dumps(list(args))
    kwargs_json = json.dumps(kwargs)
    with ProcessPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_runner, qualified_name, args_json, kwargs_json)
        try:
            result_json = future.result(timeout=timeout_s)
        except PoolTimeout:
            # Force the pool down — hung worker would otherwise leak.
            pool.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(
                f"worker exceeded {timeout_s}s running {qualified_name}"
            )
        except Exception as e:
            # Subprocess raised, segfaulted, or returned non-JSON.
            raise RuntimeError(f"worker failed running {qualified_name}: {e}") from e
    return json.loads(result_json)
