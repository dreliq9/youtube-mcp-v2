# Member-only Acquisition Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent member-only YouTube videos from reaching audio extraction or local STT.

**Architecture:** Keep the existing caption-first waterfall. Classify explicit
yt-dlp membership denials as unavailable and terminate the waterfall after
provenance is recorded.

**Tech Stack:** Python 3.13, pytest, youtube-transcript-api, yt-dlp, whisper.cpp adapter.

## Global Constraints

- Preserve public tool names, inputs, result fields, and recoverability semantics.
- Do not infer membership from generic downloader failures.
- Do not call local STT or download media after an explicit membership denial.

---

### Task 1: Membership-denial regression coverage

**Files:**
- Modify: `tests/test_transcript_acquisition.py`
- Modify: `youtube_mcp_v2/transcript_acquisition.py`

**Interfaces:**
- Consumes: `acquire_transcript(video_id, lang="en")`
- Produces: yt-dlp attempt `{outcome: "unavailable", detail_code: "membership_required"}` and a two-attempt terminal failure.

- [x] **Step 1: Write the failing test**

```python
def test_membership_required_stops_before_local_stt(monkeypatch):
    monkeypatch.setattr(transcript_api, "fetch_transcript", primary_fail)
    monkeypatch.setattr(
        ytdlp_transcript,
        "fetch_transcript",
        lambda *_a, **_k: (_ for _ in ()).throw(
            ytdlp_transcript.YtDlpTranscriptError(
                "Join this channel to get access to members-only content"
            )
        ),
    )
    monkeypatch.setattr(
        whisper_cpp,
        "settings_from_env",
        lambda: pytest.fail("local STT must not be configured"),
    )

    with pytest.raises(transcript_acquisition.TranscriptAcquisitionFailed) as exc_info:
        transcript_acquisition.acquire_transcript(VIDEO_ID)

    assert [(a.provider, a.outcome, a.detail_code) for a in exc_info.value.attempts] == [
        ("youtube-transcript-api", "unavailable", "no_transcript"),
        ("yt-dlp", "unavailable", "membership_required"),
    ]
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_transcript_acquisition.py::test_membership_required_stops_before_local_stt -q`

Expected: FAIL because the current fallback is classified as `provider_error` and reaches local STT.

- [x] **Step 3: Implement the minimal classification and early terminal failure**

```python
if _fallback_failure_detail(exc) == ("unavailable", "membership_required"):
    raise TranscriptAcquisitionFailed(
        "caption providers failed because YouTube membership is required",
        attempts=attempts,
        primary_error=primary_error,
        fallback_error=fallback_error,
    )
```

- [x] **Step 4: Run focused and full tests**

Run: `uv run python -m pytest tests/test_transcript_acquisition.py -q && uv run python -m pytest -q`

Expected: all tests pass.

- [x] **Step 5: Commit**

```bash
git add youtube_mcp_v2/transcript_acquisition.py tests/test_transcript_acquisition.py docs/superpowers
git commit -m "Skip local STT for member-only videos"
```
