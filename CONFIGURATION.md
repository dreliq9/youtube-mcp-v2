# Configuration

All configuration is opt-in. Secrets belong in the MCP client's environment block, a local `.env` file, or another process-level secret mechanism — never in tool arguments or committed files.

## YouTube Data API

```text
YOUTUBE_API_KEY=<key>
```

Enables `api.*` tools and tier-2 channel enumeration.

## Transcript acquisition waterfall

A live transcript request uses independent providers in this order:

1. `youtube-transcript-api` captions
2. yt-dlp caption extraction
3. local whisper.cpp speech-to-text, **only when a local model path is explicitly configured**

The returned provenance records each provider attempt and the provider that ultimately produced the cached artifact. Credential values and local model/audio filesystem paths are never part of that provenance.

## Primary transcript proxy routing

The primary transcript adapter can route `youtube-transcript-api` traffic through either a generic proxy or Webshare's rotating residential proxy integration.

### Generic HTTP/HTTPS/SOCKS proxy

Use one URL for both schemes:

```text
YOUTUBE_TRANSCRIPT_PROXY=socks5://user:password@proxy.example:1080
```

Or configure the schemes separately:

```text
YOUTUBE_TRANSCRIPT_HTTP_PROXY=http://user:password@proxy.example:8080
YOUTUBE_TRANSCRIPT_HTTPS_PROXY=http://user:password@proxy.example:8080
```

If only one scheme-specific URL is supplied, youtube-mcp-v2 uses it for both HTTP and HTTPS traffic. This is useful for a single rotating/SOCKS endpoint.

### Webshare

```text
YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME=<username>
YOUTUBE_TRANSCRIPT_WEBSHARE_PASSWORD=<password>
YOUTUBE_TRANSCRIPT_WEBSHARE_LOCATIONS=us,de
```

Locations are optional comma-separated country codes. Generic and Webshare proxy modes are mutually exclusive; configuring both is treated as a configuration error rather than silently choosing one.

## yt-dlp fallback routing and cookies

The independent yt-dlp caption provider has its own optional network/auth settings so a primary-provider proxy failure does not force the same route onto the fallback.

```text
YOUTUBE_YTDLP_PROXY=socks5://user:password@proxy.example:1080
```

For videos that require an authenticated YouTube session, configure **one** of:

```text
YOUTUBE_COOKIES_FILE=/path/to/cookies.txt
```

or:

```text
YOUTUBE_COOKIES_FROM_BROWSER=firefox
```

`YOUTUBE_COOKIES_FROM_BROWSER` accepts the selector syntax supported by yt-dlp, for example a browser/profile selector. Do not configure both cookie modes simultaneously; the server treats that as an error rather than silently choosing one.

Cookie-file handling follows yt-dlp's requirements; cookie files should not be committed to the repository.

The same yt-dlp proxy/cookie configuration is reused by `audio.get`, channel-snapshot fallback when page markup yields no videos, and the audio-download stage of local STT. This keeps caption fallback, source discovery, and local transcription on one credential/routing surface.

## Local whisper.cpp STT

Local STT is disabled by default. Configure a local whisper.cpp model to opt in:

```text
YOUTUBE_MCP_WHISPER_CPP_MODEL=/path/to/ggml-model.bin
```

The server looks for `whisper-cli` in `PATH`. Override the binary when needed:

```text
YOUTUBE_MCP_WHISPER_CPP_BIN=/path/to/whisper-cli
```

The hard subprocess deadline defaults to 3600 seconds and can be overridden:

```text
YOUTUBE_MCP_WHISPER_CPP_TIMEOUT_S=3600
```

Behavior:

- no configured model path → no local-STT attempt is added to the waterfall
- configured model but missing model/binary → structured recoverable acquisition failure
- audio is acquired as cached mono 16 kHz WAV using the normal yt-dlp proxy/cookie route
- whisper.cpp runs locally with automatic language detection and JSON timestamp output
- model downloads are **never** performed implicitly by youtube-mcp-v2

Successful local-STT provenance records only safe reproducibility metadata:

```text
engine = whisper.cpp
engine_version = <compact version when available>
model.name = <basename only>
model.size_bytes
model.sha256
audio.sha256
audio.sample_rate = 16000
audio.format = wav
```

The model path, audio path, yt-dlp cookie path, and whisper.cpp subprocess diagnostics containing local paths are not returned to MCP clients. The safe provenance is also persisted with the append-only transcript revision so later cache hits retain the same model/audio identity.

## Cache location, limits, and quota

```text
XDG_CACHE_HOME=/absolute/path          # cache root becomes $XDG_CACHE_HOME/youtube-mcp (default ~/.cache/youtube-mcp)
YOUTUBE_MCP_MAX_RESOURCE_BYTES=33554432         # largest frame/audio artifact served via resources/read (default 32 MiB)
YOUTUBE_MCP_MAX_VISUAL_RESOURCE_BYTES=16777216  # largest visual-evidence frame resource (default 16 MiB)
YOUTUBE_MCP_CACHE_QUOTA_BYTES=10737418240       # optional soft quota reported by `cache status` (nothing is evicted)
```

`youtube-mcp-v2 config check` validates these as positive integers; `youtube-mcp-v2 cache status` reports usage against the quota.

## Remote Streamable HTTP

These apply to `youtube-mcp-v2 serve --transport streamable-http` (see `CLI.md`).

```text
YOUTUBE_MCP_HTTP_TOKEN=<token>[,<token>...]   # bearer tokens, each >= 16 characters
YOUTUBE_MCP_HTTP_TOKEN_FILE=/run/secrets/tokens  # one token per line, or sha256:<hex digest>; '#' comments allowed
YOUTUBE_MCP_HTTP_RATE_LIMIT_PER_MIN=120       # sustained requests per minute per principal
YOUTUBE_MCP_HTTP_RATE_BURST=30                # token-bucket burst size
YOUTUBE_MCP_HTTP_MAX_BODY_BYTES=1048576       # largest accepted request body (min 1024)
YOUTUBE_MCP_HTTP_ALLOWED_HOSTS=mcp.example.com,mcp.example.com:443  # DNS-rebinding allowlist
```

- Clients send `Authorization: Bearer <token>`. Missing or wrong tokens get `401` with `WWW-Authenticate: Bearer`; tokens are compared as SHA-256 digests in constant time. A token file may hold only digests (`sha256:` + `hashlib.sha256(token).hexdigest()`), so the server host never stores the plaintext.
- Rate limiting is per token (identified internally by a 12-hex digest prefix, never the token) or per client IP when auth is off; excess requests get `429` with `Retry-After`.
- A non-loopback bind without any token refuses to start (exit 2) unless `--allow-unauthenticated` is passed for deployments behind an authenticating proxy.
- `/healthz` is unauthenticated and returns `{"status": "ok"}` only.
- Serving over HTTP sets `YOUTUBE_MCP_REMOTE=1`, which withholds server-local paths from tool results and skips registering `media.materialize`. Set it manually only if you front the stdio server with your own remote bridge.
- Startup logs a settings summary (host, port, auth mode, token **count**, limits) — never token values.

## Secret-handling contract

- Proxy URLs, usernames, passwords, cookie paths/selectors, cookies, and API keys must never be returned in MCP tool results.
- Local model/audio filesystem paths must never be returned as transcript acquisition provenance.
- Proxy/cookie configuration errors describe the missing or conflicting setting but do not echo its value.
- Subprocess diagnostics are redacted against configured proxy/cookie values before they become errors; whisper.cpp failure text is intentionally summarized rather than forwarding stderr.
- Safe diagnostics may report only provider names, whether routing is configured, cookie **mode** (`none`, `file`, or `browser`), non-secret location codes, and local engine/model hashes/identities that contain no path.
- `.env` is gitignored.

- `doctor`, `config check`, and `cache status` follow the same rule: they report that a credential, proxy, cookie source, or HTTP token is configured (and cookie **mode**), never the value. Their output additionally passes through a redactor that masks every configured secret value (and URL userinfo) wherever it appears, and tool responses and uncaught tool errors pass through the same redactor before reaching any client.
