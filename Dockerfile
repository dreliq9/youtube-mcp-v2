# syntax=docker/dockerfile:1
#
# youtube-mcp-v2 container image.
#
#   stdio (MCP clients that launch containers):
#     docker run -i --rm -v youtube-mcp-cache:/data ghcr.io/dreliq9/youtube-mcp-v2
#
#   Streamable HTTP (remote clients; bearer token required off-loopback):
#     docker run --rm -p 8000:8000 -v youtube-mcp-cache:/data \
#       -e YOUTUBE_MCP_HTTP_TOKEN=... ghcr.io/dreliq9/youtube-mcp-v2 \
#       serve --transport streamable-http --host 0.0.0.0 --allowed-host mcp.example.com
#
# Build locally:  docker build -t youtube-mcp-v2:dev .

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir "build>=1.2"
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY youtube_mcp_v2 ./youtube_mcp_v2
RUN python -m build --wheel --outdir /dist

FROM python:${PYTHON_VERSION}-slim

LABEL org.opencontainers.image.title="youtube-mcp-v2" \
      org.opencontainers.image.description="Evidence-grade YouTube research MCP server" \
      org.opencontainers.image.source="https://github.com/dreliq9/youtube-mcp-v2" \
      org.opencontainers.image.licenses="MIT" \
      io.modelcontextprotocol.server.name="io.github.dreliq9/youtube-mcp-v2"

# ffmpeg/ffprobe back frame and audio extraction; yt-dlp ships as a Python dependency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown app:app /data
USER app
WORKDIR /home/app

# All persisted evidence (SQLite observations, corpora, indexes, media) lives under /data/youtube-mcp.
ENV XDG_CACHE_HOME=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000

# No HEALTHCHECK: the default command is stdio. For HTTP deployments use
#   --health-cmd 'python -c "import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:8000/healthz\", timeout=4)"'
# (see INSTALL.md).

ENTRYPOINT ["youtube-mcp-v2"]
CMD ["serve"]
