from __future__ import annotations

from youtube_mcp_v2.adapters import channel_scrape
from youtube_mcp_v2.tools import skeleton_tools


class _EmptyChannelResponse:
    text = "<html><body>no parseable videos</body></html>"

    def raise_for_status(self) -> None:
        return None


class _EmptyChannelClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __enter__(self) -> "_EmptyChannelClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str) -> _EmptyChannelResponse:
        return _EmptyChannelResponse()


def test_empty_channel_page_uses_ytdlp_fallback(monkeypatch) -> None:
    fallback_calls: list[tuple[str, int]] = []

    def fake_ytdlp(value: str, limit: int) -> dict[str, object]:
        fallback_calls.append((value, limit))
        return {
            "channel": {"id": "UC123", "handle": "@creator", "title": "Creator"},
            "videos": [{"id": "abc123", "title": "Useful advice"}],
        }

    monkeypatch.setattr(channel_scrape.httpx, "Client", _EmptyChannelClient)
    monkeypatch.setattr(
        channel_scrape,
        "_fetch_channel_uploads_with_ytdlp",
        fake_ytdlp,
        raising=False,
    )

    result = channel_scrape.fetch_channel_uploads("@creator", limit=3)

    assert fallback_calls == [("@creator", 3)]
    assert result["source"] == "yt-dlp"
    assert result["videos"] == [{"id": "abc123", "title": "Useful advice"}]


def test_channel_builder_preserves_ytdlp_provenance(monkeypatch) -> None:
    monkeypatch.setattr(
        skeleton_tools.isolation,
        "run_isolated",
        lambda *args, **kwargs: {
            "source": "yt-dlp",
            "channel": {"id": "UC123", "handle": "@creator", "title": "Creator"},
            "videos": [],
        },
    )

    _raw, source = skeleton_tools._build_channel("@creator", 3)

    assert source == "yt-dlp"
