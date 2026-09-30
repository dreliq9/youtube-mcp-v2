# Configuration

All configuration is opt-in. Secrets belong in the MCP client's environment block, a local `.env` file, or another process-level secret mechanism — never in tool arguments or committed files.

## YouTube Data API

```text
YOUTUBE_API_KEY=<key>
```

Enables `api.*` tools and tier-2 channel enumeration.

## Transcript acquisition waterfall

`transcript.get` never asks the caller to choose a provider. A request walks these stages in order and stops at the first success:

| # | Stage (`provider` / `method`) | Runs when |
|---|---|---|
| 0 | cache / `transcript_revision` | a fresh (30-day TTL), structurally decodable cached revision exists — no upstream work at all |
| 1 | `youtube-transcript-api` / `captions` | always (native caption HTTP path) |
| 2 | `yt-dlp` / `captions` | stage 1 failed; runs **anonymously** (cookies are never sent here) |
| 3 | `yt-dlp` / `captions_authenticated` | cookies are configured **and** stage 2 was blocked, rate limited, circuit-open, or reported a sign-in/membership requirement |
| 4 | `whisper.cpp` / `local_stt` | a local model path is explicitly configured and no stage reported `membership_required` |

The returned `provenance.acquisition` records `provider`, `method`, `provider_class` (`scrape` or `local`) and an ordered `attempts` list. Each attempt has only machine-safe fields:

```json
{"provider": "yt-dlp", "method": "captions", "duration_ms": 812,
 "outcome": "success", "detail_code": null, "retry": 0}
```

`outcome` is one of `success`, `unavailable`, `blocked`, `timeout`, `error`, `skipped`. Stable `detail_code` values include:

- `transcripts_disabled`, `no_transcript`, `video_unavailable`, `video_unplayable`, `login_required`, `membership_required`, `missing_dependency` (unavailable)
- `request_blocked`, `rate_limited` (HTTP 429), `po_token_required` (blocked)
- `timeout`, `local_stt_timeout` (timeout)
- `malformed_payload`, `network_error`, `proxy_error`, `config_error`, `provider_error`, `local_stt_failed`, `cache_malformed` (error)
- `circuit_open` (skipped — see below)

The attempt history is persisted with the append-only cache revision, so a later cache hit returns the **original** acquisition provenance plus `provenance.cache = {"hit": true, "revision_fetched_at": ...}`. A corrupt cached row is never trusted: it is recorded as a `cache_malformed` attempt and the live waterfall produces a new revision.

Credential values, exception text, and local model/audio filesystem paths are never part of provenance or acquisition logs.

### Timeouts, retries, and circuit breaker

```text
YOUTUBE_TRANSCRIPT_TIMEOUT_S=20          # per-HTTP-request timeout for stage 1 (0 < n <= 300)
YOUTUBE_MCP_ACQUISITION_RETRIES=1        # extra tries per stage for timeout/network_error (0–3)
YOUTUBE_MCP_ACQUISITION_BACKOFF_S=0.5    # exponential backoff base between retries (0–10)
YOUTUBE_MCP_BREAKER_THRESHOLD=3          # consecutive blocked/rate-limited outcomes that open a stage's breaker
YOUTUBE_MCP_BREAKER_COOLDOWN_S=300       # how long an open breaker skips the stage before one half-open trial
```

- Only transient failures (`timeout`, `network_error`) are retried; each retry is a separate attempt with an incrementing `retry` index. Blocks, rate limits, malformed payloads, and "no captions" answers are never retried on the same stage. Local STT is never retried.
- yt-dlp subprocesses keep their existing hard deadlines (60s metadata, 90s subtitle download).
- Each stage (`provider:method`) has an in-process circuit breaker. After the threshold of consecutive `blocked` outcomes, the stage is skipped (`outcome: "skipped"`, `detail_code: "circuit_open"`) until the cooldown elapses; then one trial request is allowed. Success or a non-block answer closes the breaker; another block re-opens it. Breaker state is per server process and resets on restart.

## Shared proxy routing

```text
YOUTUBE_MCP_PROXY=socks5://user:password@proxy.example:1080
```

One HTTP/HTTPS/SOCKS URL used as the default route for every acquisition path: the native caption HTTP client (stage 1) and all yt-dlp invocations (caption stages, `audio.get`, channel fallback, local-STT audio download, `media.materialize`). A provider-specific setting (below) always wins over the shared route. SOCKS URLs on the native HTTP path require PySocks (`pip install "requests[socks]"`); without it the attempt fails with `detail_code: "config_error"` and the waterfall continues. yt-dlp supports SOCKS natively.

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

For videos that require an authenticated YouTube session, configure **one** of the following. Cookies are used only by the dedicated cookie-authenticated caption stage (stage 3 above), never by the anonymous yt-dlp caption stage:

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

## Secret-handling contract

- Proxy URLs, usernames, passwords, cookie paths/selectors, cookies, and API keys must never be returned in MCP tool results.
- Local model/audio filesystem paths must never be returned as transcript acquisition provenance.
- Proxy/cookie configuration errors describe the missing or conflicting setting but do not echo its value.
- Subprocess diagnostics are redacted against configured proxy/cookie values before they become errors; whisper.cpp failure text is intentionally summarized rather than forwarding stderr.
- Safe diagnostics may report only provider names, whether routing is configured, cookie **mode** (`none`, `file`, or `browser`), non-secret location codes, and local engine/model hashes/identities that contain no path.
- `.env` is gitignored.

Future `doctor`/health commands should follow the same rule: report that a credential or local model is configured, never print the credential/path itself.
