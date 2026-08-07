"""Filesystem locations shared across youtube-mcp-v2.

The cache root follows the XDG Base Directory convention:
`$XDG_CACHE_HOME/youtube-mcp` when XDG_CACHE_HOME is a non-empty absolute path,
otherwise `~/.cache/youtube-mcp`.

The function form is useful for diagnostics/tests, while module-level constants
preserve the existing import/monkeypatch pattern used throughout the package.
"""

from __future__ import annotations

import os
from pathlib import Path


def cache_base_dir() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    if configured and configured.strip():
        candidate = Path(configured).expanduser()
        # XDG base-directory variables must be absolute. A relative value is
        # invalid and should be ignored rather than changing meaning with cwd.
        if candidate.is_absolute():
            return candidate
    return Path.home() / ".cache"


def youtube_cache_dir() -> Path:
    return cache_base_dir() / "youtube-mcp"


CACHE_DIR = youtube_cache_dir()
SQLITE_PATH = CACHE_DIR / "v2.sqlite"
SKELETON_DIR = CACHE_DIR / "skeletons"
VECTOR_DIR = CACHE_DIR / "vectors"
MODEL_DIR = CACHE_DIR / "models"
FRAMES_DIR = CACHE_DIR / "frames"
AUDIO_DIR = CACHE_DIR / "audio"
EDIT_PLAN_DIR = CACHE_DIR / "edit-plans"
EDITOR_CLIP_DIR = CACHE_DIR / "editor-clips"
