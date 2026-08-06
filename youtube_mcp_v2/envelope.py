"""Standard response envelope.

v0.2 consumers rely on the original seven top-level fields. v0.3 can add optional
provenance without removing or changing those fields, providing a compatibility
bridge toward the Evidence Envelope described in EVIDENCE_MODEL.md.
"""

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
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "data": data,
        "fetched_at": now_utc(),
        "source": source,
        "cache_age_s": cache_age_s,
        "validated": validated,
        "warnings": warnings or [],
        "error": None,
    }
    if provenance is not None:
        result["provenance"] = provenance
    return result


def fail(
    code: str,
    message: str,
    *,
    recoverable: bool = True,
    source: Literal["scrape", "api", "cache"] = "scrape",
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
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
    if provenance is not None:
        result["provenance"] = provenance
    return result
