# Releasing youtube-mcp-v2

Nothing has been published yet. This document turns the first (and every later) release into one reviewed version-bump commit plus one tag push.

Distribution targets:

| Target | Identifier | Launch |
| --- | --- | --- |
| PyPI | `youtube-mcp-v2` | `uvx youtube-mcp-v2` |
| GHCR | `ghcr.io/dreliq9/youtube-mcp-v2:<version>` (+ `latest`) | `docker run -i --rm ghcr.io/dreliq9/youtube-mcp-v2` |
| MCP Registry | `io.github.dreliq9/youtube-mcp-v2` (`server.json`) | discovered by registry-aware clients |

## One-time setup

1. Move `ci/release.yml` to `.github/workflows/release.yml` (needs the `workflow` scope; see `ci/README.md`).
2. Create the `pypi`, `ghcr`, and `mcp-registry-publish` environments and the PyPI trusted publisher described in `ci/README.md`. The PyPI project name `youtube-mcp-v2` was unclaimed when this was written; a trusted publisher can be registered as a *pending* publisher before the first upload.

## Per-release checklist

1. **Bump the version** — it must match in all of these (`tests/test_distribution.py` enforces this, and the release workflow refuses a mismatched tag):
   - `youtube_mcp_v2/__init__.py` → `__version__` (single source for the package metadata),
   - `server.json` → top-level `version`, the pypi package `version`, and the oci `identifier` tag,
   - `CHANGELOG.md` → a `## <version>` heading (replace `— unreleased` with the date).
2. **Verify locally:**
   ```bash
   python -m pip install -e ".[dev,release]"
   python -m pytest -m "not network and not slow" -q
   rm -rf dist && python -m build && python -m twine check --strict dist/*
   python scripts/smoke_install.py --expect-version <version> --command uvx --from dist/youtube_mcp_v2-<version>-py3-none-any.whl youtube-mcp-v2
   docker build -t youtube-mcp-v2:dev . && docker run --rm youtube-mcp-v2:dev doctor
   ```
   Optionally validate `server.json` with `mcp-publisher validate`.
3. **Merge** the bump PR, and let the `release` workflow pass on it.
4. **Tag and push:** `git tag v<version> && git push origin v<version>`. The workflow builds and smoke-tests, then publishes to PyPI (trusted publishing), pushes the multi-arch GHCR image, and publishes `server.json` to the MCP Registry. Each step waits on its environment's approval.
5. **Post-release:** create the GitHub release from the CHANGELOG section, then check `uvx youtube-mcp-v2@<version> --version` on a clean machine.

### Manual fallback (no workflow)

```bash
python -m twine upload dist/*                      # PyPI API token
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/dreliq9/youtube-mcp-v2:<version> -t ghcr.io/dreliq9/youtube-mcp-v2:latest --push .
mcp-publisher login github && mcp-publisher publish
```

The registry checks ownership against `<!-- mcp-name: io.github.dreliq9/youtube-mcp-v2 -->` in the README (the PyPI long description) and the image label `io.modelcontextprotocol.server.name`, so publish PyPI and GHCR **before** the registry.

## Migration notes for existing users (git/editable installs → packaged releases)

- **Launch command**: replace `python -m youtube_mcp_v2`, a venv path, or `pip install git+…` in client configs with `uvx youtube-mcp-v2`. Plain `youtube-mcp-v2` (no subcommand) is still the stdio server, so existing configs keep working.
- **Cache**: unchanged. `$XDG_CACHE_HOME/youtube-mcp` or `~/.cache/youtube-mcp` is shared by every install method, so frozen skeletons, transcripts, and indexes carry over. Docker uses the `/data` volume; to reuse a host cache, mount it: `-v ~/.cache:/data`.
- **Extras**: yt-dlp is a core dependency. `[media]`, `[frame]`, and `[audio]` remain as no-op compatibility aliases. Use `[semantic]`, `[api]`, or `[all]` for the optional backends.
- **Configuration**: all environment variables are unchanged. Run `youtube-mcp-v2 config check` after upgrading to catch typos; it lists unrecognized `YOUTUBE_*` names without printing values.
- **New response field**: frame/audio artifacts now include `resource_sha256` next to `resource_uri`. This is additive.
- **Remote mode (new)**: `serve --transport streamable-http` withholds `path`, `cache_dir`, `manifest_path`, and similar server-local fields from tool results, and does not offer `media.materialize`. Local stdio responses are unchanged.
