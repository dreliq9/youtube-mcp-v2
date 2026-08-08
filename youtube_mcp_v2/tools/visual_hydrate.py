"""MCP wrapper for bounded/resumable corpus visual hydration."""

from __future__ import annotations

from typing import Any, Literal

from .. import envelope, visual_hydration


def corpus_visual_hydrate(
    handle: str,
    cursor: str | None = None,
    batch_size: int = visual_hydration.DEFAULT_BATCH_SIZE,
    max_workers: int = visual_hydration.DEFAULT_MAX_WORKERS,
    n: int = visual_hydration.DEFAULT_FRAME_COUNT,
    layout: str = "4x3",
    size: str = "1280x720",
    fmt: Literal["png", "jpg"] = "png",
    policy: Literal["missing"] = "missing",
) -> dict[str, Any]:
    """Acquire one bounded/resumable batch of timestamped corpus frames.

    The durable output is the managed frame cache. The MCP response contains only
    compact progress/status and timestamp spans; it intentionally omits local
    paths and image bytes. `corpus.visual_search` can later freeze/index those
    cached frames into content-addressed evidence artifacts.
    """
    try:
        result = visual_hydration.hydrate_visuals(
            handle,
            cursor=cursor,
            batch_size=batch_size,
            max_workers=max_workers,
            n=n,
            layout=layout,
            size=size,
            fmt=fmt,
            policy=policy,
        )
    except FileNotFoundError as exc:
        return envelope.fail("corpus_not_found", str(exc), recoverable=False)
    except visual_hydration.VisualHydrationError as exc:
        return envelope.fail("visual_hydrate_failed", str(exc), recoverable=False)
    except Exception as exc:
        return envelope.fail("visual_hydrate_failed", str(exc))

    warnings: list[str] = []
    if result["failed"]:
        warnings.append(
            f"{result['failed']} visual acquisition attempt(s) failed in this batch; "
            "restart from cursor 0 later to retry unresolved/under-covered members "
            "while sufficiently cached videos are skipped"
        )
    if not result["complete"]:
        warnings.append(
            "visual hydration is incomplete; continue with next_cursor before "
            "assuming corpus-wide visual coverage"
        )

    return envelope.ok(
        result,
        source="cache",
        validated=(result["failed"] == 0),
        warnings=warnings,
    )
