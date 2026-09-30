"""Minimal synchronous MCP stdio client used by benchmark adapters.

Adapters measure servers through the same public transport an MCP client uses:
spawn the server command, `initialize`, `tools/list`, and `tools/call`. Each
session runs inside one `anyio.run` so async context managers enter and exit
in the same task.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anyio

DEFAULT_SESSION_TIMEOUT_S = 120.0

# Environment variables a child process genuinely needs to start. Everything
# else is dropped so a developer's real credentials never reach a server under
# test unless a case injects them deliberately (for example leak canaries).
_BASE_ENV_KEYS = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "APPDATA",
    "LOCALAPPDATA",
    "UV_CACHE_DIR",
    "UV_PYTHON",
    "VIRTUAL_ENV",
)


def base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _BASE_ENV_KEYS}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update(extra)
    return env


@dataclass
class ServerCommand:
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None

    def display(self) -> str:
        return " ".join([self.command, *self.args])


@dataclass
class ToolCall:
    """Transport-neutral view of a tools/call result."""

    is_error: bool
    structured: Any
    text: str

    def payload(self) -> Any:
        """Structured content if present, else JSON-decoded text, else text."""
        if self.structured is not None:
            return self.structured
        try:
            return json.loads(self.text)
        except (TypeError, ValueError):
            return self.text


def _to_toolcall(result: Any) -> ToolCall:
    texts = [
        getattr(item, "text", "")
        for item in (getattr(result, "content", None) or [])
        if getattr(item, "type", None) == "text"
    ]
    return ToolCall(
        is_error=bool(getattr(result, "is_error", False)),
        structured=getattr(result, "structured_content", None),
        text="\n".join(texts),
    )


class Session:
    """Thin wrapper passed to session scripts."""

    def __init__(self, client: Any, init_ms: float, init_result: Any) -> None:
        self._client = client
        self.init_ms = init_ms
        self.init_result = init_result

    @property
    def server_name(self) -> str | None:
        info = getattr(self.init_result, "server_info", None)
        return getattr(info, "name", None)

    @property
    def server_version(self) -> str | None:
        info = getattr(self.init_result, "server_info", None)
        return getattr(info, "version", None) or None

    @property
    def protocol_version(self) -> str | None:
        return getattr(self.init_result, "protocol_version", None)

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(50):  # bounded pagination
            result = (
                await self._client.list_tools(cursor=cursor)
                if cursor
                else await self._client.list_tools()
            )
            for tool in result.tools:
                tools.append(
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.input_schema,
                    }
                )
            cursor = getattr(result, "next_cursor", None)
            if not cursor:
                break
        return tools

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolCall:
        return _to_toolcall(await self._client.call_tool(name, arguments))


def run_session(
    server: ServerCommand,
    script: Callable[[Session], Awaitable[Any]],
    *,
    timeout_s: float = DEFAULT_SESSION_TIMEOUT_S,
) -> tuple[Any, str]:
    """Spawn `server`, run `script(session)`, and return (result, stderr_text).

    stderr is captured to a temporary file rather than inherited so benchmark
    output stays readable and can be scanned for secret leakage.
    """
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=server.command,
        args=list(server.args),
        env=server.env if server.env is not None else base_env(),
        cwd=server.cwd,
    )

    with tempfile.TemporaryFile("w+", encoding="utf-8") as errlog:

        async def main() -> Any:
            with anyio.fail_after(timeout_s):
                started = time.perf_counter()
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as client:
                        init = await client.initialize()
                        init_ms = (time.perf_counter() - started) * 1000.0
                        return await script(Session(client, init_ms, init))

        try:
            result = anyio.run(main)
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed silently
            if isinstance(exc, KeyboardInterrupt):
                raise
            errlog.seek(0)
            raise SessionError(_root_cause(exc), errlog.read()) from exc
        errlog.seek(0)
        return result, errlog.read()


class SessionError(RuntimeError):
    """The server could not be launched or the session failed."""

    def __init__(self, message: str, stderr: str) -> None:
        super().__init__(message)
        self.stderr = stderr


def _root_cause(exc: BaseException) -> str:
    """Flatten anyio/ExceptionGroup wrappers into one short description."""
    current: BaseException = exc
    for _ in range(10):
        nested = getattr(current, "exceptions", None)
        if nested:
            current = nested[0]
            continue
        break
    if isinstance(current, TimeoutError) or type(current).__name__ == "TimeoutError":
        return "session timed out"
    return f"{type(current).__name__}: {current}"[:300]


def python_module_command(module: str, *, env: dict[str, str] | None = None) -> ServerCommand:
    return ServerCommand(command=sys.executable, args=["-m", module], env=env)
