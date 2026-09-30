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
    filters=None,
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

### Filters

`filters` restricts which evidence may be returned. It never changes ranking statistics (BM25 document frequencies and dense similarities are always computed over the whole frozen index), so the same filters on the same `index_revision` are reproducible. Unknown keys are rejected with `bad_filters`.

| key | meaning |
|---|---|
| `video_ids` | only these member videos |
| `exclude_video_ids` | never these videos |
| `channels` | only these channel names (as frozen in the corpus) |
| `time_range_s` | `[start_s, end_s]`; chunks overlapping this span of each video |
| `validated_only` | only transcript revisions whose validation status is `pass` (same as the top-level flag) |
| `acquisition_methods` | `captions` and/or `local_stt` |
| `is_generated` | `true`/`false` generated-caption (or STT) status |

`acquisition_methods` needs an index built by this version (schema 2); older pinned indexes return a clear error instead of guessing.

### Hit contract

Each hit includes:

- `rank` and a stable `evidence_id` (`ev_` + chunk digest),
- video ID, title, and channel,
- `url`: canonical YouTube URL at the chunk start, plus `anchor` — the exact source segment inside the chunk that best matches the query (`start_s`, `end_s`, `url`, `segment`, `basis: exact|lexical|chunk`),
- `start_s` / `end_s` and the supporting `excerpt`,
- `scores`: `lexical`, `semantic`, `hybrid`, `exact_match`, plus `lexical_rank` / `semantic_rank` / `exact_rank` for hybrid retrieval,
- `chunk`: index, SHA-256, and `segment_range` into the source transcript revision,
- `transcript`: revision ID and SHA-256, requested/actual language, generated status, `validated`, `validation_status` (`pass|partial|fail`, see `EVIDENCE_MODEL.md`), warnings, fetch time, and `acquisition` — provider, method (`captions|local_stt`), provider class, attempts, and for local STT the engine, model identity/revision, decode config, source-audio SHA-256, and segment timing summary,
- `corpus_revision` and `index_revision`.

The response also identifies the frozen `corpus_revision`, `corpus_manifest_sha256` (when the corpus revision carries one), immutable `index_revision`, `coverage`, and a `retrieval` record: class, index schema, chunker, lexical scorer, embedding model identity when hybrid, fusion policy, vector-search implementation, `top_k`, and the applied filters. Backend and model *choices* are never tool inputs; they are reported only as provenance.

## Reproducibility invariant

An index revision represents:

```text
frozen corpus membership
+ corpus capture identity
+ requested language
+ chunking policy (chunker id, budget, overlap)
+ exact transcript revision IDs
+ exact transcript SHA-256 hashes
+ retrieval backend/model identity
+ vector normalization/schema when semantic
+ fusion policy and vector-search implementation when semantic
+ index schema version
```

Index schema 2 (this release) adds a frozen `sources` table — per transcript revision: validation status, duration, and a copy of the acquisition provenance — and per-segment timings for anchors. New indexes therefore get new revisions; existing pinned schema-1 indexes still open and rank exactly as before (no anchors, `acquisition: null`).

Mutable lifecycle metadata such as `expired_at` is intentionally excluded from corpus identity.

If a transcript is fetched again later, preparing the corpus again creates a different index revision. Searching the older explicitly named index continues to use the older embedded evidence chunks. Changing the embedding model also creates a different hybrid index revision.

## Lexical retrieval

The lexical layer uses BM25-style ranking and a tokenizer that preserves technical identifiers. A token such as `TPS62132-Q1` is indexed both as the complete identifier and as useful components. This makes exact part numbers, model names, acronyms, and numerical identifiers first-class retrieval signals.

An exact phrase match receives a small boost, but candidates still come from the inverted index.

### Chunking

`timestamp-boundary-v1` packs whole caption/STT segments (never splitting one, so every chunk boundary is a real timing) up to the token budget, and closes a chunk early at a sentence end or a speech pause of at least 1.5 s once it is 60% full. Each chunk records its `segment_start:segment_end` range and the character offset + timing of every segment, which is how hits cite an exact `anchor` without reloading the transcript. The previous `token-window-v1` behavior remains available internally and is part of index identity.

## Dense semantic retrieval

Hybrid indexes copy the frozen lexical evidence artifact, then add L2-normalized float32 dense vectors to those exact chunks. Passage and query embeddings use retrieval-specific backend methods.

The first vector search implementation (`exact-scan-v1`) performs an exact scan across indexed chunks. This is intentionally simple and reproducible. It avoids introducing a vector database/service before benchmark data shows where approximate-nearest-neighbor indexing becomes necessary.

Vector search sits behind the internal `youtube_mcp_v2.vector_search` seam: a `VectorSearch` protocol plus a registry keyed by the name recorded in each hybrid index. A future ANN implementation registers under a new name; indexes built with it record that name, old indexes keep using the exact scan, and an index that names an implementation this server does not provide fails closed rather than silently changing results. None of this appears in the MCP tool inputs.

Semantic retrieval can therefore surface a passage even when **none of the query words appear verbatim in that passage**.

## Hybrid fusion

Lexical and semantic rankings are fused with reciprocal rank fusion (RRF), rather than directly adding incomparable BM25 and cosine score scales. The candidate set is the union of leading lexical and semantic rankings.

The default policy, `rrf-exact-v1`, adds a third ranked list: chunks that contain every identifier-like query token (anything with a digit or a technical separator — part numbers, case numbers, prices, dates, error codes, citations) and every quoted phrase verbatim. Without it, a dense model that disagrees with the lexical ranking can push an exact part number below near-miss passages. Indexes built before this policy record no fusion key and keep ranking with plain `rrf-v1`.

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

## Scale

Search never loads transcripts into model context, and never loads the whole corpus into server memory either: hybrid search scans only light per-chunk columns (IDs, timing, filter fields) plus stored vectors streamed row by row, reads text only for lexical candidates, and materializes full rows for the final `top_k`. The test suite builds a synthetic 100+ hour corpus (100 videos × 61 min) and asserts needles are found with responses under 64 KB; `bench/retrieval_suite.py --hours 100` reports latency and recall at that size.

## Future scaling and multimodal extension

The exact dense scan is a baseline, not a permanent commitment. If benchmarked corpus sizes justify ANN, the internal vector retrieval implementation can change while preserving the same `corpus.search` tool and immutable evidence identity.

The same evidence contract can later index local-STT revisions, OCR text, frame descriptions/embeddings, and scene boundaries. Cross-modal retrieval should continue to return timestamped evidence tied to frozen corpus and index revisions rather than exposing vector-database internals to the calling model.
