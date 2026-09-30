"""Remote-safe tool results.

Over stdio the client shares the server's machine, so server-local paths are a
useful convenience. Over Streamable HTTP they are meaningless to the client and
leak server layout, so remote mode withholds them: clients consume the
`resource_uri` fields (bounded binary MCP resources) instead.

Remote mode is enabled by `youtube-mcp-v2 serve --transport streamable-http`
(or explicitly with YOUTUBE_MCP_REMOTE=1) before the server module is imported.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from . import redaction
from .paths import CACHE_DIR

REMOTE_ENV = "YOUTUBE_MCP_REMOTE"
WITHHELD = "<server-local path withheld>"
REMOTE_WARNING = (
    "remote mode: server-local paths were withheld; use resource_uri fields to "
    "retrieve artifacts as MCP resources"
)

# Keys whose values are server filesystem locations.
_PATH_KEY_RE = re.compile(r"(^|_)(path|paths|dir|dirs|file|filename)$")
# Tools whose only useful output is server-local files.
LOCAL_ONLY_TOOLS = frozenset({"media.materialize"})


def remote_mode() -> bool:
    return os.environ.get(REMOTE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _local_roots() -> list[str]:
    roots = {str(CACHE_DIR), str(Path.home()), tempfile.gettempdir(), str(Path.cwd())}
    resolved = set()
    for root in roots:
        resolved.add(root)
        try:
            resolved.add(str(Path(root).resolve()))
        except OSError:
            pass
    return sorted((r for r in resolved if r and r not in {"/", "\\"}), key=len, reverse=True)


def _looks_local(text: str, roots: list[str]) -> bool:
    return any(text == root or text.startswith((root.rstrip("/\\") + os.sep, root.rstrip("/\\") + "/")) for root in roots)


def _scrub_text(text: str, roots: list[str]) -> str:
    if _looks_local(text, roots):
        return WITHHELD
    for root in roots:
        if root in text:
            text = re.sub(re.escape(root) + r"[^\s'\"]*", WITHHELD, text)
    return text


def strip_local_paths(value: Any, roots: list[str] | None = None) -> tuple[Any, int]:
    """Remove path-valued fields and server-local path strings. Returns (value, count)."""
    roots = _local_roots() if roots is None else roots
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        removed = 0
        for key, item in value.items():
            if isinstance(key, str) and _PATH_KEY_RE.search(key) and item is not None:
                removed += 1
                continue
            cleaned, n = strip_local_paths(item, roots)
            out[key] = cleaned
            removed += n
        return out, removed
    if isinstance(value, list):
        items = []
        removed = 0
        for item in value:
            cleaned, n = strip_local_paths(item, roots)
            items.append(cleaned)
            removed += n
        return items, removed
    if isinstance(value, str):
        cleaned = _scrub_text(value, roots)
        return cleaned, int(cleaned != value)
    return value, 0


def finalize_tool_result(result: Any) -> Any:
    """Last step for every tool response: redact secrets; strip paths remotely."""
    result = redaction.redact(result)
    if not remote_mode():
        return result
    cleaned, removed = strip_local_paths(result)
    if (
        removed
        and isinstance(cleaned, dict)
        and isinstance(cleaned.get("warnings"), list)
        and REMOTE_WARNING not in cleaned["warnings"]
    ):
        cleaned["warnings"] = [*cleaned["warnings"], REMOTE_WARNING]
    return cleaned
