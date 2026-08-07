"""Protocol-native reads for content-addressed visual evidence artifacts."""

from __future__ import annotations

import hashlib
import os
import re

from . import visual_index

DEFAULT_MAX_VISUAL_RESOURCE_BYTES = 16 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MIME = {"png": "image/png", "jpg": "image/jpeg"}


class VisualResourceError(RuntimeError):
    pass


class VisualResourceTooLarge(VisualResourceError):
    pass


def max_visual_resource_bytes() -> int:
    raw = os.environ.get("YOUTUBE_MCP_MAX_VISUAL_RESOURCE_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_VISUAL_RESOURCE_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_VISUAL_RESOURCE_BYTES
    return value if value > 0 else DEFAULT_MAX_VISUAL_RESOURCE_BYTES


def _validate(sha256: str, ext: str) -> tuple[str, str]:
    normalized_sha = sha256.lower()
    normalized_ext = ext.lower()
    if not _SHA_RE.fullmatch(normalized_sha):
        raise VisualResourceError("invalid visual artifact SHA-256")
    if normalized_ext not in _MIME:
        raise VisualResourceError(f"unsupported visual artifact format: {ext!r}")
    return normalized_sha, normalized_ext


def resource_uri(sha256: str, ext: str) -> str:
    normalized_sha, normalized_ext = _validate(sha256, ext)
    return f"youtube-mcp://evidence/frame/{normalized_ext}/{normalized_sha}"


def describe(sha256: str, ext: str) -> dict[str, object]:
    normalized_sha, normalized_ext = _validate(sha256, ext)
    path = visual_index._artifact_path(normalized_sha, normalized_ext)
    if not path.is_file():
        raise VisualResourceError("frozen visual artifact is unavailable")
    if visual_index._sha256_file(path) != normalized_sha:
        raise VisualResourceError("frozen visual artifact failed integrity check")
    size = path.stat().st_size
    return {
        "resource_uri": resource_uri(normalized_sha, normalized_ext),
        "resource_mime_type": _MIME[normalized_ext],
        "resource_size_bytes": size,
        "resource_portable": size <= max_visual_resource_bytes(),
    }


def read(sha256: str, ext: str) -> bytes:
    normalized_sha, normalized_ext = _validate(sha256, ext)
    path = visual_index._artifact_path(normalized_sha, normalized_ext)
    if not path.is_file():
        raise VisualResourceError("frozen visual artifact is unavailable")
    size = path.stat().st_size
    limit = max_visual_resource_bytes()
    if size > limit:
        raise VisualResourceTooLarge(
            f"visual artifact is {size} bytes; resource limit is {limit} bytes"
        )
    payload = path.read_bytes()
    # Verify the bytes we are actually about to return, not a second filesystem
    # read after the payload was captured.
    if hashlib.sha256(payload).hexdigest() != normalized_sha:
        raise VisualResourceError("frozen visual artifact failed integrity check")
    return payload
