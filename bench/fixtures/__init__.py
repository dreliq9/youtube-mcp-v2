"""Deterministic upstream fixtures for the in-repo youtube-mcp-v2 adapter.

Fixtures replace only the outermost upstream boundary (the youtube-transcript-api
client object, the yt-dlp subprocess, the watch-page HTTP transport, the Data API
client) so the server's own parsing, fallback, validation, caching, and envelope
code runs for real. All transcript text is synthetic and benchmark-owned; no
third-party video or transcript payload is stored in this repository.
"""
