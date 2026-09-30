"""Streamable HTTP boundary + remote-safe artifacts. In-process ASGI; no sockets, no network."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import BlobResourceContents

from youtube_mcp_v2 import artifacts, envelope, http_transport, remote
from youtube_mcp_v2 import server as server_module
from youtube_mcp_v2.adapters import frame_extract
from youtube_mcp_v2.server import mcp

TOKEN = "unit-test-token-abcdefghijklmnop"
VIDEO_ID = "jNQXAC9IVRw"
BASE = "http://127.0.0.1:8000"


def _settings(**overrides) -> http_transport.HttpSettings:
    settings = http_transport.HttpSettings(token_digests=[hashlib.sha256(TOKEN.encode()).hexdigest()])
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


@asynccontextmanager
async def _serve(settings: http_transport.HttpSettings):
    app = http_transport.build_app(settings, server=mcp)
    inner = app.app
    async with inner.router.lifespan_context(inner):
        yield app


def _http(app, token: str | None = TOKEN) -> httpx2.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE, headers=headers)


def _frame(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "frames"
    monkeypatch.setattr(artifacts, "FRAMES_DIR", root)
    path = root / VIDEO_ID / "single_1000ms_640x360.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\nremote-fixture")
    monkeypatch.setattr(
        frame_extract,
        "extract_single_frame",
        lambda *_a, **_k: frame_extract.SingleFrameResult(path=str(path), timestamp_s=1.0, cached=False),
    )
    return path


def _payload(result) -> dict:
    if result.structured_content:
        data = result.structured_content
        return data.get("result", data) if isinstance(data, dict) else data
    return json.loads(result.content[0].text)


# ---------------------------------------------------------------------------
# Boundary: health, auth, rate limit, settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_is_open_and_mcp_requires_bearer_token() -> None:
    async with _serve(_settings()) as app:
        async with _http(app, token=None) as http:
            health = await http.get("/healthz")
            assert health.status_code == 200 and health.json() == {"status": "ok"}
            missing = await http.post("/mcp", json={})
            assert missing.status_code == 401
            assert missing.headers["www-authenticate"].startswith("Bearer")
        async with _http(app, token="wrong-token-wrong-token") as http:
            assert (await http.post("/mcp", json={})).status_code == 401
        async with _http(app) as http:
            response = await http.post("/mcp", headers={"Authorization": f"Basic {TOKEN}"}, json={})
            assert response.status_code == 401


def test_rate_limit_bucket_refills_with_clock() -> None:
    now = [0.0]
    bucket = http_transport.TokenBucket(rate_per_min=60, burst=2, clock=lambda: now[0])
    assert bucket.take("a") == 0 and bucket.take("a") == 0
    assert bucket.take("a") == pytest.approx(1.0)
    assert bucket.take("b") == 0  # independent principals
    now[0] = 1.0
    assert bucket.take("a") == 0


@pytest.mark.asyncio
async def test_rate_limited_requests_get_429_with_retry_after() -> None:
    settings = _settings(rate_per_min=1, burst=1)
    async with _serve(settings) as app:
        async with _http(app) as http:
            first = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert first.status_code != 429
            second = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "ping"})
            assert second.status_code == 429
            assert int(second.headers["retry-after"]) >= 1
            # Health checks are never rate limited.
            assert (await http.get("/healthz")).status_code == 200


def test_token_file_accepts_plaintext_and_sha256_lines(tmp_path) -> None:
    other = "second-token-0123456789abcdef"
    path = tmp_path / "tokens"
    path.write_text(f"# comment\n{TOKEN}\nsha256:{hashlib.sha256(other.encode()).hexdigest()}\n\n")
    digests = http_transport.load_token_digests({"YOUTUBE_MCP_HTTP_TOKEN_FILE": str(path)})
    assert digests == [hashlib.sha256(TOKEN.encode()).hexdigest(), hashlib.sha256(other.encode()).hexdigest()]
    path.write_text("sha256:nothex\n")
    with pytest.raises(http_transport.HttpConfigError, match="line 1"):
        http_transport.load_token_digests({"YOUTUBE_MCP_HTTP_TOKEN_FILE": str(path)})
    with pytest.raises(http_transport.HttpConfigError) as exc:
        http_transport.load_token_digests({"YOUTUBE_MCP_HTTP_TOKEN": "short-secret"})
    assert "short-secret" not in str(exc.value)


def test_settings_refuse_public_unauthenticated_bind_and_never_expose_tokens() -> None:
    with pytest.raises(http_transport.HttpConfigError, match="non-loopback"):
        http_transport.HttpSettings(host="0.0.0.0").validate()
    warnings = http_transport.HttpSettings(host="0.0.0.0", allow_unauthenticated=True).validate()
    assert any("authentication is disabled" in w for w in warnings)
    assert any("DNS-rebinding" in w for w in warnings)
    assert http_transport.HttpSettings(host="127.0.0.1").validate() == ["HTTP authentication is disabled (no bearer tokens configured)"]
    assert http_transport.HttpSettings(host="::1").validate()
    summary = _settings().summary()
    assert summary["auth"] == "bearer" and summary["token_count"] == 1
    assert TOKEN not in json.dumps(summary)
    assert hashlib.sha256(TOKEN.encode()).hexdigest() not in json.dumps(summary)


def test_http_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_MCP_HTTP_TOKEN", f"{TOKEN}, another-token-0123456789")
    monkeypatch.setenv("YOUTUBE_MCP_HTTP_RATE_LIMIT_PER_MIN", "10")
    monkeypatch.setenv("YOUTUBE_MCP_HTTP_ALLOWED_HOSTS", "mcp.example.com, mcp2.example.com")
    settings = http_transport.HttpSettings.from_env(port=9000)
    assert len(settings.token_digests) == 2
    assert settings.rate_per_min == 10
    assert settings.port == 9000
    assert settings.allowed_hosts == ["mcp.example.com", "mcp2.example.com"]
    monkeypatch.setenv("YOUTUBE_MCP_HTTP_RATE_BURST", "zero")
    with pytest.raises(http_transport.HttpConfigError, match="RATE_BURST"):
        http_transport.HttpSettings.from_env()


@pytest.mark.asyncio
async def test_allowed_hosts_enable_dns_rebinding_protection() -> None:
    settings = _settings(host="0.0.0.0", allowed_hosts=["mcp.example.com"])
    async with _serve(settings) as app:
        async with _http(app) as http:
            request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
            headers = {"accept": "application/json, text/event-stream", "host": "evil.example.net"}
            assert (await http.post("/mcp", json=request, headers=headers)).status_code in (400, 421)
            headers["host"] = "mcp.example.com"
            assert (await http.post("/mcp", json=request, headers=headers)).status_code == 200


# ---------------------------------------------------------------------------
# Protocol eras: modern stateless and handshake-era clients
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [True, False])
@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_sdk_clients_list_tools_over_http(stateless: bool, mode: str) -> None:
    async with _serve(_settings(stateless=stateless)) as app:
        async with _http(app) as http:
            async with Client(streamable_http_client(f"{BASE}/mcp", http_client=http), mode=mode) as client:
                tools = await client.list_tools()
    names = {tool.name for tool in tools.tools}
    assert {"inspect.video", "frame.get", "corpus.search"} <= names


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["2025-03-26", "2025-06-18", "2025-11-25"])
async def test_handshake_era_initialize_negotiates_requested_version(version: str) -> None:
    async with _serve(_settings()) as app:
        async with _http(app) as http:
            response = await http.post(
                "/mcp",
                headers={"accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "old", "version": "1"}},
                },
            )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == version
    assert result["serverInfo"]["version"]


@pytest.mark.asyncio
async def test_stateful_mode_issues_session_ids() -> None:
    async with _serve(_settings(stateless=False)) as app:
        async with _http(app) as http:
            response = await http.post(
                "/mcp",
                headers={"accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "old", "version": "1"}}},
            )
    assert response.status_code == 200
    assert response.headers.get("mcp-session-id")


# ---------------------------------------------------------------------------
# Remote-safe results and artifact retrieval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_remote_client_retrieves_frame_without_server_filesystem(tmp_path, monkeypatch, mode) -> None:
    path = _frame(tmp_path, monkeypatch)
    expected = path.read_bytes()
    monkeypatch.setenv(remote.REMOTE_ENV, "1")
    async with _serve(_settings()) as app:
        async with _http(app) as http:
            async with Client(streamable_http_client(f"{BASE}/mcp", http_client=http), mode=mode) as client:
                result = await client.call_tool("frame.get", {"url_or_id": VIDEO_ID, "timestamp_s": 1.0, "size": "640x360"})
                data = _payload(result)
                raw = json.dumps(data)
                assert str(tmp_path) not in raw
                assert "path" not in data["data"]
                assert remote.REMOTE_WARNING in data["warnings"]
                uri = data["data"]["resource_uri"]
                assert uri == f"youtube-mcp://artifact/frame/png/{VIDEO_ID}/single_1000ms_640x360"
                read = await client.read_resource(uri)
    content = read.contents[0]
    assert isinstance(content, BlobResourceContents)
    blob = base64.b64decode(content.blob)
    assert blob == expected
    assert hashlib.sha256(blob).hexdigest() == data["data"]["resource_sha256"]


@pytest.mark.asyncio
async def test_remote_client_cannot_read_oversized_or_traversal_artifacts(tmp_path, monkeypatch) -> None:
    _frame(tmp_path, monkeypatch)
    monkeypatch.setenv("YOUTUBE_MCP_MAX_RESOURCE_BYTES", "4")
    async with _serve(_settings()) as app:
        async with _http(app) as http:
            async with Client(streamable_http_client(f"{BASE}/mcp", http_client=http), mode="2026-07-28") as client:
                with pytest.raises(Exception, match="limit"):
                    await client.read_resource(f"youtube-mcp://artifact/frame/png/{VIDEO_ID}/single_1000ms_640x360")
                with pytest.raises(Exception):
                    await client.read_resource(f"youtube-mcp://artifact/frame/png/{VIDEO_ID}/..%2F..%2Fsecret")


@pytest.mark.asyncio
async def test_tool_responses_never_emit_configured_secrets(monkeypatch) -> None:
    canary = "CANARY-api-key-http-0123456789"
    monkeypatch.setenv("YOUTUBE_API_KEY", canary)
    monkeypatch.setattr(
        server_module,
        "_inspect_video",
        lambda url_or_id: envelope.fail("upstream_error", f"request https://x/?key={canary} failed"),
    )
    async with _serve(_settings()) as app:
        async with _http(app) as http:
            async with Client(streamable_http_client(f"{BASE}/mcp", http_client=http), mode="2026-07-28") as client:
                result = await client.call_tool("inspect.video", {"url_or_id": VIDEO_ID})
    text = json.dumps(_payload(result))
    assert canary not in text
    assert "<redacted>" in text


@pytest.mark.asyncio
async def test_uncaught_tool_exception_text_is_scrubbed(monkeypatch, tmp_path) -> None:
    canary = "CANARY-api-key-exc-0123456789"
    monkeypatch.setenv("YOUTUBE_API_KEY", canary)
    monkeypatch.setenv(remote.REMOTE_ENV, "1")

    def boom(url_or_id):
        raise RuntimeError(f"failed with {canary} at {Path.home() / 'secret' / 'file.txt'}")

    monkeypatch.setattr(server_module, "_inspect_video", boom)
    async with _serve(_settings()) as app:
        async with _http(app) as http:
            async with Client(streamable_http_client(f"{BASE}/mcp", http_client=http), mode="2026-07-28") as client:
                result = await client.call_tool("inspect.video", {"url_or_id": VIDEO_ID})
    text = json.dumps([c.model_dump() for c in result.content])
    assert result.is_error
    assert canary not in text
    assert str(Path.home()) not in text


def test_strip_local_paths_keeps_uris_and_removes_paths(tmp_path) -> None:
    roots = [str(tmp_path)]
    value = {
        "data": {
            "path": str(tmp_path / "a.png"),
            "frame_paths": [str(tmp_path / "b.png")],
            "plan_path": None,
            "resource_uri": "youtube-mcp://artifact/frame/png/x/y",
            "note": f"wrote {tmp_path / 'c.json'} ok",
            "url": "https://www.youtube.com/watch?v=abc",
            "profile": "kept",
        }
    }
    cleaned, removed = remote.strip_local_paths(value, roots)
    data = cleaned["data"]
    assert "path" not in data and "frame_paths" not in data
    assert data["plan_path"] is None  # nothing to withhold
    assert data["resource_uri"].startswith("youtube-mcp://")
    assert data["url"].startswith("https://")
    assert data["profile"] == "kept"
    assert str(tmp_path) not in data["note"] and remote.WITHHELD in data["note"]
    assert removed == 3


def test_local_mode_keeps_paths_for_same_host_clients(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(remote.REMOTE_ENV, raising=False)
    value = {"data": {"path": str(tmp_path / "a.png")}, "warnings": []}
    assert remote.finalize_tool_result(value) == value


def test_remote_mode_does_not_register_local_only_tools(tmp_path) -> None:
    code = (
        "import asyncio, json\n"
        "from youtube_mcp_v2.server import mcp\n"
        "print(json.dumps(sorted(t.name for t in asyncio.run(mcp.list_tools()))))\n"
    )
    env = {**os.environ, remote.REMOTE_ENV: "1", "XDG_CACHE_HOME": str(tmp_path)}
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120, check=True)
    names = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "media.materialize" not in names
    assert "frame.get" in names
