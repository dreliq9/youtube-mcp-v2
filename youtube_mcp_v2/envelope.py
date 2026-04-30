"""Standard response envelope. Every tool returns this shape."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ok(
    data: Any,
    *,
    source: Literal["scrape", "api", "cache"] = "scrape",
    cache_age_s: int = 0,
    validated: bool = True,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "data": data,
        "fetched_at": now_utc(),
        "source": source,
        "cache_age_s": cache_age_s,
        "validated": validated,
        "warnings": warnings or [],
        "error": None,
    }


def fail(
    code: str,
    message: str,
    *,
    recoverable: bool = True,
    source: Literal["scrape", "api", "cache"] = "scrape",
) -> dict[str, Any]:
    return {
        "data": None,
        "fetched_at": now_utc(),
        "source": source,
        "cache_age_s": 0,
        "validated": False,
        "warnings": [],
        "error": {
            "code": code,
            "message": message,
            "recoverable": recoverable,
        },
    }
