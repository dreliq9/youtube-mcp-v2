from __future__ import annotations

import pytest

from youtube_mcp_v2.adapters.url import parse_video_id


VIDEO_ID = "jNQXAC9IVRw"


def test_youtu_be_candidate_must_be_valid_video_id() -> None:
    with pytest.raises(ValueError):
        parse_video_id("https://youtu.be/short")


def test_watch_query_candidate_must_be_valid_video_id() -> None:
    with pytest.raises(ValueError):
        parse_video_id("https://www.youtube.com/watch?v=short")


def test_path_candidate_must_be_valid_video_id() -> None:
    with pytest.raises(ValueError):
        parse_video_id("https://www.youtube.com/shorts/not-an-id")


def test_youtube_like_untrusted_hostname_is_not_accepted() -> None:
    with pytest.raises(ValueError):
        parse_video_id(f"https://youtube.example.com/watch?v={VIDEO_ID}")


def test_unrelated_hostname_containing_youtube_is_not_accepted() -> None:
    with pytest.raises(ValueError):
        parse_video_id(f"https://notyoutube.com/watch?v={VIDEO_ID}")


def test_youtube_nocookie_embed_is_supported() -> None:
    assert (
        parse_video_id(f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}")
        == VIDEO_ID
    )


def test_music_youtube_watch_is_supported() -> None:
    assert parse_video_id(f"https://music.youtube.com/watch?v={VIDEO_ID}") == VIDEO_ID


def test_query_noise_does_not_change_video_id() -> None:
    assert (
        parse_video_id(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list=PL123&t=42s"
        )
        == VIDEO_ID
    )
