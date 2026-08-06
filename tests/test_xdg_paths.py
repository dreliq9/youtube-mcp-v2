from __future__ import annotations

import importlib
from pathlib import Path

from youtube_mcp_v2 import cache, paths, skeleton
from youtube_mcp_v2.adapters import audio_extract, frame_extract


def _reload_storage_modules() -> None:
    importlib.reload(paths)
    importlib.reload(cache)
    importlib.reload(skeleton)
    importlib.reload(audio_extract)
    importlib.reload(frame_extract)


def test_xdg_cache_home_moves_every_persisted_artifact_root(tmp_path, monkeypatch) -> None:
    with monkeypatch.context() as scoped:
        scoped.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
        _reload_storage_modules()

        expected = tmp_path / "xdg-cache" / "youtube-mcp"
        assert paths.CACHE_DIR == expected
        assert cache.CACHE_DIR == expected
        assert cache.CACHE_PATH == expected / "v2.sqlite"
        assert skeleton.SKELETON_DIR == expected / "skeletons"
        assert skeleton.VECTOR_DIR == expected / "vectors"
        assert skeleton.MODEL_DIR == expected / "models"
        assert audio_extract.AUDIO_DIR == expected / "audio"
        assert frame_extract.FRAMES_DIR == expected / "frames"

    _reload_storage_modules()


def test_blank_xdg_cache_home_uses_home_cache(monkeypatch) -> None:
    with monkeypatch.context() as scoped:
        scoped.setenv("XDG_CACHE_HOME", "   ")
        importlib.reload(paths)
        assert paths.cache_base_dir() == Path.home() / ".cache"
    importlib.reload(paths)


def test_relative_xdg_cache_home_is_invalid_and_ignored(monkeypatch) -> None:
    with monkeypatch.context() as scoped:
        scoped.setenv("XDG_CACHE_HOME", "relative-cache")
        importlib.reload(paths)
        assert paths.cache_base_dir() == Path.home() / ".cache"
        assert paths.CACHE_DIR == Path.home() / ".cache" / "youtube-mcp"
    importlib.reload(paths)
