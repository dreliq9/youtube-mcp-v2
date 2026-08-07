# Heterogeneous corpus composition

`corpus.compose` creates a new immutable research corpus from deliberately chosen sources rather than requiring the research universe to come from one channel or one search query.

## Why

Cross-domain questions are often the interesting ones. A caller may want to combine videos discovered independently—for example a documentary, lecture, interview, tutorial, review, performance, debate, and archival clip—then search that exact source set repeatedly.

Using a topic search as a fake container weakens provenance and makes later revision work ambiguous. Composed collections therefore have a first-class `target="collection"` and `set-*` handle.

## Tool

```text
corpus.compose(
  label,
  videos=None,
  include_handles=None,
  base_handle=None,
  remove=None
)
```

### Inputs

- `label`: human-readable purpose/name for the new research set.
- `videos`: direct YouTube IDs or supported YouTube URLs to append.
- `include_handles`: existing frozen channel/topic/collection handles to union into the new set.
- `base_handle`: optional existing corpus to derive from.
- `remove`: IDs/URLs to exclude from the final result.

At least one source (`videos`, `include_handles`, or `base_handle`) is required.

## Deterministic membership order

Composition uses a simple reproducible order:

1. members from `base_handle`, if supplied,
2. members from each `include_handles` corpus in caller order,
3. explicit `videos` in caller order,
4. removals applied last.

Duplicate video IDs keep their first occurrence. This matters because the first occurrence also preserves the first frozen membership record/title/channel metadata.

## No network side effects

`corpus.compose` performs no live YouTube request.

For direct videos that are not already in an included corpus, it uses cached video metadata when available and otherwise stores a lean canonical ID/URL record. Existing inspect/transcript/frame hydration paths can populate evidence later.

This keeps source selection cheap, auditable, and independent of upstream availability.

## Revision discipline

Composition never mutates its source corpora. The new collection records:

- `parent_handle`,
- included source handles,
- explicit video IDs,
- actual added IDs,
- actual removed IDs,
- duplicate IDs ignored,
- frozen final membership.

Deriving a revised collection therefore produces a new `set-*` handle while the old collection remains readable.

## Limits

Initial defensive bounds:

- up to 500 explicit video references per composition call,
- up to 25 included frozen corpora,
- up to 2,000 videos in the resulting composed corpus.

These bounds control tool payload size; they are not intended to define the eventual maximum searchable corpus size.

## Example research patterns

### Cross-domain synthesis

Freeze separate topic/channel corpora, then combine them:

```text
history_set = skeleton.build(target="topic", value="productive failure history")
biology_set = skeleton.build(target="topic", value="adaptation failure biology")
coaching_set = skeleton.build(target="topic", value="learning from mistakes coaching")

mixed = corpus.compose(
  label="productive failure across domains",
  include_handles=[history_set, biology_set, coaching_set]
)
```

### Curated source list

```text
corpus.compose(
  label="primary interviews",
  videos=[url_a, url_b, url_c, url_d]
)
```

### Immutable revision

```text
corpus.compose(
  label="primary interviews rev2",
  base_handle=old_handle,
  videos=[new_source],
  remove=[source_now_known_to_be_irrelevant]
)
```

The resulting handle works with the same downstream corpus indexing/search machinery as channel/topic snapshots.
