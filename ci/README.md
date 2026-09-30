# ci/ — workflows staged outside `.github/`

`release.yml` is a complete GitHub Actions workflow that is **not active yet**. It was authored with a token that lacks the `workflow` scope, which GitHub requires for any change under `.github/workflows/`.

To activate it (one commit by someone with workflow permission):

```bash
git mv ci/release.yml .github/workflows/release.yml
```

Then, before the first tag, create these GitHub Environments (Settings → Environments) and restrict each to `v*` tags, ideally with a required reviewer:

| Environment | Used by | Setup |
| --- | --- | --- |
| `pypi` | `publish-pypi` | Add a PyPI *trusted publisher* for project `youtube-mcp-v2`: owner `dreliq9`, repo `youtube-mcp-v2`, workflow `release.yml`, environment `pypi`. No API token is stored. |
| `ghcr` | `publish-ghcr` | None; uses the job's `GITHUB_TOKEN` with `packages: write`. After the first push, make the package public in the package settings. |
| `mcp-registry-publish` | `publish-mcp-registry` | None; uses GitHub OIDC (`mcp-publisher login github-oidc`) for the `io.github.dreliq9/*` namespace. |

On pull requests the workflow only builds, runs `twine check`, and runs install and Docker smoke tests; nothing is published. See `RELEASING.md` for the full release procedure.
