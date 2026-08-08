# Editor handoff — from video evidence to an editable timeline

youtube-mcp-v2 does not become a non-linear editor. Its responsibility ends at an **editor-neutral, provenance-preserving clip plan plus explicitly materialized local media assets**.

This creates a clean boundary:

```text
YouTube research / evidence retrieval
        ↓
corpus.clip_plan
        ↓
immutable youtube-mcp.clip-plan/v1
        ↓
media.materialize        (explicit heavy/network step)
        ↓
youtube-mcp.materialized-clip-plan/v1
        ↓
      editor adapter
      /          \
   Declip       FCP-MCP
```

## `corpus.clip_plan`

```text
corpus.clip_plan(
  handle,
  query,
  target_duration_s=60,
  max_clips=8,
  min_clip_duration_s=4,
  max_clip_duration_s=20,
  context_before_s=2,
  context_after_s=2,
  lang="en",
  validated_only=false,
  modalities="auto"
)
```

The tool uses the existing bounded transcript/visual evidence retrieval code in local-only `auto` mode. It never downloads a model and never downloads source video.

The returned immutable plan contains:

- `plan_revision` (`cp-...`),
- frozen corpus revision,
- retrieval/index revisions when available,
- query and planning settings,
- ordered clip candidates,
- source YouTube ID/URL/title/channel,
- source `start_s` / `end_s`,
- evidence rank/type and the bounded supporting evidence object.

Plans are content-addressed under the managed cache root in `edit-plans/`. Repeating the same evidence/settings resolves to the same plan identity instead of silently rewriting it.

Planning is intentionally deterministic. The calling reasoning model can create several plans, reorder clips, reject candidates, or use more specific queries when it wants a stronger narrative. youtube-mcp does not hide a second LLM inside the tool.

## `media.materialize`

```text
media.materialize(
  plan_revision,
  max_height=720,
  clip_ids=None
)
```

This is the explicit heavy/network boundary.

- maximum 12 clips per call,
- maximum 8 unique source videos per call,
- maximum 180 seconds per clip,
- maximum 900 seconds total source range per call,
- allowed heights: 360 / 480 / 720 / 1080.

The materializer groups requested clips by YouTube video and downloads each unique source at most once during the call. Each range is then re-encoded to an editor-friendly H.264/AAC MP4 with an exact local source range.

Outputs are cached under `editor-clips/<video_id>/` and hashed with SHA-256. The materialized manifest records:

- original YouTube URL/video ID,
- source range,
- local MP4 path,
- duration,
- SHA-256 and byte size,
- title/channel when available,
- whether the clip was already cached.

The manifest itself is content-addressed under `edit-plans/materialized/`.

`YOUTUBE_YTDLP_PROXY`, `YOUTUBE_COOKIES_FILE`, and `YOUTUBE_COOKIES_FROM_BROWSER` are honored for source acquisition. Their values are not exposed in tool results.

## Neutral asset semantics

Every materialized asset is already trimmed to the plan's source range. A downstream editor therefore treats the asset as a new local clip whose edit range begins at zero:

```json
{
  "clip_id": "clip-001",
  "path": "/.../editor-clips/VIDEO/clip_98000ms_108000ms_720p.mp4",
  "duration_s": 10.0,
  "video_id": "...",
  "source_start_s": 98.0,
  "source_end_s": 108.0,
  "source_url": "https://www.youtube.com/watch?v=...",
  "sha256": "..."
}
```

This is intentionally easy for any local editing system to consume.

## Declip mapping

Declip's declarative project schema maps directly:

```json
{
  "asset": "<materialized asset.path>",
  "start": "auto",
  "trim_in": 0,
  "trim_out": "<asset.duration_s>"
}
```

A Declip adapter can place these objects in materialized-manifest order on a timeline track and choose transitions/output settings independently. youtube-mcp does not force those creative decisions.

## FCP-MCP mapping

For FCP-MCP, a materialized asset can feed the existing montage/rough-cut generators as a normal local source:

```json
{
  "src": "<materialized asset.path>",
  "name": "<asset.title or clip_id>",
  "duration": "<asset.duration_s>s"
}
```

The adapter can pass the resulting list to `fcpxml_generate_montage` / `fcpxml_auto_rough_cut`, then use FCP-MCP's normal transactional/reviewable editing workflow for subsequent changes.

## Example workflow

Prompt:

> Make a 90-second montage showing how people in different fields describe a frontier.

Possible orchestration:

1. `corpus.compose` a mixed set of historians, artists, scientists, ranchers, entrepreneurs, etc.
2. hydrate/index the desired evidence as needed.
3. `corpus.clip_plan(..., target_duration_s=90)`.
4. inspect/revise the returned candidates if desired.
5. `media.materialize(plan_revision)`.
6. pass the materialized manifest to Declip for a headless render **or** to FCP-MCP for an editable Final Cut timeline.
7. use the editor MCP for transitions, titles, pacing, captions, music, QC, and final export.

## Provenance and publication

The handoff preserves source identity/ranges specifically so an agent or human can audit where every excerpt came from. Materialization does not assert that a source may legally be republished; rights, licensing, attribution, platform policy, and fair-use/fair-dealing analysis remain publication-context decisions outside the retrieval contract.
