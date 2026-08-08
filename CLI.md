# CLI

## Run the MCP server

Historical behavior remains the default:

```bash
youtube-mcp-v2
```

An explicit equivalent is available for scripts and humans:

```bash
youtube-mcp-v2 serve
```

Both run the stdio MCP server.

## Diagnose a local installation

```bash
youtube-mcp-v2 doctor
```

`doctor` is deliberately offline. It verifies local conditions that should be deterministic:

- supported Python version,
- required Python distributions,
- MCP server import/initialization,
- configured cache root writability,
- yt-dlp availability,
- ffmpeg / ffprobe availability,
- whether a YouTube Data API key is configured.

Missing media binaries are warnings in the v0.2.1 line because metadata/search/transcript functionality can still operate without every media capability. Required package, Python, server-import, or cache failures produce a failing exit status.

For automation:

```bash
youtube-mcp-v2 doctor --json
```

The JSON report is stable machine-readable diagnostics. It reports whether `YOUTUBE_API_KEY` is configured but never prints the key value. Future proxy/cookie/provider diagnostics must follow the same rule: presence/capability only, never credential values.

`doctor` intentionally does not make a YouTube request. Live upstream reachability belongs in network smoke/benchmark workflows where throttling or provider blocking can be distinguished from a broken local install.
