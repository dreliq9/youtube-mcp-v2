# CLI

```text
youtube-mcp-v2 [--version]
youtube-mcp-v2 serve [--transport stdio|streamable-http] [HTTP options]
youtube-mcp-v2 doctor [--json]
youtube-mcp-v2 config check [--json]
youtube-mcp-v2 cache status [--json]
```

`python -m youtube_mcp_v2 ...` is equivalent. Every diagnostic command is **offline** (no YouTube request) and read-only apart from doctor's cache-writability probe file. All output passes through the secret redactor (`youtube_mcp_v2/redaction.py`); tests plant canary values in every secret-bearing environment variable and assert none appear in any command's text or JSON output.

Exit status: `0` when the report is `ok` or `warn`, `1` when any check `fail`s, `2` for invalid `serve` configuration.

## Run the MCP server

Historical behavior remains the default — with no command, or with `serve`, the stdio MCP server runs:

```bash
youtube-mcp-v2
youtube-mcp-v2 serve
```

### Streamable HTTP

```bash
YOUTUBE_MCP_HTTP_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))") \
  youtube-mcp-v2 serve --transport streamable-http --host 0.0.0.0 --port 8000 --allowed-host mcp.example.com
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. A non-loopback bind **requires** a bearer token unless `--allow-unauthenticated` is given. |
| `--port` | `8000` | TCP port. |
| `--path` | `/mcp` | MCP endpoint path. `/healthz` is always served unauthenticated for liveness probes. |
| `--stateful` | off | Issue `Mcp-Session-Id` sessions with SSE streams. Default is stateless, which is what horizontally scaled deployments need. |
| `--json-response` / `--sse-response` | JSON when stateless, SSE when stateful | Response framing for POST requests. |
| `--allowed-host` | none (warning) | Public `Host` header value(s) to accept; enables the SDK's DNS-rebinding protection. Repeatable; also `YOUTUBE_MCP_HTTP_ALLOWED_HOSTS`. |
| `--allow-unauthenticated` | off | Permit a public bind without tokens — only behind an authenticating reverse proxy. |

Auth, rate limit, and body limit come from the environment (see `CONFIGURATION.md#remote-streamable-http`). Serving over HTTP always enables **remote mode** (`YOUTUBE_MCP_REMOTE=1`):

- tool results are scrubbed of server-local filesystem paths (path-like keys are dropped; strings under the cache, home, temp, or working directory are replaced with `<server-local path withheld>`) and the envelope's `warnings` list says so when anything was withheld,
- frames, audio, and visual evidence are delivered through bounded MCP resources: each result has `resource_uri`, `resource_mime_type`, `resource_size_bytes`, and `resource_sha256`, and the client reads the bytes with `resources/read`,
- `media.materialize` is not registered, because materialized editor clips exist only as server-local files and have no resource representation yet,
- uncaught tool exception text is scrubbed the same way (secrets redacted, local paths withheld) before it becomes a protocol error.

Secret redaction of tool responses is applied in local stdio mode too; only path withholding is remote-specific.

#### Protocol-era compatibility

| Client | Behavior |
| --- | --- |
| MCP 2026-07-28 (single-exchange, stateless) | Served on `--path` with the default stateless JSON mode. |
| Handshake-era Streamable HTTP clients (protocol 2025-03-26, 2025-06-18, 2025-11-25) | Served on the same endpoint. `initialize` negotiates the client's requested version; stateless mode works for request/response tools; use `--stateful` if a client depends on `Mcp-Session-Id` or server-initiated SSE streams. |
| 2024-11-05 HTTP+SSE transport (separate `/sse` + `/messages` endpoints) | **Not served.** These clients should use stdio (`uvx youtube-mcp-v2`) or a stdio↔HTTP bridge. |

All three supported rows are covered by `tests/test_http_transport.py` using the MCP SDK's own client over an in-process ASGI transport.

## `doctor`

```bash
youtube-mcp-v2 doctor [--json]
```

Checks:

- supported Python version,
- installed versions of required distributions (`mcp`, `youtube-transcript-api`, `yt-dlp`, `httpx`, `beautifulsoup4`, `python-dotenv`) and optional extras (`fastembed`, `google-api-python-client`),
- `yt-dlp`, `ffmpeg`, `ffprobe` on `PATH` with their local `--version` string,
- cache root writability and free space on the cache volume,
- whether a YouTube Data API key is configured (presence only),
- configured backends: transcript proxy mode, yt-dlp proxy/cookie mode, local whisper.cpp STT model/binary, semantic/visual embedding backend and model names,
- MCP server import/initialization.

Missing media binaries and optional extras are warnings/info because metadata, search, and transcript functionality still work. Required package, Python, server-import, cache, or invalid proxy/cookie configuration failures produce exit status 1.

## `config check`

```bash
youtube-mcp-v2 config check [--json]
```

Validates the environment without touching the network: cache root (XDG), API key presence vs the `api` extra, transcript proxy configuration (generic vs Webshare conflicts), yt-dlp proxy/cookie mode (conflicting modes, missing cookie file), whisper.cpp model/binary/timeout, embedding backend and models, positive-integer limits (`YOUTUBE_MCP_MAX_RESOURCE_BYTES`, `YOUTUBE_MCP_MAX_VISUAL_RESOURCE_BYTES`, `YOUTUBE_MCP_CACHE_QUOTA_BYTES`), HTTP transport settings, and unrecognized `YOUTUBE_*` variable names (typo detection — names are reported, values never are).

## `cache status`

```bash
youtube-mcp-v2 cache status [--json]
```

Reports bytes and file counts per cache area (SQLite, skeletons, vectors, models, frames, audio, edit plans, editor clips), SQLite table row counts and `PRAGMA quick_check` using a **read-only** connection, free space on the cache volume (warns below 1 GiB), and usage against the optional soft quota `YOUTUBE_MCP_CACHE_QUOTA_BYTES`. It never deletes or rewrites anything; eviction remains an explicit, separate decision.
