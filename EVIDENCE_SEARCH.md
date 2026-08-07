# Cross-modal evidence search

`corpus.evidence_search` is the compound research surface for asking one question across spoken and on-screen evidence without making the calling model manually reconcile two retrieval systems.

It does **not** replace the component tools. `corpus.search` remains the precise transcript index interface and `corpus.visual_search` remains the precise frame index interface. The compound tool consumes those evidence contracts and produces a bounded fused ranking.

## Call shape

```text
corpus.evidence_search(
    handle,
    query,
    top_k=10,
    lang="en",
    index_revision=None,
    visual_index_revision=None,
    validated_only=False,
    auto_prepare=True,
    modalities="auto",
    temporal_window_s=20.0,
)
```

`top_k` is capped at 25. Internally each requested modality may retrieve up to `min(50, max(20, top_k*5))` candidates so temporal pairing has enough recall without turning the compound response into an unbounded evidence dump.

## Modalities

- `auto` — attempt transcript and visual retrieval, but continue with whichever locally searchable modalities are available.
- `transcript` — transcript evidence is required; visual retrieval is not attempted unless an explicit visual index revision is supplied.
- `visual` — visual evidence is required; transcript retrieval is not attempted unless an explicit transcript index revision is supplied.
- `both` — both transcript and visual retrieval must succeed.

An explicitly supplied component index revision always makes that component required. A caller that pins evidence should never have the pin silently ignored because another modality happened to work.

## No surprise model downloads

The compound tool is intentionally **local-only with respect to ML setup**:

- transcript retrieval calls the component search with `semantic="auto"`;
- visual retrieval calls the component search with `visual="auto"`.

Those auto modes never download model weights.

If richer semantic or visual retrieval is desired, initialize/build the component indexes explicitly first:

```text
corpus.prepare(handle, semantic="required")
corpus.visual_search(handle, "warmup", visual="required")
```

Then `corpus.evidence_search` can use the locally available immutable indexes without turning an ordinary research query into a model-install operation.

## Fusion

Raw BM25, cosine, and CLIP scores are not directly comparable. The compound layer therefore fuses **ranks**, not raw component score scales.

Each candidate receives a reciprocal-rank contribution:

```text
1 / (60 + component_rank)
```

Transcript and visual contributions are summed only when two pieces of evidence are temporally paired. Remaining single-modality candidates retain their own contribution. Final returned scores are normalized relative to the highest returned cluster.

The component hit itself retains its original component score/provenance, so the caller can inspect why it ranked inside that modality.

## Temporal pairing

Cross-modal agreement is useful only when it refers to approximately the same moment.

The first pairing algorithm:

1. considers only transcript/frame candidates from the same video;
2. measures frame distance from the transcript segment interval (`0` when the frame timestamp falls inside the segment);
3. ignores pairs farther apart than `temporal_window_s`;
4. sorts candidate pair edges by distance and component ranks; and
5. greedily performs one-to-one pairing.

One frame therefore cannot be copied into many nearby transcript hits simply because they overlap the same scene.

The temporal window defaults to 20 seconds and is capped at 120 seconds.

## Evidence cluster types

Every result has one of three `evidence_type` values:

### `transcript`

Contains a bounded transcript chunk from the immutable transcript index, including timestamp, excerpt, transcript revision/hash, validation/provenance, and component scores.

### `frame`

Contains the frozen visual evidence identity/resource metadata from the immutable visual index. Same-host `artifact_path` is deliberately removed from the compound result; remote clients should use the content-addressed MCP resource URI.

### `cross_modal`

Contains both a transcript evidence object and a frame evidence object from the same video within the temporal window.

Its fusion metadata includes:

- transcript component rank
- visual component rank
- temporal distance in seconds
- raw RRF sum
- normalized fused score

Cross-modal agreement naturally receives two independent reciprocal-rank contributions and can therefore outrank a strong single-modality hit without inventing an arbitrary hand-tuned BM25-vs-cosine weighting scale.

## Reproducibility

The compound response reports both component index revisions:

```text
transcript_index_revision
visual_index_revision
```

The underlying component evidence retains its transcript/chunk hashes or frame SHA-256/resource identity.

Supplying explicit index revisions pins the exact component evidence/model revisions to search. A pinned component failure is returned as an error rather than silently falling back to a different index or modality.

## Coverage and warnings

The result exposes transcript and visual coverage separately. Component warnings are preserved with a modality prefix.

In `auto` mode, an unavailable component becomes a warning and the query continues with the other modality. This is important because visual coverage can be intentionally partial while frame hydration is still progressing.

If no requested modality is searchable, the tool returns a recoverable `evidence_unavailable` error.

## Context-efficiency goal

The compound tool is intentionally not a summarizer. It returns the smallest ranked evidence set required for the calling model to reason.

That division of labor is important:

```text
large frozen corpus
      ↓
local deterministic retrieval
      ↓
small timestamped evidence set
      ↓
frontier model reasoning/synthesis
```

The MCP should spend local compute reducing the corpus; the calling model should spend expensive context/reasoning only on the evidence that survived retrieval.

## Future fusion layers

The same compound contract can later incorporate additional evidence types without changing the basic philosophy:

- OCR text tied to frame SHA/timestamp
- scene boundaries
- local object/product detections
- audio-event evidence
- comments or description metadata when explicitly requested

New modalities should retain independent evidence identity and component scores rather than collapsing everything into an opaque vector database result.
