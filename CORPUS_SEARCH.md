# Corpus evidence search

`corpus.*` is the retrieval layer for research across many frozen YouTube videos. Existing `skeleton.*` handles are accepted as corpus revision identifiers so historical research sets remain usable while the public vocabulary evolves.

## Why prepare an index?

Without a corpus index, an agent that wants to find one claim across hundreds of videos has to fetch many full transcripts into model context. `corpus.prepare` moves that filtering work into a local immutable artifact. `corpus.search` can then return only the small number of timestamped evidence chunks most likely to answer the question.

The evidence contract is backend-neutral. Every index freezes the corpus membership and exact transcript revisions it contains; lexical and hybrid semantic retrieval are two immutable index types under that same contract.

## Prepare

```text
corpus.prepare(
    handle,
    lang="en",
    chunk_tokens=500,
    chunk_overlap=50,
    semantic="auto",
)
```

Preparation:

1. loads the frozen skeleton/corpus membership,
2. selects the newest cached transcript revision for each video and requested language,
3. chunks timed segments while preserving temporal boundaries,
4. records the transcript row ID and SHA-256 for every included source,
5. builds a BM25-style inverted index in SQLite,
6. optionally embeds those exact same frozen chunks for hybrid retrieval,
7. derives a content-addressed `index_revision`, and
8. publishes the index without overwriting an existing revision.

Preparation **does not perform live YouTube acquisition**. Videos without a cached transcript are returned in `missing_videos`. This keeps index construction deterministic and prevents an apparently local indexing operation from silently selecting new YouTube evidence.

### Semantic mode

The MCP exposes policy rather than a concrete vector backend:

- `semantic="off"` — dependency-free lexical index only.
- `semantic="auto"` — use a semantic backend only if the package and configured model are already available locally. It never downloads a model; otherwise it prepares lexical retrieval and returns a warning.
- `semantic="required"` — require semantic retrieval and explicitly allow model initialization/download unless `YOUTUBE_MCP_EMBED_LOCAL_ONLY=1` is set.

The current optional implementation uses FastEmbed/ONNX. Install it with:

```bash
pip install "youtube-mcp-v2[semantic]"
```

The default configured model is `BAAI/bge-small-en-v1.5`. Override it with:

```text
YOUTUBE_MCP_EMBED_MODEL=<supported FastEmbed text model>
```

Model files are cached under the youtube-mcp model root. `YOUTUBE_MCP_EMBED_LOCAL_ONLY=1` forbids downloads even in required mode.

A practical first initialization is:

```text
corpus.prepare(handle, semantic="required")
```

After the model is local, normal calls can return to `semantic="auto"`.

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
    semantic="auto",
)
```

When `index_revision` is supplied, the exact immutable index is searched. This pins not only transcript evidence but also retrieval backend/model identity. Auto mode will not download a missing model required by a pinned hybrid index; it fails clearly instead of silently changing the algorithm.

When no revision is supplied:

- `off` selects/builds the newest lexical index,
- `auto` prefers a compatible local hybrid index/model and falls back to lexical when semantic retrieval is not local, and
- `required` selects/builds a hybrid index for the configured model.

Each hit includes:

- rank,
- video ID, title, and channel,
- canonical YouTube URL with timestamp when timing exists,
- `start_s` / `end_s`,
- supporting excerpt,
- lexical, semantic, and final hybrid scores when applicable,
- component lexical/semantic ranks for hybrid retrieval,
- chunk index and chunk SHA-256,
- transcript revision ID and SHA-256,
- requested and actual caption language,
- generated-caption status when known,
- transcript validation state and warnings, and
- transcript acquisition timestamp.

The response also identifies the frozen `corpus_revision`, immutable `index_revision`, retrieval class, and embedding model identity when a hybrid index is used.

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
+ vector normalization/schema when semantic
```

Mutable lifecycle metadata such as `expired_at` is intentionally excluded from corpus identity.

If a transcript is fetched again later, preparing the corpus again creates a different index revision. Searching the older explicitly named index continues to use the older embedded evidence chunks. Changing the embedding model also creates a different hybrid index revision.

## Lexical retrieval

The lexical layer uses BM25-style ranking and a tokenizer that preserves technical identifiers. A token such as `TPS62132-Q1` is indexed both as the complete identifier and as useful components. This makes exact part numbers, model names, acronyms, and numerical identifiers first-class retrieval signals.

An exact phrase match receives a small boost, but candidates still come from the inverted index.

## Dense semantic retrieval

Hybrid indexes copy the frozen lexical evidence artifact, then add L2-normalized float32 dense vectors to those exact chunks. Passage and query embeddings use retrieval-specific backend methods.

The first vector search implementation performs an exact scan across indexed chunks. This is intentionally simple and reproducible. It avoids introducing a vector database/service before benchmark data shows where approximate-nearest-neighbor indexing becomes necessary.

Semantic retrieval can therefore surface a passage even when **none of the query words appear verbatim in that passage**.

## Hybrid fusion

Lexical and semantic rankings are fused with reciprocal rank fusion (RRF), rather than directly adding incomparable BM25 and cosine score scales. The candidate set is the union of leading lexical and semantic rankings.

This preserves two important behaviors simultaneously:

1. paraphrases/concepts can be found through dense similarity, and
2. exact identifiers such as part numbers remain strong signals even if the dense model prefers a different passage.

Search results retain component scores/ranks so the calling agent can inspect why a hit surfaced.

## Partial coverage

A corpus can be prepared even when only some member videos have cached transcripts. The index records:

- total corpus videos,
- indexed videos,
- missing video IDs,
- coverage fraction, and
- chunk count.

Tool responses warn when coverage is partial. `validated_only=true` can further restrict retrieval to transcript revisions that passed the available validation gates.

## Future scaling and multimodal extension

The exact dense scan is a baseline, not a permanent commitment. If benchmarked corpus sizes justify ANN, the internal vector retrieval implementation can change while preserving the same `corpus.search` tool and immutable evidence identity.

The same evidence contract can later index local-STT revisions, OCR text, frame descriptions/embeddings, and scene boundaries. Cross-modal retrieval should continue to return timestamped evidence tied to frozen corpus and index revisions rather than exposing vector-database internals to the calling model.
