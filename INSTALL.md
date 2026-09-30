# Installing youtube-mcp-v2

youtube-mcp-v2 is a Python (3.10–3.13) MCP server. The recommended launch path is [`uvx`](https://docs.astral.sh/uv/guides/tools/), which runs the published package in an isolated, cached environment with no manual virtualenv.

> **Status:** the package is prepared for PyPI (`youtube-mcp-v2`), GHCR (`ghcr.io/dreliq9/youtube-mcp-v2`), and the MCP Registry (`io.github.dreliq9/youtube-mcp-v2`) but has **not been published yet** — see `RELEASING.md`. Until the first release, replace `uvx youtube-mcp-v2` with
> `uvx --from git+https://github.com/dreliq9/youtube-mcp-v2 youtube-mcp-v2`
> and build the Docker image locally (`docker build -t youtube-mcp-v2 .`).

## 1. Prerequisites

| | Linux | macOS | Windows (PowerShell) |
| --- | --- | --- | --- |
| uv | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `brew install uv` or the Linux one-liner | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` or `winget install astral-sh.uv` |
| ffmpeg + ffprobe (frames/audio/clips only) | `sudo apt install ffmpeg` / `sudo dnf install ffmpeg` / `sudo pacman -S ffmpeg` | `brew install ffmpeg` | `winget install Gyan.FFmpeg` (or `choco install ffmpeg`, `scoop install ffmpeg`), then open a new terminal |

uv downloads a suitable Python automatically if none is installed. yt-dlp is a Python dependency and needs no separate install. Metadata, search, transcripts, corpora, and lexical search work without ffmpeg; `doctor` reports what is missing.

## 2. Run and verify

```bash
uvx youtube-mcp-v2 --version
uvx youtube-mcp-v2 doctor          # offline: versions, ffmpeg/yt-dlp, cache writability, backends
uvx youtube-mcp-v2 config check    # validates environment variables without printing secrets
uvx youtube-mcp-v2                 # stdio MCP server (what MCP clients launch)
```

Optional extras:

```bash
uvx --from "youtube-mcp-v2[semantic]" youtube-mcp-v2   # FastEmbed semantic + visual search
uvx --from "youtube-mcp-v2[api]" youtube-mcp-v2        # YouTube Data API tier-2 tools
uvx --from "youtube-mcp-v2[all]" youtube-mcp-v2        # both
```

Persistent installs instead of `uvx`:

```bash
uv tool install "youtube-mcp-v2[all]"      # puts youtube-mcp-v2 on PATH
pipx install "youtube-mcp-v2[all]"
python -m pip install "youtube-mcp-v2[all]"   # inside a virtualenv; Windows: py -m pip ...
```

Cache location: `$XDG_CACHE_HOME/youtube-mcp` when `XDG_CACHE_HOME` is an absolute path, otherwise `~/.cache/youtube-mcp` on every OS (on Windows, `%USERPROFILE%\.cache\youtube-mcp`). Inspect it with `youtube-mcp-v2 cache status`.

## 3. Configure an MCP client (stdio)

Generic JSON used by Claude Desktop, Cursor, and most clients:

```json
{
  "mcpServers": {
    "youtube": {
      "command": "uvx",
      "args": ["youtube-mcp-v2"],
      "env": { "YOUTUBE_API_KEY": "optional" }
    }
  }
}
```

- Claude Desktop config file: macOS `~/Library/Application Support/Claude/claude_desktop_config.json`; Windows `%APPDATA%\Claude\claude_desktop_config.json`.
- Claude Code: `claude mcp add youtube -- uvx youtube-mcp-v2`.
- **macOS GUI apps** do not inherit your shell `PATH`. If the client cannot find `uvx` or ffmpeg, use the absolute path (`which uvx`, typically `~/.local/bin/uvx` or `/opt/homebrew/bin/uvx`) as `command` and add `"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"` to `env`.
- **Windows**: if the client reports `uvx` not found, use the full path from `where uvx` (typically `C:\Users\<you>\.local\bin\uvx.exe`) and escape backslashes in JSON.

Secrets (API key, proxies, cookies) go in the client's `env` block or a local `.env`, never in tool arguments. See `CONFIGURATION.md`.

## 4. Docker

```bash
docker build -t youtube-mcp-v2 .                 # until the GHCR image is published
docker run --rm youtube-mcp-v2 doctor
```

stdio (client launches the container):

```json
{
  "mcpServers": {
    "youtube": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-v", "youtube-mcp-cache:/data", "ghcr.io/dreliq9/youtube-mcp-v2"]
    }
  }
}
```

The image runs as a non-root user (uid 10001), includes ffmpeg, and stores all evidence under the `/data` volume. Pass secrets with `-e NAME` (value from the host environment) rather than `-e NAME=value` in shared configs.

## 5. Remote deployment

```bash
export YOUTUBE_MCP_HTTP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker run -d --name youtube-mcp -p 8000:8000 -v youtube-mcp-cache:/data \
  -e YOUTUBE_MCP_HTTP_TOKEN \
  --health-cmd 'python -c "import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:8000/healthz\", timeout=4)"' \
  ghcr.io/dreliq9/youtube-mcp-v2 \
  serve --transport streamable-http --host 0.0.0.0 --allowed-host mcp.example.com
```

Without Docker: `youtube-mcp-v2 serve --transport streamable-http --host 0.0.0.0 --allowed-host mcp.example.com` with the same environment.

Connect a client to `https://mcp.example.com/mcp` with header `Authorization: Bearer <token>`, e.g. Claude Code:

```bash
claude mcp add --transport http youtube https://mcp.example.com/mcp --header "Authorization: Bearer $YOUTUBE_MCP_HTTP_TOKEN"
```

Deployment checklist:

- Terminate TLS in a reverse proxy (Caddy, nginx, a cloud load balancer); the server itself speaks plain HTTP.
- Always set `--allowed-host` / `YOUTUBE_MCP_HTTP_ALLOWED_HOSTS` to the public host name (DNS-rebinding protection).
- Prefer `YOUTUBE_MCP_HTTP_TOKEN_FILE` with `sha256:<digest>` lines so no plaintext token is stored on the host. Rotate by adding the new token, updating clients, then removing the old one.
- Default limits: 120 requests/min per token with a burst of 30, 1 MiB request bodies, 32 MiB frame/audio resources, 16 MiB visual-evidence resources.
- The default mode is stateless JSON, so several replicas can sit behind a load balancer without sticky sessions, provided they share the `/data` volume or each has its own cache.
- Remote results never contain server filesystem paths. Frames, audio, and visual evidence come back as `resource_uri` + `resource_sha256`; fetch the bytes with MCP `resources/read`. `media.materialize` is local-only and is not offered remotely.
- Clients that only speak the 2024-11-05 HTTP+SSE transport are not supported over HTTP; run them against stdio instead. Compatibility details: `CLI.md#protocol-era-compatibility`.

## 6. From source (contributors)

```bash
git clone https://github.com/dreliq9/youtube-mcp-v2 && cd youtube-mcp-v2
uv venv && uv pip install -e ".[dev,all]"      # or python -m venv .venv && pip install -e ".[dev,all]"
python -m pytest -m "not network and not slow" -q
python scripts/smoke_install.py --command .venv/bin/youtube-mcp-v2   # Windows: .venv\Scripts\youtube-mcp-v2.exe
```
