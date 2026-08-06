from __future__ import annotations

from youtube_mcp_v2 import cache
from youtube_mcp_v2.tools import scrape as scrape_tool


def _redirect_cache(tmp_path, monkeypatch) -> None:
    cache_dir = tmp_path / "youtube-mcp"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_PATH", cache_dir / "v2.sqlite")


def _results(n: int) -> list[dict]:
    return [
        {
            "id": f"video{i:06d}"[-11:],
            "title": f"Video {i}",
            "url": f"https://www.youtube.com/watch?v=video{i:06d}"[-43:],
        }
        for i in range(n)
    ]


def test_scrape_search_reuses_cached_larger_revision(tmp_path, monkeypatch) -> None:
    _redirect_cache(tmp_path, monkeypatch)
    calls: list[tuple[str, int]] = []

    def fake_isolated(_target: str, query: str, n: int, timeout_s: int):
        calls.append((query, n))
        return _results(n)

    monkeypatch.setattr(scrape_tool.isolation, "run_isolated", fake_isolated)

    first = scrape_tool.scrape_search("battery research", n=5)
    second = scrape_tool.scrape_search("battery research", n=3)

    assert first["source"] == "scrape"
    assert second["source"] == "cache"
    assert len(second["data"]) == 3
    assert calls == [("battery research", 5)]


def test_scrape_search_refetches_when_cached_revision_is_too_small(
    tmp_path, monkeypatch
) -> None:
    _redirect_cache(tmp_path, monkeypatch)
    calls: list[int] = []

    def fake_isolated(_target: str, _query: str, n: int, timeout_s: int):
        calls.append(n)
        return _results(n)

    monkeypatch.setattr(scrape_tool.isolation, "run_isolated", fake_isolated)

    scrape_tool.scrape_search("battery research", n=2)
    larger = scrape_tool.scrape_search("battery research", n=4)

    assert larger["source"] == "scrape"
    assert len(larger["data"]) == 4
    assert calls == [2, 4]

    with cache.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM search_results WHERE query = ?",
            ("battery research",),
        ).fetchone()["n"]
    assert count == 2  # append-only: both revisions remain queryable


def test_search_cache_normalizes_outer_whitespace(tmp_path, monkeypatch) -> None:
    _redirect_cache(tmp_path, monkeypatch)
    calls = 0

    def fake_isolated(_target: str, query: str, n: int, timeout_s: int):
        nonlocal calls
        calls += 1
        assert query == "battery research"
        return _results(n)

    monkeypatch.setattr(scrape_tool.isolation, "run_isolated", fake_isolated)

    first = scrape_tool.scrape_search("  battery research  ", n=3)
    second = scrape_tool.scrape_search("battery research", n=3)

    assert first["source"] == "scrape"
    assert second["source"] == "cache"
    assert calls == 1


def test_get_search_results_requires_enough_rows(tmp_path, monkeypatch) -> None:
    _redirect_cache(tmp_path, monkeypatch)
    cache.put_search_results("query", _results(2))

    assert cache.get_search_results("query", min_results=2) is not None
    assert cache.get_search_results("query", min_results=3) is None


def test_empty_search_query_is_never_cached(tmp_path, monkeypatch) -> None:
    _redirect_cache(tmp_path, monkeypatch)
    env = scrape_tool.scrape_search("   ", n=3)
    assert env["error"]["code"] == "bad_query"
    assert not cache.CACHE_PATH.exists()
