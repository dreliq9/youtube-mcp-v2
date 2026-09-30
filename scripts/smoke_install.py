#!/usr/bin/env python3
"""Clean-install / first-launch smoke test for a built youtube-mcp-v2.

Run with the Python of the environment where the wheel is installed:

    python scripts/smoke_install.py                          # `youtube-mcp-v2` on PATH
    python scripts/smoke_install.py --command uvx --from dist/youtube_mcp_v2-X-py3-none-any.whl youtube-mcp-v2

Checks (localhost only; never contacts YouTube):
  1. `<command> --version` matches the installed package version,
  2. `<command> doctor --json` exits 0 and emits a report,
  3. stdio: initialize + tools/list through the MCP SDK client,
  4. Streamable HTTP: /healthz, 401 without a token, and tools/list for both a
     modern (2026-07-28) and a handshake-era (legacy) client with a bearer token.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import anyio

EXPECTED_MIN_TOOLS = 15


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300, check=False)


async def _stdio(cmd: list[str], env: dict[str, str]) -> int:
    from mcp import Client, StdioServerParameters

    params = StdioServerParameters(command=cmd[0], args=[*cmd[1:], "serve"], env=env)
    async with Client(params) as client:
        tools = await client.list_tools()
    return len(tools.tools)


async def _http(url: str, token: str, mode: str) -> int:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
        async with Client(streamable_http_client(url, http_client=http), mode=mode) as client:
            tools = await client.list_tools()
    return len(tools.tools)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", nargs=argparse.REMAINDER, default=["youtube-mcp-v2"])
    parser.add_argument("--expect-version", default=None)
    args = parser.parse_args()
    cmd = args.command or ["youtube-mcp-v2"]

    cache = tempfile.mkdtemp(prefix="ytmcp-smoke-")
    env = {**os.environ, "XDG_CACHE_HOME": cache, "PYTHONUTF8": "1"}
    env.pop("YOUTUBE_API_KEY", None)

    if args.expect_version is None:
        from importlib.metadata import version

        try:
            args.expect_version = version("youtube-mcp-v2")
        except Exception:
            args.expect_version = ""

    out = _run([*cmd, "--version"], env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith(args.expect_version), out.stdout
    print(f"ok  --version: {out.stdout.strip()}")

    out = _run([*cmd, "doctor", "--json"], env)
    assert out.returncode == 0, out.stdout + out.stderr
    report = json.loads(out.stdout)
    assert report["status"] == "ok", report
    print(f"ok  doctor: {report['summary']}")

    count = anyio.run(_stdio, cmd, env)
    assert count >= EXPECTED_MIN_TOOLS, count
    print(f"ok  stdio first launch: {count} tools")

    token = secrets.token_urlsafe(24)
    port = _free_port()
    http_env = {**env, "YOUTUBE_MCP_HTTP_TOKEN": token}
    proc = subprocess.Popen(
        [*cmd, "serve", "--transport", "streamable-http", "--port", str(port)],
        env=http_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 120
        while True:
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=2) as resp:
                    assert json.loads(resp.read()) == {"status": "ok"}
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise SystemExit("HTTP server did not start:\n" + (proc.stdout.read() if proc.stdout else ""))
                time.sleep(0.5)
        print("ok  http /healthz")

        request = urllib.request.Request(base + "/mcp", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(request, timeout=5)
            raise SystemExit("unauthenticated request was not rejected")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc.code
        print("ok  http rejects missing bearer token (401)")

        for mode in ("2026-07-28", "legacy"):
            count = anyio.run(_http, base + "/mcp", token, mode)
            assert count >= EXPECTED_MIN_TOOLS, count
            print(f"ok  http {mode} client: {count} tools")
    finally:
        proc.terminate()
        try:
            output, _ = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
    if token in (output or ""):
        raise SystemExit("bearer token leaked into server output")
    print("ok  server output contains no token")
    return 0


if __name__ == "__main__":
    sys.exit(main())
