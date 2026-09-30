"""Packaging/registry metadata consistency and a real first stdio launch (no network)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from youtube_mcp_v2 import __version__, config_check, redaction

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_NAME = "io.github.dreliq9/youtube-mcp-v2"

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None


def _server_json() -> dict:
    return json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def test_version_is_single_sourced() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    assert 'version = { attr = "youtube_mcp_v2.__version__" }' in text
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", __version__)


@pytest.mark.skipif(tomllib is None, reason="tomllib requires Python 3.11+")
def test_pyproject_metadata_is_publishable() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == "youtube-mcp-v2"
    assert project["license"] == "MIT"
    assert project["scripts"] == {"youtube-mcp-v2": "youtube_mcp_v2.cli:main"}
    assert {"Homepage", "Repository", "Issues", "Changelog"} <= set(project["urls"])
    assert any("3.13" in c for c in project["classifiers"])


def test_registry_metadata_matches_package_and_readme() -> None:
    doc = _server_json()
    assert doc["name"] == REGISTRY_NAME
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", doc["name"])
    assert 1 <= len(doc["description"]) <= 100
    assert doc["version"] == __version__
    pypi = next(p for p in doc["packages"] if p["registryType"] == "pypi")
    assert pypi["identifier"] == "youtube-mcp-v2"
    assert pypi["version"] == __version__
    assert pypi["registryBaseUrl"] == "https://pypi.org"
    oci = next(p for p in doc["packages"] if p["registryType"] == "oci")
    assert oci["identifier"].endswith(f":{__version__}")
    # Registry ownership proofs: README token (PyPI) and image label (OCI).
    assert f"mcp-name: {REGISTRY_NAME}" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert f'io.modelcontextprotocol.server.name="{REGISTRY_NAME}"' in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_registry_env_vars_are_known_and_secrets_flagged() -> None:
    for package in _server_json()["packages"]:
        for var in package.get("environmentVariables", []):
            name = var["name"]
            assert name in config_check.KNOWN_ENV_VARS or name == "XDG_CACHE_HOME"
            if name in redaction.SECRET_ENV_VARS:
                assert var.get("isSecret") is True, name


def test_changelog_mentions_current_version() -> None:
    assert f"## {__version__}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_first_stdio_launch_lists_tools(tmp_path) -> None:
    from mcp import Client, StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "youtube_mcp_v2", "serve"],
        env={"XDG_CACHE_HOME": str(tmp_path), "PYTHONPATH": str(ROOT)},
        cwd=str(tmp_path),
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        info = client.session.server_info if hasattr(client.session, "server_info") else None
    names = {t.name for t in tools.tools}
    assert {"inspect.video", "transcript.get", "corpus.search", "frame.get"} <= names
    if info is not None:
        assert info.version == __version__
