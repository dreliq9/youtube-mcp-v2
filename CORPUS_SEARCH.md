# Corpus evidence search

`corpus.*` is the retrieval layer for research across many frozen YouTube videos. Existing `skeleton.*` handles are accepted as corpus revision identifiers so historical research sets remain usable while the public vocabulary evolves.

## Why prepare an index?

Without a corpus index, an agent that wants to find one claim across hundreds of videos has to fetch many full transcripts into model context. `corpus.prepare` moves that filtering work into a local immutable artifact. `corpus.search` can then return only the small number of timestamped evidence chunks most likely to answer the question.

The first backend is deliberately dependency-free lexical retrieval. Dense semantic embeddings are a replaceable next layer, not a different public API.

## Prepare

```text
corpus.prepare(handle, lang="en", chunk_tokens=500, chunk_overlap=50)
```

Preparation:

1. loads the frozen skeleton/corpus membership,
2. selects the newest cached transcript revision for each video and requested language,
3. chunks timed segments while preserving temporal boundaries,
4. records the transcript row ID and SHA-256 for every included source,
5. builds a BM25-style inverted index in SQLite,
6. derives a content-addressed `index_revision`, and
7. publishes the index without overwriting an existing revision.

Preparation **does not perform live YouTube acquisition**. Videos without a cached transcript are returned in `missing_videos`. This keeps index construction deterministic and prevents an apparently local indexing operation from silently changing network state or selecting new evidence.

## Search

```text
corpus.search(
    handle,
    query,
    top_k=10,
    lang="en",
    index_revision=None,
    validated_only=False,
    auto_prepare=True,
)
```

When `index_revision` is supplied, the exact immutable index is searched. When it is omitted, the newest prepared index for the requested language is used. If none exists and `auto_prepare=true`, an index is built from transcript revisions already in cache.

Each hit includes:

- rank,
- video ID, title, and channel,
- canonical YouTube URL with timestamp when timing exists,
- `start_s` / `end_s`,
- supporting excerpt,
- retrieval score slots for lexical, semantic, and hybrid ranking,
- chunk index and chunk SHA-256,
- transcript revision ID and SHA-256,
- requested and actual caption language,
- generated-caption status when known,
- transcript validation state and warnings, and
- transcript acquisition timestamp.

The response also identifies the frozen `corpus_revision` and immutable `index_revision`.

## Reproducibility invariant

An index revision represents:

```text
frozen corpus membership
+ corpus capture identity
+ requested language
+ chunking policy
+ exact transcript revision IDs
+ exact transcript SHA-256 hashes
+ retrieval backend/model identity
```

Mutable lifecycle metadata such as `expired_at` is intentionally excluded from corpus identity.

If a transcript is fetched again later, preparing the corpus again creates a different index revision. Searching the older explicitly named index continues to use the older embedded evidence chunks. This is the central distinction between this design and a mutable vector collection whose documents are silently replaced in place.

## Lexical baseline

The baseline uses BM25-style ranking and a tokenizer that preserves technical identifiers. A token such as `TPS62132-Q1` is indexed both as the complete identifier and as useful components. This makes exact part numbers, model names, acronyms, and numerical identifiers first-class retrieval signals.

An exact phrase match receives a small boost, but candidates still come from the inverted index.

## Partial coverage

A corpus can be prepared even when only some member videos have cached transcripts. The index records:

- total corpus videos,
- indexed videos,
- missing video IDs,
- coverage fraction, and
- chunk count.

Tool responses warn when coverage is partial. `validated_only=true` can further restrict retrieval to transcript revisions that passed the available validation gates.

## Semantic extension contract

Dense semantic retrieval must extend this index rather than replace it. The public `corpus.search` contract should remain stable while implementation backends are replaceable.

The intended hybrid behavior is:

1. preserve lexical retrieval for exact identifiers/numbers/names,
2. retrieve semantic candidates across all indexed chunks,
3. fuse lexical and semantic rankings without requiring comparable raw score scales,
4. return both component scores and the final hybrid score, and
5. include embedding model identity in the immutable index revision.

A semantic-only match must be able to surface even when no query token appears verbatim in the transcript.

## Future multimodal extension

The same evidence contract can later index OCR text, frame descriptions/embeddings, scene boundaries, and local-STT revisions. Cross-modal retrieval should continue to return timestamped evidence tied to frozen corpus and index revisions rather than exposing vector-database internals to the calling model.
