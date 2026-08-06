"""scrape.* — no-API-key search via page scraping."""

from __future__ import annotations

from typing import Any

from .. import cache, envelope, isolation


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

    normalized = query.strip()
    n = max(1, min(int(n), 50))

    cached = cache.get_search_results(normalized, min_results=n)
    if cached is not None:
        results, age = cached
        return envelope.ok(
            results[:n],
            source="cache",
            cache_age_s=age,
        )

    try:
        results = isolation.run_isolated(
            "youtube_mcp_v2.adapters.search_scrape.search_videos",
            normalized,
            n,
            timeout_s=20,
        )
    except TimeoutError as e:
        return envelope.fail("search_scrape_timeout", str(e))
    except RuntimeError as e:
        return envelope.fail("search_scrape_failed", str(e))

    cache.put_search_results(normalized, results)
    return envelope.ok(results, source="scrape")
