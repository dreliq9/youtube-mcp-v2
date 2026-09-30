"""`youtube-mcp-v2 cache status` — storage usage, quota, and cache health.

Read-only and offline. Reports sizes/counts per cache area, SQLite row counts
and integrity, free disk space, and an optional soft quota
(YOUTUBE_MCP_CACHE_QUOTA_BYTES). Nothing is deleted.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import paths
from .doctor import Check

LOW_DISK_BYTES = 1024 * 1024 * 1024  # warn below 1 GiB free
_AREAS = (
    ("sqlite", "v2.sqlite*", "metadata/transcript/search observations"),
    ("skeletons", "skeletons", "frozen corpus revisions"),
    ("vectors", "vectors", "corpus/visual index revisions"),
    ("models", "models", "local embedding models"),
    ("frames", "frames", "extracted frames"),
    ("audio", "audio", "extracted audio"),
    ("edit_plans", "edit-plans", "clip-plan revisions"),
    ("editor_clips", "editor-clips", "materialized editor clips"),
)
_TABLES = ("transcripts", "video_meta", "search_results")


def _tree_usage(path: Path) -> tuple[int, int]:
    total = 0
    files = 0
    if path.is_file():
        return path.stat().st_size, 1
    if not path.is_dir():
        return 0, 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
                files += 1
            except OSError:
                continue
    return total, files


def _quota() -> int | None:
    raw = os.environ.get("YOUTUBE_MCP_CACHE_QUOTA_BYTES", "").strip()
    try:
        value = int(raw) if raw else None
    except ValueError:
        return None
    return value if value and value > 0 else None


def _sqlite_status(db: Path) -> dict[str, Any]:
    if not db.is_file():
        return {"exists": False}
    status: dict[str, Any] = {"exists": True, "tables": {}}
    try:
        # Read-only URI: never creates or migrates the cache.
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    except sqlite3.Error as exc:
        return {"exists": True, "error": type(exc).__name__}
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in _TABLES:
            if table in names:
                status["tables"][table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        status["quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as exc:
        status["error"] = type(exc).__name__
    finally:
        conn.close()
    return status


def run_cache_status(cache_dir: Path | None = None) -> dict[str, Any]:
    root = paths.CACHE_DIR if cache_dir is None else cache_dir
    areas: dict[str, Any] = {}
    total_bytes = 0
    total_files = 0
    for key, pattern, description in _AREAS:
        if "*" in pattern:
            size = files = 0
            for match in root.glob(pattern) if root.is_dir() else []:
                s, f = _tree_usage(match)
                size += s
                files += f
        else:
            size, files = _tree_usage(root / pattern)
        areas[key] = {"bytes": size, "files": files, "description": description}
        total_bytes += size
        total_files += files

    checks: list[Check] = []
    sqlite_state = _sqlite_status(root / "v2.sqlite")
    if not root.exists():
        checks.append(Check("cache:root", "info", "cache root does not exist yet (created on first use)"))
    else:
        checks.append(Check("cache:root", "pass", "cache root present"))
    if sqlite_state.get("error") or sqlite_state.get("quick_check") not in (None, "ok"):
        checks.append(Check("cache:sqlite", "fail", "SQLite cache failed its integrity check", {"error": sqlite_state.get("error")}))
    elif sqlite_state.get("exists"):
        checks.append(Check("cache:sqlite", "pass", "SQLite cache readable and passes quick_check"))

    probe = root if root.exists() else next((p for p in root.parents if p.exists()), None)
    disk: dict[str, Any] | None = None
    if probe is not None:
        usage = shutil.disk_usage(probe)
        disk = {"total_bytes": usage.total, "free_bytes": usage.free}
        if usage.free < LOW_DISK_BYTES:
            checks.append(Check("cache:disk", "warn", "less than 1 GiB free on the cache volume", disk))
        else:
            checks.append(Check("cache:disk", "pass", "cache volume has free space", disk))

    quota = _quota()
    quota_state: dict[str, Any] = {"quota_bytes": quota, "used_bytes": total_bytes}
    if quota is not None:
        quota_state["used_fraction"] = round(total_bytes / quota, 4)
        if total_bytes > quota:
            checks.append(Check("cache:quota", "warn", "cache usage exceeds YOUTUBE_MCP_CACHE_QUOTA_BYTES", quota_state))
        else:
            checks.append(Check("cache:quota", "pass", "cache usage within quota", quota_state))

    failures = [c for c in checks if c.status == "fail"]
    warnings = [c for c in checks if c.status == "warn"]
    return {
        "report": "cache",
        "status": "fail" if failures else "ok",
        "cache_root": str(root),
        "total_bytes": total_bytes,
        "total_files": total_files,
        "areas": areas,
        "sqlite": sqlite_state,
        "disk": disk,
        "quota": quota_state,
        "summary": {"failures": len(failures), "warnings": len(warnings)},
        "checks": [c.as_dict() for c in checks],
    }


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def render_text(report: dict[str, Any]) -> str:
    lines = [f"cache root: {report['cache_root']}", f"total: {_human(report['total_bytes'])} in {report['total_files']} files", ""]
    for key, area in report["areas"].items():
        lines.append(f"  {key:<13} {_human(area['bytes']):>10}  {area['files']:>6} files  ({area['description']})")
    tables = (report.get("sqlite") or {}).get("tables") or {}
    if tables:
        lines.append("")
        lines.append("  rows: " + ", ".join(f"{k}={v}" for k, v in tables.items()))
    quota = report["quota"]
    if quota.get("quota_bytes"):
        lines.append(f"  quota: {_human(quota['used_bytes'])} of {_human(quota['quota_bytes'])}")
    lines.append("")
    glyph = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "info": "INFO"}
    for check in report["checks"]:
        lines.append(f"[{glyph[check['status']]}] {check['message']}")
    lines.append(f"overall: {report['status'].upper()}")
    return "\n".join(lines) + "\n"
