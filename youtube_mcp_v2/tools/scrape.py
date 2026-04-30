"""scrape.* — no-API-key search via page scraping."""

from __future__ import annotations

from typing import Any

from .. import envelope, isolation


def scrape_search(query: str, n: int = 10) -> dict[str, Any]:
    """Search YouTube via page scraping. No API key needed.

    USE WHEN: discovering videos by topic without a YOUTUBE_API_KEY.
    DO NOT USE WHEN: YOUTUBE_API_KEY is set — call api.search instead for richer
                     fields (channel ids, dates, no rate-limit quirks).
    OUTPUT SHAPE: envelope wrapping list of {id, title, channel, channel_id,
                  duration, views, published, url}.
    """
    if not query or not query.strip():
        return envelope.fail("bad_query", "query is empty", recoverable=False)
    n = max(1, min(int(n), 50))

    try:
        results = isolation.run_isolated(
            "youtube_mcp_v2.adapters.search_scrape.search_videos",
            query, n,
            timeout_s=20,
        )
    except TimeoutError as e:
        return envelope.fail("search_scrape_timeout", str(e))
    except RuntimeError as e:
        return envelope.fail("search_scrape_failed", str(e))

    return envelope.ok(results, source="scrape")
