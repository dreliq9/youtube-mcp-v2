# Configuration

All configuration is opt-in. Secrets belong in the MCP client's environment block, a local `.env` file, or another process-level secret mechanism — never in tool arguments or committed files.

## YouTube Data API

```text
YOUTUBE_API_KEY=<key>
```

Enables `api.*` tools and tier-2 channel enumeration.

## Transcript acquisition waterfall

A live transcript request first uses `youtube-transcript-api`. If that provider is unavailable, blocked, or cannot produce a usable track, youtube-mcp-v2 tries an independent yt-dlp caption path.

The returned provenance records each provider attempt and the provider that ultimately produced the cached artifact. Credential values are never part of that provenance.

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

## Secret-handling contract

- Proxy URLs, usernames, passwords, cookie paths/selectors, cookies, and API keys must never be returned in MCP tool results.
- Proxy/cookie configuration errors describe the missing or conflicting setting but do not echo its value.
- Subprocess diagnostics are redacted against configured proxy/cookie values before they become errors.
- Safe diagnostics may report only provider names, whether routing is configured, cookie **mode** (`none`, `file`, or `browser`), and non-secret location codes.
- `.env` is gitignored.

Future `doctor`/health commands should follow the same rule: report that a credential is configured, never print the credential itself.
