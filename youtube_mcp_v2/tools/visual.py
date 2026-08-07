"""Cross-modal visual evidence retrieval over already-cached corpus frames."""

from __future__ import annotations

from typing import Any, Literal

from .. import envelope, visual_embeddings, visual_index, visual_resources
from ..embeddings import EmbeddingError, EmbeddingUnavailable

VisualMode = Literal["auto", "required"]


def corpus_visual_search(
    handle: str,
    query: str,
    top_k: int = 10,
    visual_index_revision: str | None = None,
    auto_prepare: bool = True,
    visual: VisualMode = "auto",
) -> dict[str, Any]:
    """Search frozen timestamped frame evidence with a natural-language query.

    This tool never downloads video/frame content. `auto_prepare` means "index
    already-cached trusted frames", not "acquire missing frames". Visual model
    auto mode is also local-only and never downloads model weights.
    """
    warnings: list[str] = []
    try:
        if visual not in {"auto", "required"}:
            raise EmbeddingError("visual must be 'auto' or 'required'")

        backend = visual_embeddings.resolve_visual_backend(visual)
        if backend is None:
            return envelope.fail(
                "visual_unavailable",
                "visual retrieval is not locally available; install the semantic "
                "extra and run once with visual='required' to initialize the "
                "configured paired visual models",
                recoverable=True,
            )

        if visual_index_revision is None:
            prepared = visual_index.latest_visual_index(
                handle,
                text_model_id=backend.text_model_id,
                image_model_id=backend.image_model_id,
            )
            if prepared is None:
                if not auto_prepare:
                    return envelope.fail(
                        "visual_index_missing",
                        "no compatible visual index exists; cache timestamped frames "
                        "and call again with auto_prepare=true",
                        recoverable=True,
                    )
                prepared = visual_index.prepare_visual_index(handle, backend=backend)
        else:
            prepared = None

        result = visual_index.search_visual_index(
            handle,
            query,
            backend=backend,
            top_k=top_k,
            visual_index_revision=(
                visual_index_revision
                if visual_index_revision is not None
                else prepared.visual_index_revision
            ),
        )

        for hit in result.get("hits", []):
            resource = visual_resources.describe(
                hit["frame_sha256"], PathLikeExt.from_path(hit["artifact_path"])
            )
            hit.update(resource)
            if not resource["resource_portable"]:
                warnings.append(
                    "one or more visual evidence frames exceed the portable MCP "
                    "resource limit; local artifact_path remains available"
                )

        coverage = result.get("coverage") or {}
        missing = coverage.get("missing_videos") or []
        if missing:
            warnings.append(
                f"visual coverage is partial: {coverage.get('indexed_videos', 0)}/"
                f"{coverage.get('total_videos', 0)} corpus videos have cached "
                "timestamped frame evidence"
            )
    except FileNotFoundError as exc:
        return envelope.fail("corpus_not_found", str(exc), recoverable=False)
    except EmbeddingUnavailable as exc:
        return envelope.fail("visual_unavailable", str(exc), recoverable=True)
    except EmbeddingError as exc:
        return envelope.fail("visual_embedding_failed", str(exc), recoverable=True)
    except visual_index.VisualIndexNotFound as exc:
        return envelope.fail("visual_index_missing", str(exc), recoverable=True)
    except (visual_index.VisualIndexError, visual_resources.VisualResourceError) as exc:
        return envelope.fail("visual_search_failed", str(exc), recoverable=False)
    except Exception as exc:
        return envelope.fail("visual_search_failed", str(exc))

    return envelope.ok(result, source="cache", warnings=list(dict.fromkeys(warnings)))


class PathLikeExt:
    """Tiny helper to keep resource format derived from the frozen artifact itself."""

    @staticmethod
    def from_path(path: str) -> str:
        suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if suffix == "jpeg":
            suffix = "jpg"
        if suffix not in {"png", "jpg"}:
            raise visual_resources.VisualResourceError(
                f"unsupported frozen visual artifact format: {suffix!r}"
            )
        return suffix
