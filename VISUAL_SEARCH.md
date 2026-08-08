# Visual evidence search

`corpus.visual_search` retrieves **on-screen evidence** across a frozen YouTube corpus: charts, hardware, diagrams, products, user interfaces, and other information that may never appear in the spoken transcript.

It follows the same evidence discipline as transcript corpus search:

- frozen corpus membership,
- immutable content-addressed index revisions,
- explicit model identity,
- timestamped evidence,
- exact artifact SHA-256,
- protocol-native resource delivery, and
- no hidden live acquisition during index preparation.

## Public tool

```text
corpus.visual_search(
    handle,
    query,
    top_k=10,
    visual_index_revision=None,
    auto_prepare=True,
    visual="auto",
)
```

### Visual policy

- `visual="auto"` — uses the paired visual models only if the semantic package and configured model files are already local. It never downloads model weights.
- `visual="required"` — explicitly requires visual retrieval and may initialize/download the configured models unless `YOUTUBE_MCP_EMBED_LOCAL_ONLY=1` is set.

The current backend is FastEmbed/ONNX CLIP using paired text/image encoders by default:

```text
YOUTUBE_MCP_VISUAL_TEXT_MODEL=Qdrant/clip-ViT-B-32-text
YOUTUBE_MCP_VISUAL_IMAGE_MODEL=Qdrant/clip-ViT-B-32-vision
```

These are implementation defaults, not MCP contract fields. A future backend may replace them while preserving the same evidence/search surface.

## Deliberate acquisition boundary

`corpus.visual_search` **does not download videos and does not create missing frames**.

When `auto_prepare=true`, preparation means:

> discover trustworthy timestamped frames that already exist in the managed frame cache, freeze those pixels into immutable evidence artifacts, and index them.

Bulk visual acquisition belongs in a separate bounded/resumable hydration workflow. Keeping acquisition and preparation separate prevents a seemingly small search request from unexpectedly downloading hundreds of videos.

## Trusted timestamp sources

The first visual index accepts two frame-cache forms:

1. individual `single_<timestamp-ms>...png|jpg` files, where the timestamp is encoded in the managed filename;
2. individual contact-sheet source frames referenced by a managed `sheet_*.json` manifest whose `frame_timestamps` and `frame_paths` line up.

Manifest paths are accepted only when the resolved file is directly inside the expected managed cache directory for that video. A stale or malicious manifest cannot point the indexer at arbitrary local files.

The tiled contact-sheet image itself is not indexed because it represents multiple timestamps in one image and is therefore poor evidence for a single temporal claim.

## Frozen pixel artifacts

The ordinary frame cache is an acquisition cache: a frame may be rebuilt later. That makes its pathname unsuitable as immutable evidence identity.

During visual preparation each accepted frame is therefore copied into a separate content-addressed store:

```text
vectors/visual-artifacts/<sha-prefix>/<sha256>.<ext>
```

The copy is verified against its SHA-256 and published without replacing an existing artifact. Visual index identity records the exact frame SHA-256 and timestamp.

If the ordinary frame cache is later rebuilt with different pixels, the old visual index still points to the original frozen frame bytes. Preparing again produces a different visual index revision when the frozen frame set changes.

## Visual index identity

A visual index revision represents:

```text
visual schema version
+ frozen corpus revision/hash
+ paired text model identity
+ paired image model identity
+ each video/timestamp/frame SHA-256/format
+ vector normalization schema
```

Changing the pixels, corpus capture, or model pair creates a different `visual_index_revision`.

## Retrieval

The first implementation embeds the natural-language query into the paired CLIP text space and performs an exact cosine scan over L2-normalized frame embeddings.

Exact scan is intentional for the first evidence baseline. Approximate-nearest-neighbor indexing can replace the internal scan later when benchmarked corpus sizes justify it; the public tool and immutable evidence contract do not need to change.

A hit includes:

- rank,
- video ID/title/channel,
- exact `timestamp_s`,
- canonical timestamped YouTube URL,
- frame SHA-256,
- frozen artifact format,
- source kind (`single_frame` or `contact_sheet_frame`),
- cross-modal similarity score,
- local artifact path for same-host agents,
- protocol-native MCP resource URI/mime/size/portability metadata.

## Protocol-native frozen image resources

Portable hits use content-addressed URIs:

```text
youtube-mcp://evidence/frame/png/<sha256>
youtube-mcp://evidence/frame/jpg/<sha256>
```

Resource reads:

- accept only a 64-hex SHA-256 and supported image format,
- resolve only through the content-addressed visual artifact store,
- verify the exact bytes being returned against the URI hash, and
- enforce `YOUTUBE_MCP_MAX_VISUAL_RESOURCE_BYTES` (default 16 MiB).

This lets a remote MCP client consume the exact frozen evidence image without sharing the server filesystem.

## Coverage

Visual preparation reports:

- total corpus videos,
- videos with at least one indexed timestamped frame,
- missing video IDs,
- coverage fraction,
- total indexed frame count.

Search warns when coverage is partial. A missing video means only that no trustworthy timestamped frame evidence is currently cached; it does not mean the video lacks useful visual information.

## Next step: visual hydration

The next acquisition layer should populate timestamped frame evidence in bounded/resumable batches, analogous to transcript `corpus.hydrate`:

```text
frozen corpus
   ↓
bounded visual hydration
   ↓
immutable visual preparation
   ↓
corpus.visual_search
```

After that, transcript and visual retrieval can be fused into one cross-modal evidence query while preserving the component evidence types and scores.
