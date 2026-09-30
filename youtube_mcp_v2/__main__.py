"""`python -m youtube_mcp_v2` — same entry point as the `youtube-mcp-v2` script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
