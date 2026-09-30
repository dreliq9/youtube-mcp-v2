"""Deterministic tests for immutable corpus revisions (issue #3). No network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from youtube_mcp_v2 import cache, corpus_index, corpus_revision, envelope, skeleton
from youtube_mcp_v2.adapters import channel_scrape, data_api
from youtube_mcp_v2.tools import corpus_revisions as cr
from youtube_mcp_v2.tools import skeleton_tools
from youtube_mcp_v2.tools.corpus import corpus_search
from youtube_mcp_v2.tools.corpus_compose import corpus_compose

VID_A = "jNQXAC9IVRw"
VID_B = "dQw4w9WgXcQ"
VID_C = "aqz-KE-bpKQ"
VID_D = "9bZkp7q19f0"
CHANNEL = "UCBJycsmduvYEL83R_U4JriQ"
PLAYLIST = "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


@pytest.fixture
def storage(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "youtube-mcp"
    monkeypatch.setattr(cache, "CACHE_DIR", root)
    monkeypatch.setattr(cache, "CACHE_PATH", root / "v2.sqlite")
    monkeypatch.setattr(skeleton, "CACHE_DIR", root)
    monkeypatch.setattr(skeleton, "SKELETON_DIR", root / "skeletons")
    monkeypatch.setattr(skeleton, "VECTOR_DIR", root / "vectors")
    monkeypatch.setattr(skeleton, "MODEL_DIR", root / "models")
    monkeypatch.setattr(corpus_index, "VECTOR_DIR", root / "vectors")
    monkeypatch.setattr(corpus_index, "INDEX_ROOT", root / "vectors" / "corpus")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    return root


def _video(vid: str, title: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": title,
        "duration_s": 60,
        "published": "2 days ago",
        "view_count": 100,
        "thumbnail_url": f"https://i.ytimg.com/vi/{vid}/hq.jpg",
        **extra,
    }


class FakeIsolation:
    """Stands in for isolation.run_isolated; returns scripted upstream listings."""

    def __init__(self) -> None:
        self.responses: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def script(self, dotted_suffix: str, *responses: Any) -> None:
        self.responses[dotted_suffix] = list(responses)

    def __call__(self, dotted: str, *args: Any, timeout_s: float = 0) -> Any:
        self.calls.append((dotted, args))
        for suffix, queue in self.responses.items():
            if dotted.endswith(suffix):
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, BaseException):
                    raise item
                return item
        raise AssertionError(f"unexpected isolated call {dotted}")


@pytest.fixture
def upstream(monkeypatch) -> FakeIsolation:
    fake = FakeIsolation()
    monkeypatch.setattr(cr.isolation, "run_isolated", fake)
    return fake


def _channel_listing(*videos: dict[str, Any], source: str = "scrape") -> dict[str, Any]:
    return {"channel": {"id": CHANNEL, "title": "Lab"}, "videos": list(videos), "source": source}


def _put_transcript(vid: str, text: str) -> None:
    cache.put_transcript(
        video_id=vid,
        lang="en",
        actual_lang="en",
        is_generated=False,
        text=text,
        segments=[{"start_s": 1.0, "duration_s": 4.0, "end_s": 5.0, "text": text}],
        word_count=len(text.split()),
        validated=True,
        warnings=[],
    )


def _build_channel(upstream: FakeIsolation, *videos: dict[str, Any], source: str = "scrape") -> dict[str, Any]:
    upstream.script("fetch_channel_uploads", _channel_listing(*videos, source=source))
    env = cr.corpus_build("channel", CHANNEL, limit=50)
    assert env["error"] is None, env
    return env


def _file_bytes(handle: str) -> bytes:
    return skeleton.skeleton_path(handle).read_bytes()


def _save_legacy(handle: str, target: str, value: str, videos: list[dict[str, Any]], source: str = "scrape") -> None:
    skeleton.save_skeleton(
        {
            "handle": handle,
            "target": target,
            "value": value,
            "built_at": "2026-05-01T00:00:00Z",
            "source": source,
            "expired_at": None,
            "channel": None,
            "videos": videos,
        }
    )


# ---------------------------------------------------------------------------
# Revision discipline
# ---------------------------------------------------------------------------


def test_rebuild_creates_new_revision_and_never_mutates_old(storage, upstream) -> None:
    first = _build_channel(upstream, _video(VID_A, "One"), _video(VID_B, "Two"))
    handle_a = first["data"]["handle"]
    before = _file_bytes(handle_a)
    assert first["data"]["schema_version"] == corpus_revision.SCHEMA_VERSION
    assert first["data"]["previous_handle"] is None
    assert first["data"]["count"] == 2

    second = _build_channel(upstream, _video(VID_C, "Three"), _video(VID_A, "One"))
    handle_b = second["data"]["handle"]

    assert handle_b != handle_a
    assert second["data"]["previous_handle"] == handle_a
    assert _file_bytes(handle_a) == before
    assert [v["id"] for v in skeleton.load_skeleton(handle_a)["videos"]] == [VID_A, VID_B]


def test_manifest_records_versioned_acquisition_provenance(storage, upstream) -> None:
    cache.put_video_meta(VID_A, {"id": VID_A, "title": "One"})
    _put_transcript(VID_A, "capture time words")
    env = _build_channel(upstream, _video(VID_A, "One"), _video(VID_B, "Two"))
    payload = skeleton.load_skeleton(env["data"]["handle"])
    manifest = payload["manifest"]

    assert manifest["manifest_version"] == 1
    assert manifest["server"]["name"] == "youtube-mcp-v2"
    assert manifest["server"]["version"]
    assert manifest["captured_at"] == payload["built_at"]
    assert manifest["origin"]["target"] == "channel"
    assert manifest["acquisition"]["provider"] == "channel-page"
    assert manifest["acquisition"]["provider_class"] == "scrape"
    assert manifest["acquisition"]["attempts"][0]["outcome"] == "success"
    assert all("message" not in attempt for attempt in manifest["acquisition"]["attempts"])
    assert manifest["membership"]["count"] == 2

    first, second = manifest["entries"]
    assert first["url"] == f"https://www.youtube.com/watch?v={VID_A}"
    assert len(first["metadata_sha256"]) == 64
    assert first["metadata_revision"]["row_id"] > 0
    assert set(first["transcript_revisions"]) == {"en"}
    assert len(first["transcript_revisions"]["en"]["sha256"]) == 64
    assert second["metadata_revision"] is None
    assert second["transcript_revisions"] == {}

    assert env["provenance"]["acquisition"]["provider"] == "channel-page"
    assert corpus_revision.integrity(payload)["ok"] is True


def test_manifest_transcript_hash_matches_index_source_revision(storage, upstream) -> None:
    _put_transcript(VID_A, "buck converter ripple")
    env = _build_channel(upstream, _video(VID_A, "One"))
    handle = env["data"]["handle"]
    prepared = corpus_index.prepare_index(handle)
    conn = corpus_index.sqlite3.connect(prepared.path)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()
    source = json.loads(meta["source_revisions"])[0]
    entry = skeleton.load_skeleton(handle)["manifest"]["entries"][0]
    assert entry["transcript_revisions"]["en"]["sha256"] == source["transcript_sha256"]


def test_yt_dlp_channel_fallback_is_recorded_in_attempts(storage, upstream) -> None:
    env = _build_channel(upstream, _video(VID_A, "One"), source="yt-dlp")
    manifest = skeleton.load_skeleton(env["data"]["handle"])["manifest"]
    assert manifest["acquisition"]["provider"] == "yt-dlp"
    assert [(a["provider"], a["outcome"]) for a in manifest["acquisition"]["attempts"]] == [
        ("channel-page", "empty"),
        ("yt-dlp", "success"),
    ]


def test_integrity_detects_edits_to_a_revision_file(storage, upstream) -> None:
    env = _build_channel(upstream, _video(VID_A, "One"))
    handle = env["data"]["handle"]
    path = skeleton.skeleton_path(handle)
    data = json.loads(path.read_text())
    data["videos"][0]["title"] = "Tampered"
    path.write_text(json.dumps(data))

    got = cr.corpus_get(handle)
    assert got["data"]["integrity"]["ok"] is False
    assert got["data"]["integrity"]["membership_sha256_ok"] is False
    assert got["validated"] is False
    assert got["warnings"]


def test_query_against_revision_a_stays_reproducible_after_b(storage, upstream) -> None:
    _put_transcript(VID_A, "The TPS62132 buck converter is efficient at light load.")
    handle_a = _build_channel(upstream, _video(VID_A, "One"))["data"]["handle"]
    index_a = corpus_index.prepare_index(handle_a).index_revision
    before = corpus_search(handle_a, "TPS62132", index_revision=index_a)
    assert before["data"]["hits"][0]["video_id"] == VID_A

    # Evidence refreshes and the channel is rebuilt with different membership.
    _put_transcript(VID_A, "Replacement transcript about gallium nitride only.")
    _put_transcript(VID_B, "A brand new upload about TPS62132 layout.")
    handle_b = _build_channel(upstream, _video(VID_B, "Two"), _video(VID_A, "One"))["data"]["handle"]
    index_b = corpus_index.prepare_index(handle_b).index_revision
    assert index_b != index_a

    after = corpus_search(handle_a, "TPS62132", index_revision=index_a)
    assert after["data"]["hits"] == before["data"]["hits"]
    assert after["data"]["index_revision"] == index_a
    assert [v["id"] for v in skeleton.load_skeleton(handle_a)["videos"]] == [VID_A]

    got = cr.corpus_get(handle_a)
    assert [r["index_revision"] for r in got["data"]["index_revisions"]] == [index_a]
    diff = cr.corpus_diff(handle_b)["data"]
    assert diff["base_handle"] == handle_a
    assert [c["change"] for c in diff["transcript_changed"]["changes"]] == ["changed"]


# ---------------------------------------------------------------------------
# Diff fixtures
# ---------------------------------------------------------------------------


def _pair(upstream, base_videos, head_videos, head_source="scrape"):
    a = _build_channel(upstream, *base_videos)["data"]["handle"]
    b = _build_channel(upstream, *head_videos, source=head_source)["data"]["handle"]
    return a, b


def test_diff_no_change(storage, upstream) -> None:
    videos = [_video(VID_A, "One"), _video(VID_B, "Two")]
    _a, b = _pair(upstream, videos, videos)
    env = cr.corpus_diff(b)
    counts = env["data"]["counts"]
    assert env["error"] is None
    assert counts == {
        "base": 2,
        "head": 2,
        "added": 0,
        "removed": 0,
        "metadata_changed": 0,
        "volatile_only": 0,
        "transcript_changed": 0,
        "provenance_changed": 0,
        "unchanged": 2,
    }
    assert env["data"]["provenance_changed"]["revision"]["changed"] is False
    assert env["data"]["transcript_changed"]["comparison"] == "capture-time"
    assert env["warnings"] == []


def test_diff_added_and_removed(storage, upstream) -> None:
    a, b = _pair(
        upstream,
        [_video(VID_A, "One"), _video(VID_B, "Two")],
        [_video(VID_C, "Three"), _video(VID_A, "One")],
    )
    data = cr.corpus_diff(b, base_handle=a)["data"]
    assert [(i["id"], i["position"]) for i in data["added"]] == [(VID_C, 0)]
    assert [(i["id"], i["position"]) for i in data["removed"]] == [(VID_B, 1)]
    assert data["counts"]["unchanged"] == 1


def test_diff_metadata_change_ignores_volatile_fields_by_default(storage, upstream) -> None:
    _a, b = _pair(
        upstream,
        [_video(VID_A, "One"), _video(VID_B, "Two")],
        [_video(VID_A, "One (remastered)"), _video(VID_B, "Two", view_count=999, published="3 days ago")],
    )
    data = cr.corpus_diff(b)["data"]
    assert data["counts"]["metadata_changed"] == 1
    assert data["counts"]["volatile_only"] == 1
    assert data["metadata_changed"] == [
        {"id": VID_A, "position": 0, "changes": {"title": {"before": "One", "after": "One (remastered)"}}}
    ]

    strict = cr.corpus_diff(b, ignore_volatile=False)["data"]
    assert strict["counts"]["metadata_changed"] == 2
    assert set(strict["metadata_changed"][1]["changes"]) == {"published", "view_count"}


def test_diff_reports_transcript_revision_change_separately(storage, upstream) -> None:
    _put_transcript(VID_A, "first capture")
    a = _build_channel(upstream, _video(VID_A, "One"), _video(VID_B, "Two"))["data"]["handle"]
    _put_transcript(VID_A, "refreshed transcript")
    _put_transcript(VID_B, "newly captioned")
    b = _build_channel(upstream, _video(VID_A, "One"), _video(VID_B, "Two"))["data"]["handle"]

    data = cr.corpus_diff(b, base_handle=a)["data"]
    assert data["counts"]["metadata_changed"] == 0
    assert data["counts"]["transcript_changed"] == 2
    changes = {(c["id"], c["change"]) for c in data["transcript_changed"]["changes"]}
    assert changes == {(VID_A, "changed"), (VID_B, "added")}
    assert data["counts"]["unchanged"] == 0


def test_diff_reports_provenance_change_separately(storage, upstream) -> None:
    videos = [_video(VID_A, "One")]
    _a, b = _pair(upstream, videos, videos, head_source="yt-dlp")
    data = cr.corpus_diff(b)["data"]
    prov = data["provenance_changed"]
    assert prov["revision"]["changed"] is True
    assert prov["revision"]["before"]["provider"] == "channel-page"
    assert prov["revision"]["after"]["provider"] == "yt-dlp"
    assert prov["videos"] == [{"id": VID_A, "before": "channel-page", "after": "yt-dlp"}]
    assert data["counts"]["metadata_changed"] == 0
    assert data["counts"]["added"] == data["counts"]["removed"] == 0


def test_diff_is_reproducible_and_ignores_current_cache(storage, upstream) -> None:
    videos = [_video(VID_A, "One")]
    _a, b = _pair(upstream, videos, videos)
    first = cr.corpus_diff(b)["data"]
    _put_transcript(VID_A, "arrives after both captures")
    assert cr.corpus_diff(b)["data"] == first


def test_diff_truncates_lists_but_keeps_complete_counts(storage, upstream) -> None:
    base = [_video(VID_A, "One")]
    head = [_video(f"vid{i:08d}", f"n{i}") for i in range(5)]
    _a, b = _pair(upstream, base, head)
    env = cr.corpus_diff(b, max_items=2)
    assert env["data"]["counts"]["added"] == 5
    assert len(env["data"]["added"]) == 2
    assert env["data"]["truncated"] == {"added": 3}
    assert any("truncated" in w for w in env["warnings"])


def test_diff_requires_base_when_no_lineage(storage, upstream) -> None:
    only = _build_channel(upstream, _video(VID_A, "One"))["data"]["handle"]
    env = cr.corpus_diff(only)
    assert env["error"]["code"] == "no_base_revision"


def test_cross_scope_diff_warns(storage, upstream) -> None:
    a = _build_channel(upstream, _video(VID_A, "One"))["data"]["handle"]
    cache.put_video_meta(VID_A, {"id": VID_A, "title": "One"})
    b = cr.corpus_build("videos", "picked", videos=[VID_A], fetch_metadata=False)["data"]["handle"]
    env = cr.corpus_diff(b, base_handle=a)
    assert env["data"]["scope"]["same_scope"] is False
    assert any("different scopes" in w for w in env["warnings"])


# ---------------------------------------------------------------------------
# Legacy skeleton data stays readable
# ---------------------------------------------------------------------------


LEGACY = "chan-UCBJycsmduvYEL83R_U4JriQ-20260501-000000-000001"


def test_legacy_skeleton_is_readable_as_corpus_revision(storage) -> None:
    _save_legacy(LEGACY, "channel", CHANNEL, [_video(VID_A, "One"), _video(VID_B, "Two")])
    before = _file_bytes(LEGACY)

    got = cr.corpus_get(LEGACY)["data"]
    assert got["legacy"] is True
    assert got["schema_version"] == corpus_revision.LEGACY_SCHEMA_VERSION
    assert got["manifest"]["derived"] is True
    assert got["manifest"]["entry_count"] == 2
    assert got["integrity"]["checked"] is False

    page = cr.corpus_list(LEGACY, page_size=1)["data"]
    assert page["items"][0]["id"] == VID_A
    assert page["items"][0]["capture"]["transcript_revisions"] is None
    assert page["next_cursor"]

    index = cr.corpus_index_tool()["data"]
    assert [r["handle"] for r in index["revisions"]] == [LEGACY]
    assert index["revisions"][0]["legacy"] is True
    assert _file_bytes(LEGACY) == before


def test_rebuild_after_legacy_links_lineage_and_diffs(storage, upstream) -> None:
    _save_legacy(LEGACY, "channel", CHANNEL, [_video(VID_A, "One"), _video(VID_B, "Two")])
    head = _build_channel(upstream, _video(VID_A, "One"))
    assert head["data"]["previous_handle"] == LEGACY
    env = cr.corpus_diff(head["data"]["handle"])
    assert env["data"]["legacy"] == {"base": True, "head": False}
    assert env["data"]["counts"]["removed"] == 1
    assert env["data"]["transcript_changed"]["comparison"] == "unavailable"
    assert any("legacy" in w for w in env["warnings"])


def test_legacy_skeleton_build_writes_manifest_revision_with_old_shape(storage, monkeypatch) -> None:
    monkeypatch.setattr(skeleton_tools, "_build_topic", lambda value, limit: {"videos": [_video(VID_A, "One")]})
    env = skeleton_tools.skeleton_build("topic", "power electronics", limit=5)
    assert env["error"] is None
    assert set(env["data"]) == {"handle", "target", "value", "source", "count"}
    payload = skeleton.load_skeleton(env["data"]["handle"])
    assert payload["schema_version"] == corpus_revision.SCHEMA_VERSION
    assert payload["manifest"]["acquisition"]["provider"] == "search-page"

    assert skeleton_tools.skeleton_build("playlist", PLAYLIST)["error"]["code"] == "bad_target"


def test_expiring_a_revision_uses_sidecar_and_keeps_file_immutable(storage, upstream) -> None:
    handle = _build_channel(upstream, _video(VID_A, "One"))["data"]["handle"]
    before = _file_bytes(handle)
    env = skeleton_tools.skeleton_expire(handle)
    assert env["error"] is None
    assert _file_bytes(handle) == before
    assert skeleton.lifecycle_path(handle).is_file()
    assert cr.corpus_get(handle)["data"]["lifecycle"]["expired_at"]
    assert cr.corpus_get(handle)["data"]["integrity"]["ok"] is True
    assert skeleton_tools.skeleton_get(handle)["data"]["expired_at"]
    assert cr.corpus_index_tool()["data"]["revisions"][0]["expired_at"]


def test_expiring_legacy_skeleton_keeps_v02_in_place_behavior(storage) -> None:
    _save_legacy(LEGACY, "channel", CHANNEL, [_video(VID_A, "One")])
    skeleton_tools.skeleton_expire(LEGACY)
    assert json.loads(skeleton.skeleton_path(LEGACY).read_text())["expired_at"]
    assert not skeleton.lifecycle_path(LEGACY).exists()


# ---------------------------------------------------------------------------
# Pagination and enrichment
# ---------------------------------------------------------------------------


def test_membership_pagination_walks_every_member_once(storage, upstream) -> None:
    videos = [_video(f"vid{i:08d}", f"n{i}") for i in range(7)]
    handle = _build_channel(upstream, *videos)["data"]["handle"]
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        env = cr.corpus_list(handle, cursor=cursor, page_size=3)
        assert env["error"] is None
        seen.extend(item["id"] for item in env["data"]["items"])
        pages += 1
        cursor = env["data"]["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == [v["id"] for v in videos]


def test_membership_cursor_is_bound_to_its_revision(storage, upstream) -> None:
    a = _build_channel(upstream, _video(VID_A, "One"), _video(VID_B, "Two"))["data"]["handle"]
    b = _build_channel(upstream, _video(VID_A, "One"), _video(VID_B, "Two"))["data"]["handle"]
    cursor = cr.corpus_list(a, page_size=1)["data"]["next_cursor"]
    assert cr.corpus_list(b, cursor=cursor)["error"]["code"] == "bad_cursor"
    assert cr.corpus_list(a, cursor="not-a-cursor!!")["error"]["code"] == "bad_cursor"
    index_cursor = corpus_revision._encode_cursor({"k": "index", "t": None, "b": "", "h": ""})
    assert cr.corpus_list(a, cursor=index_cursor)["error"]["code"] == "bad_cursor"


def test_index_pagination_is_stable_when_new_revisions_appear(storage, upstream) -> None:
    handles = [_build_channel(upstream, _video(VID_A, "One"))["data"]["handle"] for _ in range(4)]
    first = cr.corpus_index_tool(page_size=2)["data"]
    assert [r["handle"] for r in first["revisions"]] == handles[::-1][:2]
    assert first["total"] == 4

    newest = _build_channel(upstream, _video(VID_A, "One"))["data"]["handle"]
    second = cr.corpus_index_tool(cursor=first["next_cursor"], page_size=2)["data"]
    assert [r["handle"] for r in second["revisions"]] == handles[::-1][2:]
    assert newest not in [r["handle"] for r in second["revisions"]]
    assert second["next_cursor"] is None

    assert cr.corpus_index_tool(target="playlist", cursor=first["next_cursor"])["error"]["code"] == "bad_cursor"
    assert cr.corpus_index_tool(target="bogus")["error"]["code"] == "bad_target"


def test_index_filters_by_target_and_orders_across_scopes(storage, upstream) -> None:
    cache.put_video_meta(VID_A, {"id": VID_A, "title": "One"})
    vids = cr.corpus_build("videos", "picked", videos=[VID_A], fetch_metadata=False)["data"]["handle"]
    chan = _build_channel(upstream, _video(VID_A, "One"))["data"]["handle"]
    data = cr.corpus_index_tool(target="videos")["data"]
    assert [r["handle"] for r in data["revisions"]] == [vids]
    # Newest first even within one second ("chan-" sorts before "vids-").
    assert [r["handle"] for r in cr.corpus_index_tool()["data"]["revisions"]] == [chan, vids]


def test_enrichment_is_returned_beside_frozen_records(storage, upstream) -> None:
    handle = _build_channel(upstream, _video(VID_A, "One"))["data"]["handle"]
    before = _file_bytes(handle)
    cache.put_video_meta(VID_A, {"id": VID_A, "title": "Renamed later", "has_transcript": True})
    _put_transcript(VID_A, "later transcript")

    env = cr.corpus_list(handle, enrich=True)
    item = env["data"]["items"][0]
    assert item["record"]["title"] == "One"
    assert "has_transcript" not in item["record"]
    assert item["capture"]["transcript_revisions"] == {}
    enrichment = env["data"]["enrichment"]["videos"][VID_A]
    assert enrichment["metadata"]["title"] == "Renamed later"
    assert set(enrichment["cached_transcripts"]) == {"en"}
    assert _file_bytes(handle) == before

    plain = cr.corpus_list(handle)
    assert "enrichment" not in plain["data"]


# ---------------------------------------------------------------------------
# Playlist and explicit-set targets (no API key)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        PLAYLIST,
        f"https://www.youtube.com/playlist?list={PLAYLIST}",
        f"https://www.youtube.com/watch?v={VID_A}&list={PLAYLIST}&index=2",
        f"https://m.youtube.com/playlist?list={PLAYLIST}",
    ],
)
def test_parse_playlist_id_accepts_ids_and_urls(value: str) -> None:
    assert cr.parse_playlist_id(value) == PLAYLIST


@pytest.mark.parametrize("value", ["", "short", "https://example.com/playlist?list=" + PLAYLIST, "https://notyoutube.com/playlist?list=" + PLAYLIST, "PL bad id"])
def test_parse_playlist_id_rejects_non_playlists(value: str) -> None:
    with pytest.raises(ValueError):
        cr.parse_playlist_id(value)


def test_playlist_corpus_without_api_key_uses_yt_dlp(storage, upstream) -> None:
    upstream.script(
        "fetch_playlist_items",
        {
            "playlist": {"id": PLAYLIST, "title": "Talks"},
            "videos": [_video(VID_B, "Two", channel="Lab B"), _video(VID_A, "One", channel="Lab A")],
            "source": "yt-dlp",
        },
    )
    env = cr.corpus_build("playlist", f"https://www.youtube.com/playlist?list={PLAYLIST}")
    assert env["error"] is None, env
    handle = env["data"]["handle"]
    assert handle.startswith(f"plist-{PLAYLIST}-")
    assert env["data"]["value"] == PLAYLIST
    assert env["source"] == "scrape"
    payload = skeleton.load_skeleton(handle)
    assert [v["id"] for v in payload["videos"]] == [VID_B, VID_A]
    assert payload["playlist"]["title"] == "Talks"
    assert payload["manifest"]["origin"]["canonical_url"] == f"https://www.youtube.com/playlist?list={PLAYLIST}"
    assert payload["manifest"]["acquisition"]["provider"] == "yt-dlp"
    assert upstream.calls[0][1] == (PLAYLIST, 50)


def test_playlist_corpus_prefers_api_and_falls_back_on_quota(storage, upstream, monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    monkeypatch.setattr(
        data_api,
        "playlist_items",
        lambda playlist_id, limit=50: {"playlist": {"id": playlist_id}, "videos": [_video(VID_A, "One")]},
    )
    env = cr.corpus_build("playlist", PLAYLIST)
    assert env["source"] == "api"
    assert upstream.calls == []

    def quota(playlist_id, limit=50):
        raise data_api.ApiQuotaError("quota")

    monkeypatch.setattr(data_api, "playlist_items", quota)
    upstream.script("fetch_playlist_items", {"playlist": None, "videos": [_video(VID_A, "One")], "source": "yt-dlp"})
    fallback = cr.corpus_build("playlist", PLAYLIST)
    assert fallback["error"] is None
    attempts = fallback["provenance"]["acquisition"]["attempts"]
    assert [(a["provider"], a["outcome"], a["detail_code"]) for a in attempts] == [
        ("youtube-data-api", "error", "quota_exceeded"),
        ("yt-dlp", "success", None),
    ]
    assert fallback["data"]["previous_handle"] == env["data"]["handle"]
    diff = cr.corpus_diff(fallback["data"]["handle"])["data"]
    assert diff["provenance_changed"]["revision"]["changed"] is True


def test_playlist_build_failure_reports_attempts(storage, upstream) -> None:
    upstream.script("fetch_playlist_items", RuntimeError("yt-dlp exploded"))
    env = cr.corpus_build("playlist", PLAYLIST)
    assert env["error"]["code"] == "corpus_build_failed"
    assert env["provenance"]["acquisition"]["attempts"][0]["outcome"] == "error"
    assert list(skeleton.SKELETON_DIR.glob("*.json")) == []


def test_playlist_bad_value(storage) -> None:
    assert cr.corpus_build("playlist", "nope")["error"]["code"] == "bad_value"


def test_explicit_video_set_without_api_key(storage, monkeypatch) -> None:
    cache.put_video_meta(VID_A, {"id": VID_A, "title": "Cached One", "channel": "Lab"})
    fetched: list[str] = []

    def fake_inspect(video_id: str):
        fetched.append(video_id)
        if video_id == VID_C:
            return envelope.fail("video_unavailable", "private", recoverable=False)
        cache.put_video_meta(video_id, {"id": video_id, "title": "Fetched Two"})
        return envelope.ok({"id": video_id}, source="scrape")

    monkeypatch.setattr("youtube_mcp_v2.tools.inspect.inspect_video", fake_inspect)
    env = cr.corpus_build(
        "videos",
        "my picks",
        videos=[f"https://youtu.be/{VID_A}", VID_B, VID_C, VID_A],
    )
    assert env["error"] is None, env
    assert fetched == [VID_B, VID_C]
    assert env["data"]["handle"].startswith("vids-my-picks-")
    assert env["data"]["count"] == 3
    assert any("ID-only" in w for w in env["warnings"])
    payload = skeleton.load_skeleton(env["data"]["handle"])
    assert [v["id"] for v in payload["videos"]] == [VID_A, VID_B, VID_C]
    assert [v["title"] for v in payload["videos"]] == ["Cached One", "Fetched Two", None]
    sources = [e["metadata_source"] for e in payload["manifest"]["entries"]]
    assert sources == ["cache", "watch-page", "unavailable"]
    assert payload["manifest"]["acquisition"]["provider"] == "watch-page"


def test_explicit_video_set_offline_mode_uses_cache_only(storage, monkeypatch) -> None:
    def boom(video_id: str):
        raise AssertionError("must not fetch")

    monkeypatch.setattr("youtube_mcp_v2.tools.inspect.inspect_video", boom)
    env = cr.corpus_build("videos", videos=[VID_A], fetch_metadata=False)
    assert env["error"] is None
    assert env["data"]["value"] == "videos"
    assert env["source"] == "cache"


def test_explicit_video_set_validation(storage) -> None:
    assert cr.corpus_build("videos", videos=[])["error"]["code"] == "bad_value"
    assert cr.corpus_build("videos", videos=["not a video"])["error"]["code"] == "bad_video"
    too_many = [f"vid{i:08d}" for i in range(cr.MAX_EXPLICIT_BUILD_VIDEOS + 1)]
    assert cr.corpus_build("videos", videos=too_many)["error"]["code"] == "too_many_videos"
    assert cr.corpus_build("collection", "x")["error"]["code"] == "bad_target"


def test_compose_writes_manifest_revision_and_is_diffable(storage) -> None:
    cache.put_video_meta(VID_A, {"id": VID_A, "title": "One"})
    cache.put_video_meta(VID_B, {"id": VID_B, "title": "Two"})
    first = corpus_compose("set label", videos=[VID_A])
    second = corpus_compose("derived label", base_handle=first["data"]["handle"], videos=[VID_B])
    assert second["error"] is None
    payload = skeleton.load_skeleton(second["data"]["handle"])
    assert payload["schema_version"] == corpus_revision.SCHEMA_VERSION
    assert payload["parent_handle"] == first["data"]["handle"]
    assert payload["manifest"]["acquisition"]["provider"] == "compose"
    diff = cr.corpus_diff(second["data"]["handle"])["data"]
    assert diff["base_handle"] == first["data"]["handle"]
    assert [i["id"] for i in diff["added"]] == [VID_B]

    again = corpus_compose("set label", videos=[VID_A])
    assert skeleton.load_skeleton(again["data"]["handle"])["manifest"]["lineage"]["previous_handle"] == first["data"]["handle"]


# ---------------------------------------------------------------------------
# Adapters (offline)
# ---------------------------------------------------------------------------


def test_fetch_playlist_items_shapes_yt_dlp_entries(monkeypatch) -> None:
    import subprocess

    lines = [
        {
            "id": VID_A,
            "title": "One",
            "duration": 61,
            "channel": "Lab A",
            "channel_id": "UCa",
            "playlist_title": "Talks",
            "playlist_channel": "Owner",
            "playlist_channel_id": CHANNEL,
            "playlist_count": 3,
        },
        {"id": None, "title": "[Private video]"},
        {"id": VID_B, "title": "Two", "uploader": "Lab B"},
    ]
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        stdout = "\n".join(json.dumps(line) for line in lines) + "\nnot json\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(channel_scrape.subprocess, "run", fake_run)
    out = channel_scrape.fetch_playlist_items(PLAYLIST, 10)
    assert captured["cmd"][-1] == f"https://www.youtube.com/playlist?list={PLAYLIST}"
    assert "--flat-playlist" in captured["cmd"]
    assert out["source"] == "yt-dlp"
    assert out["playlist"]["title"] == "Talks"
    assert out["playlist"]["channel_id"] == CHANNEL
    assert out["playlist"]["item_count"] == 3
    assert [v["id"] for v in out["videos"]] == [VID_A, VID_B]
    assert [v["channel"] for v in out["videos"]] == ["Lab A", "Lab B"]
    assert out["videos"][0]["duration_s"] == 61


def test_data_api_playlist_items_keeps_playlist_order(monkeypatch) -> None:
    class Call:
        def __init__(self, payload):
            self.payload = payload

        def execute(self):
            return self.payload

    class Resource:
        def __init__(self, handler):
            self.handler = handler

        def list(self, **kwargs):
            return Call(self.handler(**kwargs))

    def playlists(**kwargs):
        return {"items": [{"id": PLAYLIST, "snippet": {"title": "Talks", "channelTitle": "Owner", "channelId": CHANNEL}, "contentDetails": {"itemCount": 3}}]}

    def playlist_items(**kwargs):
        return {"items": [{"contentDetails": {"videoId": v}} for v in (VID_B, VID_C, VID_A)]}

    def videos(**kwargs):
        # The API returns videos in arbitrary order and omits private ones.
        return {
            "items": [
                {"id": vid, "snippet": {"title": title, "channelTitle": "Lab", "channelId": "UCx", "publishedAt": "2026-01-01T00:00:00Z", "thumbnails": {}}, "contentDetails": {"duration": "PT1M"}, "statistics": {"viewCount": "5"}}
                for vid, title in ((VID_A, "One"), (VID_B, "Two"))
            ]
        }

    class Client:
        def playlists(self):
            return Resource(playlists)

        def playlistItems(self):
            return Resource(playlist_items)

        def videos(self):
            return Resource(videos)

    monkeypatch.setattr(data_api, "_get_client", lambda: Client())
    monkeypatch.setattr(data_api, "_execute", lambda request: request.execute())
    out = data_api.playlist_items(PLAYLIST, limit=10)
    assert out["playlist"]["title"] == "Talks"
    assert [v["id"] for v in out["videos"]] == [VID_B, VID_A]
    assert out["videos"][0]["channel"] == "Lab"


# ---------------------------------------------------------------------------
# Server tool surface
# ---------------------------------------------------------------------------


def _tool_names(env: dict[str, str]) -> list[str]:
    import os
    import subprocess
    import sys

    code = (
        "import asyncio, json\n"
        "from youtube_mcp_v2.server import mcp\n"
        "print(json.dumps(sorted(t.name for t in asyncio.run(mcp.list_tools()))))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_server_registers_corpus_tools_and_hides_legacy_by_default() -> None:
    names = _tool_names({"YOUTUBE_MCP_LEGACY_SKELETON_TOOLS": ""})
    for tool in ("corpus.build", "corpus.get", "corpus.list", "corpus.index", "corpus.diff"):
        assert tool in names
    assert not [n for n in names if n.startswith("skeleton.")]
    assert len(names) == 21

    legacy = _tool_names({"YOUTUBE_MCP_LEGACY_SKELETON_TOOLS": "1"})
    assert {n for n in legacy if n.startswith("skeleton.")} == {
        "skeleton.build",
        "skeleton.list",
        "skeleton.get",
        "skeleton.diff",
        "skeleton.expire",
        "skeleton.index",
    }
