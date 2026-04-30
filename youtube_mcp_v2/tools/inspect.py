"""inspect.* — pre-flight tools. Cheap. Run before expensive calls.

USE WHEN: you have a video URL/id and want to know capabilities (transcript? language?
duration? age-gated?) before deciding which heavy tool to call next.
"""

from __future__ import annotations

from typing import Any

from .. import cache, envelope
from ..adapters import page as page_adapter
from ..adapters.url import parse_video_id


def inspect_video(url_or_id: str) -> dict[str, Any]:
    """Pre-flight check on a YouTube video.

    USE WHEN: you need to know whether a video has transcripts, what language(s)
    they're in, the duration, and whether it's age-gated/livestream — before calling
    transcript.get or frame.get.
    DO NOT USE WHEN: you already have this metadata cached from a recent call.
    OUTPUT SHAPE: { id, title, duration_s, channel, lang_default,
                    available_caption_langs[], has_transcript, age_gated,
                    embed_allowed, livestream }
    """
    try:
        video_id = parse_video_id(url_or_id)
    except ValueError as e:
        return envelope.fail("bad_url", str(e), recoverable=False)

    cached = cache.get_video_meta(video_id)
    if cached is not None:
        payload, age = cached
        return envelope.ok(payload, source="cache", cache_age_s=age)

    try:
        wp = page_adapter.fetch_watch_page(video_id)
    except Exception as e:  # network/parse errors return clean failure, not crash
        return envelope.fail(
            "watch_page_fetch_failed",
            f"could not fetch watch page for {video_id}: {e}",
        )

    payload = {
        "id": wp.video_id,
        "title": wp.title,
        "channel": wp.channel,
        "channel_id": wp.channel_id,
        "duration_s": wp.duration_s,
        "view_count": wp.view_count,
        "publish_date": wp.publish_date,
        "thumbnail_url": wp.thumbnail_url,
        "lang_default": (wp.available_caption_langs[0] if wp.available_caption_langs else None),
        "available_caption_langs": wp.available_caption_langs,
        "has_transcript": wp.has_transcript,
        "age_gated": wp.age_gated,
        "embed_allowed": wp.embed_allowed,
        "livestream": wp.livestream,
    }

    cache.put_video_meta(video_id, payload)
    return envelope.ok(payload, source="scrape")
