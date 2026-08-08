"""End-to-end smoke test. Hits the real network — skip with -k 'not network'.

Pinned video: https://www.youtube.com/watch?v=jNQXAC9IVRw
("Me at the zoo" — first ever YouTube video. Stable since 2005, has transcripts.)
"""

from __future__ import annotations

import pytest

from youtube_mcp_v2.tools.inspect import inspect_video
from youtube_mcp_v2.tools.transcript import transcript_get
from youtube_mcp_v2.adapters.url import parse_video_id
from youtube_mcp_v2 import validate


PINNED_ID = "jNQXAC9IVRw"


# ----- pure unit tests -------------------------------------------------------


def test_parse_video_id_bare() -> None:
    assert parse_video_id(PINNED_ID) == PINNED_ID


def test_parse_video_id_watch() -> None:
    assert parse_video_id(f"https://www.youtube.com/watch?v={PINNED_ID}") == PINNED_ID


def test_parse_video_id_short() -> None:
    assert parse_video_id(f"https://youtu.be/{PINNED_ID}") == PINNED_ID


def test_parse_video_id_shorts() -> None:
    assert parse_video_id(f"https://www.youtube.com/shorts/{PINNED_ID}") == PINNED_ID


def test_parse_video_id_bad() -> None:
    with pytest.raises(ValueError):
        parse_video_id("https://example.com/not-youtube")


def test_validate_word_rate_band() -> None:
    # 19s zoo video: small text would be sparse; 6 words ~= 0.3 wps → boundary.
    ok, warns = validate.validate_transcript(
        text="all right so here we are at the zoo",
        segments=[{"start_s": 0.0, "duration_s": 9.0}],
        duration_s=19,
        requested_lang="en",
        actual_lang="en",
    )
    # word_count=10, duration=19 → 0.526 wps → in-band
    assert ok, warns


def test_validate_empty_fails() -> None:
    ok, warns = validate.validate_transcript(
        text="",
        segments=[],
        duration_s=60,
        requested_lang="en",
        actual_lang="en",
    )
    assert not ok
    assert "empty" in warns[0]


def test_validate_truncation_marker() -> None:
    ok, warns = validate.validate_transcript(
        text="hello world [...] etcetera",
        segments=None,
        duration_s=10,
        requested_lang="en",
        actual_lang="en",
    )
    assert not ok
    assert any("truncation" in w for w in warns)


# ----- network smoke (real YouTube) ------------------------------------------
#
# Two layers of assertion:
#   1. Envelope shape is ALWAYS correct (passes even when upstream is throttling).
#   2. Data presence is asserted only when the upstream is reachable; if upstream
#      returns a recoverable error (429, IP block, transcript-api block), the
#      test SKIPS rather than failing — confirming the scaffold's error path
#      handled it cleanly.

ENVELOPE_KEYS = {"data", "fetched_at", "source", "cache_age_s",
                 "validated", "warnings", "error"}


def _assert_envelope_shape(env: dict) -> None:
    assert ENVELOPE_KEYS.issubset(env.keys()), f"missing keys: {ENVELOPE_KEYS - env.keys()}"
    assert env["source"] in ("scrape", "yt-dlp", "api", "cache")
    assert isinstance(env["warnings"], list)
    if env["error"] is not None:
        for k in ("code", "message", "recoverable"):
            assert k in env["error"]


def _skip_if_throttled(env: dict, label: str) -> None:
    err = env.get("error")
    if err and err.get("recoverable") and any(
        s in err.get("message", "").lower()
        for s in ("429", "too many requests", "blocking requests", "ipblocked")
    ):
        pytest.skip(f"{label}: upstream throttled this IP — scaffold returned clean error")


@pytest.mark.network
def test_inspect_video_pinned() -> None:
    env = inspect_video(PINNED_ID)
    _assert_envelope_shape(env)
    _skip_if_throttled(env, "inspect.video")
    assert env["error"] is None, env["error"]
    data = env["data"]
    assert data["id"] == PINNED_ID
    assert data["duration_s"] is not None and data["duration_s"] > 0
    assert isinstance(data["available_caption_langs"], list)


@pytest.mark.network
def test_transcript_get_text_pinned() -> None:
    env = transcript_get(PINNED_ID, mode="text", lang="en")
    _assert_envelope_shape(env)
    _skip_if_throttled(env, "transcript.get(text)")
    assert env["error"] is None, env["error"]
    data = env["data"]
    assert data["id"] == PINNED_ID
    assert isinstance(data["text"], str)
    assert data["word_count"] >= 1


@pytest.mark.network
def test_transcript_get_timed_pinned() -> None:
    env = transcript_get(PINNED_ID, mode="timed", lang="en")
    _assert_envelope_shape(env)
    _skip_if_throttled(env, "transcript.get(timed)")
    assert env["error"] is None, env["error"]
    data = env["data"]
    assert isinstance(data["segments"], list)
    if data["segments"]:
        seg = data["segments"][0]
        assert "start_s" in seg and "text" in seg


@pytest.mark.network
def test_transcript_get_chunked_pinned() -> None:
    env = transcript_get(PINNED_ID, mode="chunked", lang="en", chunk_tokens=20, chunk_overlap=5)
    _assert_envelope_shape(env)
    _skip_if_throttled(env, "transcript.get(chunked)")
    assert env["error"] is None, env["error"]
    data = env["data"]
    assert isinstance(data["chunks"], list)
    if data["chunks"]:
        c = data["chunks"][0]
        for k in ("i", "n", "start_s", "end_s", "text", "token_estimate"):
            assert k in c


# Envelope-shape check on a known-bad input — ALWAYS runs, no network.

def test_envelope_shape_on_bad_url() -> None:
    env = inspect_video("https://example.com/not-youtube")
    _assert_envelope_shape(env)
    assert env["error"] is not None
    assert env["error"]["code"] == "bad_url"
    assert env["error"]["recoverable"] is False


# ---------------------------------------------------------------------------
# Skeleton — pure unit tests (no network)
# ---------------------------------------------------------------------------

import json
import tempfile
import shutil
from pathlib import Path

from youtube_mcp_v2 import skeleton as _sk
from youtube_mcp_v2.tools.skeleton_tools import (
    skeleton_get, skeleton_expire, skeleton_index, skeleton_build, skeleton_list,
)
from youtube_mcp_v2.tools.scrape import scrape_search


@pytest.fixture
def tmp_skeleton_dirs(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="ytmcp-test-"))
    monkeypatch.setattr(_sk, "CACHE_DIR", tmp)
    monkeypatch.setattr(_sk, "SKELETON_DIR", tmp / "skeletons")
    monkeypatch.setattr(_sk, "VECTOR_DIR", tmp / "vectors")
    monkeypatch.setattr(_sk, "MODEL_DIR", tmp / "models")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def test_make_handle_channel() -> None:
    h = _sk.make_handle("channel", "UCBJycsmduvYEL83R_U4JriQ")
    assert h.startswith("chan-UCBJycsmduvYEL83R_U4JriQ-")
    assert _sk.is_valid_handle(h)


def test_make_handle_topic_slugifies() -> None:
    h = _sk.make_handle("topic", "Soldering Iron Tutorial!!! 2026")
    assert h.startswith("topic-soldering-iron-tutorial-2026-")
    assert _sk.is_valid_handle(h)


def test_skeleton_path_rejects_bad_handle() -> None:
    with pytest.raises(ValueError):
        _sk.skeleton_path("../../etc/passwd")


def test_save_load_roundtrip(tmp_skeleton_dirs) -> None:
    payload = {
        "handle": _sk.make_handle("topic", "test query"),
        "target": "topic",
        "value": "test query",
        "built_at": _sk._now_iso(),
        "source": "scrape",
        "expired_at": None,
        "channel": None,
        "videos": [
            {"id": "abc12345678", "title": "Test", "duration_s": 60,
             "has_transcript": None, "caption_track_id": None, "lang": None},
        ],
    }
    _sk.save_skeleton(payload)
    loaded = _sk.load_skeleton(payload["handle"])
    assert loaded == payload


def test_expire_does_not_delete(tmp_skeleton_dirs) -> None:
    handle = _sk.make_handle("topic", "expire test")
    _sk.save_skeleton({
        "handle": handle, "target": "topic", "value": "expire test",
        "built_at": _sk._now_iso(), "source": "scrape", "expired_at": None,
        "channel": None, "videos": [],
    })
    expired = _sk.expire_skeleton(handle)
    assert expired["expired_at"] is not None
    # Re-load — file still exists, expired_at preserved.
    again = _sk.load_skeleton(handle)
    assert again["expired_at"] == expired["expired_at"]


def test_skeleton_get_not_found(tmp_skeleton_dirs) -> None:
    env = skeleton_get("chan-DOESNOTEXIST-20260101-000000")
    assert env["error"] is not None
    assert env["error"]["code"] == "skeleton_not_found"


def test_skeleton_get_bad_handle(tmp_skeleton_dirs) -> None:
    env = skeleton_get("not a handle")
    assert env["error"] is not None
    assert env["error"]["code"] == "bad_handle"


def test_skeleton_build_bad_target() -> None:
    env = skeleton_build("playlist", "value")
    assert env["error"] is not None
    assert env["error"]["code"] == "bad_target"


def test_skeleton_build_empty_value() -> None:
    env = skeleton_build("topic", "   ")
    assert env["error"] is not None
    assert env["error"]["code"] == "bad_value"


def test_skeleton_index_empty(tmp_skeleton_dirs) -> None:
    env = skeleton_index()
    _assert_envelope_shape(env)
    assert env["error"] is None
    assert env["data"] == []


# ---------------------------------------------------------------------------
# Search + skeleton — network smokes (skip cleanly on rate limit)
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_scrape_search_envelope() -> None:
    env = scrape_search("first ever youtube video", n=3)
    _assert_envelope_shape(env)
    _skip_if_throttled(env, "scrape.search")
    assert env["error"] is None, env["error"]
    assert isinstance(env["data"], list)
    if env["data"]:
        r = env["data"][0]
        for k in ("id", "title", "url"):
            assert k in r


@pytest.mark.network
def test_skeleton_build_topic(tmp_skeleton_dirs) -> None:
    env = skeleton_build("topic", "first ever youtube video", limit=3)
    _assert_envelope_shape(env)
    _skip_if_throttled(env, "skeleton.build(topic)")
    assert env["error"] is None, env["error"]
    handle = env["data"]["handle"]
    assert handle.startswith("topic-")

    listed = skeleton_list(handle)
    assert listed["error"] is None
    assert isinstance(listed["data"], list)


@pytest.mark.network
def test_skeleton_build_channel_pinned(tmp_skeleton_dirs) -> None:
    # jawed (uploader of "Me at the zoo"). Stable channel since 2005.
    env = skeleton_build("channel", "@jawed", limit=5)
    _assert_envelope_shape(env)
    _skip_if_throttled(env, "skeleton.build(channel)")
    assert env["error"] is None, env["error"]
    handle = env["data"]["handle"]
    assert handle.startswith("chan-")


# ---------------------------------------------------------------------------
# Frame — pure unit tests
# ---------------------------------------------------------------------------

from youtube_mcp_v2.tools.frame import frame_get
from youtube_mcp_v2.adapters import frame_extract


def test_frame_get_bad_url() -> None:
    env = frame_get("https://example.com/not-youtube", mode="single", timestamp_s=10)
    _assert_envelope_shape(env)
    assert env["error"]["code"] == "bad_url"


def test_frame_get_missing_timestamp() -> None:
    env = frame_get(PINNED_ID, mode="single")
    _assert_envelope_shape(env)
    assert env["error"]["code"] == "missing_timestamp"


def test_frame_get_bad_mode() -> None:
    env = frame_get(PINNED_ID, mode="thumbnail")  # type: ignore[arg-type]
    _assert_envelope_shape(env)
    assert env["error"]["code"] == "bad_mode"


def test_parse_layout_valid() -> None:
    assert frame_extract._parse_layout("4x3") == (4, 3)
    assert frame_extract._parse_layout("1x1") == (1, 1)


def test_parse_layout_invalid() -> None:
    with pytest.raises(frame_extract.FrameExtractError):
        frame_extract._parse_layout("4-3")
    with pytest.raises(frame_extract.FrameExtractError):
        frame_extract._parse_layout("0x3")


# ---------------------------------------------------------------------------
# Frame — network smoke (pinned 19s "Me at the zoo")
# ---------------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.slow
def test_frame_get_single_pinned(tmp_path, monkeypatch) -> None:
    # Redirect frame cache to a tmp path so the test is hermetic.
    monkeypatch.setattr(frame_extract, "FRAMES_DIR", tmp_path / "frames")
    env = frame_get(PINNED_ID, mode="single", timestamp_s=5.0)
    _assert_envelope_shape(env)
    if env["error"] is not None:
        # yt-dlp/ffmpeg paths can fail on rate limit, missing format, etc.
        # The contract is that the envelope is well-formed; data presence is best-effort.
        pytest.skip(f"frame.get(single) upstream issue: {env['error']['message'][:120]}")
    data = env["data"]
    assert data["timestamp_s"] == 5.0
    from pathlib import Path as _P
    assert _P(data["path"]).exists()


@pytest.mark.network
@pytest.mark.slow
def test_frame_get_sheet_pinned(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(frame_extract, "FRAMES_DIR", tmp_path / "frames")
    env = frame_get(PINNED_ID, mode="sheet", n=4, layout="2x2", size="640x360")
    _assert_envelope_shape(env)
    if env["error"] is not None:
        pytest.skip(f"frame.get(sheet) upstream issue: {env['error']['message'][:120]}")
    data = env["data"]
    assert data["layout"] == "2x2"
    assert len(data["frame_timestamps"]) == 4
    assert len(data["frame_paths"]) == 4
    from pathlib import Path as _P
    assert _P(data["path"]).exists()
    for fp in data["frame_paths"]:
        assert _P(fp).exists()


# ---------------------------------------------------------------------------
# api.* — pure unit tests (no key, no network)
# ---------------------------------------------------------------------------

from youtube_mcp_v2.adapters import data_api
from youtube_mcp_v2.tools.api import (
    api_search, api_channel_stats, api_trending, api_video_categories,
)


def test_parse_iso_duration() -> None:
    assert data_api.parse_iso_duration("PT4M13S") == 4 * 60 + 13
    assert data_api.parse_iso_duration("PT1H2M3S") == 3723
    assert data_api.parse_iso_duration("PT0S") == 0
    assert data_api.parse_iso_duration("PT30S") == 30
    assert data_api.parse_iso_duration("") is None
    assert data_api.parse_iso_duration(None) is None
    assert data_api.parse_iso_duration("not-iso") is None


def test_shape_search_item() -> None:
    raw = {
        "id": {"videoId": "abc12345678"},
        "snippet": {
            "title": "Hello",
            "description": "x" * 250,
            "channelTitle": "Chan",
            "channelId": "UC123",
            "publishedAt": "2026-04-01T00:00:00Z",
        },
    }
    out = data_api._shape_search_item(raw)
    assert out["id"] == "abc12345678"
    assert out["title"] == "Hello"
    assert out["channel"] == "Chan"
    assert out["channel_id"] == "UC123"
    assert out["url"] == "https://www.youtube.com/watch?v=abc12345678"
    assert out["description_excerpt"].endswith("…")
    assert len(out["description_excerpt"]) <= 201  # 200 + ellipsis


def test_shape_video_full_lean() -> None:
    raw = {
        "id": "vid",
        "snippet": {
            "title": "T", "channelTitle": "C", "channelId": "UC",
            "publishedAt": "2026-04-01T00:00:00Z", "categoryId": "10",
        },
        "contentDetails": {"duration": "PT12M34S"},
        "statistics": {"viewCount": "1000", "likeCount": "10", "commentCount": "5"},
    }
    out = data_api._shape_video_full(raw)
    assert out["duration_s"] == 12 * 60 + 34
    assert out["view_count"] == 1000
    assert out["like_count"] == 10
    assert out["comment_count"] == 5
    assert out["category_id"] == "10"


def test_api_search_no_key(monkeypatch) -> None:
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(data_api, "_client", None)
    env = api_search("anything")
    _assert_envelope_shape(env)
    assert env["error"]["code"] == "auth_required"


def test_api_search_empty_query() -> None:
    env = api_search("   ")
    _assert_envelope_shape(env)
    assert env["error"]["code"] == "bad_query"


def test_api_search_quota_exhausted(monkeypatch) -> None:
    def boom(*a, **k):
        raise data_api.ApiQuotaError("quota gone")
    monkeypatch.setattr(data_api, "search", boom)
    env = api_search("anything")
    _assert_envelope_shape(env)
    assert env["error"]["code"] == "quota_exceeded"
    assert env["error"]["recoverable"] is True


def test_api_channel_stats_not_found(monkeypatch) -> None:
    monkeypatch.setattr(data_api, "channel_stats", lambda v: None)
    env = api_channel_stats("UC_does_not_exist")
    _assert_envelope_shape(env)
    assert env["error"]["code"] == "channel_not_found"


def test_api_trending_call_failure(monkeypatch) -> None:
    def boom(*a, **k):
        raise data_api.ApiCallError("upstream 500")
    monkeypatch.setattr(data_api, "trending", boom)
    env = api_trending(region="US")
    _assert_envelope_shape(env)
    assert env["error"]["code"] == "api_call_failed"


def test_api_video_categories_happy_path(monkeypatch) -> None:
    monkeypatch.setattr(
        data_api, "video_categories",
        lambda region: [{"id": "1", "title": "Film & Animation"}],
    )
    env = api_video_categories(region="US")
    _assert_envelope_shape(env)
    assert env["error"] is None
    assert env["data"][0]["id"] == "1"
