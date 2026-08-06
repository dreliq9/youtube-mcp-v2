# Configuration

All configuration is opt-in. Secrets belong in the MCP client's environment block, a local `.env` file, or another process-level secret mechanism — never in tool arguments or committed files.

## YouTube Data API

```text
YOUTUBE_API_KEY=<key>
```

Enables `api.*` tools and tier-2 channel enumeration.

## Transcript proxy routing

The transcript adapter can route `youtube-transcript-api` traffic through either a generic proxy or Webshare's rotating residential proxy integration.

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

## Secret-handling contract

- Proxy URLs, usernames, passwords, cookies, and API keys must never be returned in MCP tool results.
- Proxy configuration errors describe the missing/conflicting setting but do not echo its value.
- Safe diagnostics may report only the proxy mode, whether generic HTTP/HTTPS routing is configured, and non-secret location codes.
- `.env` is gitignored.

Future `doctor`/health commands should follow the same rule: report that a credential is configured, never print the credential itself.
