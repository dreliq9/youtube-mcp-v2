"""Portable MCP resource references for persisted frame/audio artifacts.

Local filesystem paths remain useful to same-host coding agents. Resource URIs
make the same persisted bytes consumable by clients that do not share the
server's filesystem. Resource reads are bounded so a remote client cannot
accidentally pull an arbitrarily large full-length audio file into one response.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .paths import AUDIO_DIR, FRAMES_DIR

DEFAULT_MAX_RESOURCE_BYTES = 32 * 1024 * 1024
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_FRAME_MIME = {"png": "image/png", "jpg": "image/jpeg"}
_AUDIO_MIME = {
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
}


class ArtifactError(RuntimeError):
    """Base error for persisted artifact references."""


class ArtifactNotFound(ArtifactError):
    pass


class ArtifactTooLarge(ArtifactError):
    pass


@dataclass(frozen=True)
class ArtifactReference:
    uri: str | None
    mime_type: str
    size_bytes: int
    portable: bool
    kind: Literal["frame", "audio"]

    def as_dict(self) -> dict[str, object]:
        return {
            "resource_uri": self.uri,
            "resource_mime_type": self.mime_type,
            "resource_size_bytes": self.size_bytes,
            "resource_portable": self.portable,
        }


def max_resource_bytes() -> int:
    raw = os.environ.get("YOUTUBE_MCP_MAX_RESOURCE_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_RESOURCE_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_RESOURCE_BYTES
    return value if value > 0 else DEFAULT_MAX_RESOURCE_BYTES


def _validate_identity(video_id: str, name: str) -> None:
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise ArtifactError(f"invalid video id for artifact: {video_id!r}")
    if not name or not _NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise ArtifactError(f"invalid artifact name: {name!r}")


def _mime(kind: str, ext: str) -> str:
    table = _FRAME_MIME if kind == "frame" else _AUDIO_MIME if kind == "audio" else {}
    try:
        return table[ext]
    except KeyError as exc:
        raise ArtifactError(f"unsupported {kind} artifact format: {ext!r}") from exc


def _root(kind: str) -> Path:
    if kind == "frame":
        return FRAMES_DIR
    if kind == "audio":
        return AUDIO_DIR
    raise ArtifactError(f"unsupported artifact kind: {kind!r}")


def _resource_uri(kind: str, ext: str, video_id: str, name: str) -> str:
    _validate_identity(video_id, name)
    _mime(kind, ext)
    return f"youtube-mcp://artifact/{kind}/{ext}/{video_id}/{name}"


def reference_for_path(
    path: str | Path,
    *,
    kind: Literal["frame", "audio"],
    video_id: str,
) -> ArtifactReference:
    """Create a portable reference for an artifact path returned by our adapters."""
    artifact_path = Path(path)
    ext = artifact_path.suffix.lower().lstrip(".")
    name = artifact_path.stem
    _validate_identity(video_id, name)
    mime_type = _mime(kind, ext)

    try:
        size = artifact_path.stat().st_size
    except OSError as exc:
        raise ArtifactNotFound(f"artifact path is unavailable: {artifact_path}") from exc

    limit = max_resource_bytes()
    portable = size <= limit
    return ArtifactReference(
        uri=_resource_uri(kind, ext, video_id, name) if portable else None,
        mime_type=mime_type,
        size_bytes=size,
        portable=portable,
        kind=kind,
    )


def _safe_path(
    *,
    kind: Literal["frame", "audio"],
    ext: str,
    video_id: str,
    name: str,
) -> Path:
    _validate_identity(video_id, name)
    _mime(kind, ext)

    expected_dir = (_root(kind) / video_id).resolve()
    candidate = (expected_dir / f"{name}.{ext}").resolve()
    if candidate.parent != expected_dir:
        raise ArtifactError("artifact path escaped its video cache directory")
    if not candidate.is_file():
        raise ArtifactNotFound(f"artifact does not exist: {kind}/{video_id}/{name}.{ext}")
    return candidate


def read_resource(
    *,
    kind: Literal["frame", "audio"],
    ext: str,
    video_id: str,
    name: str,
) -> bytes:
    """Read one persisted artifact for an MCP binary-resource response."""
    path = _safe_path(kind=kind, ext=ext, video_id=video_id, name=name)
    size = path.stat().st_size
    limit = max_resource_bytes()
    if size > limit:
        raise ArtifactTooLarge(
            f"artifact is {size} bytes; portable resource limit is {limit} bytes. "
            "Request a shorter audio clip or raise YOUTUBE_MCP_MAX_RESOURCE_BYTES."
        )
    return path.read_bytes()
