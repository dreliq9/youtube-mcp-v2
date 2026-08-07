"""Corpus evidence-search tools.

Legacy `skeleton.*` handles are accepted as corpus revision identifiers. This is
intentional: the public vocabulary can evolve toward `corpus.*` without making
existing frozen research snapshots unreadable.
"""

from __future__ import annotations

from typing import Any, Literal

from .. import corpus_index, embeddings, envelope, hybrid_index

SemanticMode = Literal["off", "auto", "required"]


def _coverage_warning(prepared: corpus_index.PreparedIndex) -> str | None:
    if not prepared.missing_videos:
        return None
    return (
        f"search coverage is partial: {prepared.indexed_videos}/"
        f"{prepared.total_videos} videos have cached transcripts"
    )


def _prepare_for_mode(
    handle: str,
    *,
    lang: str,
    chunk_tokens: int,
    chunk_overlap: int,
    semantic: SemanticMode,
) -> tuple[corpus_index.PreparedIndex, list[str]]:
    warnings: list[str] = []
    backend = embeddings.resolve_backend(semantic)
    if backend is not None:
        prepared = hybrid_index.prepare_hybrid_index(
            handle,
            backend=backend,
            requested_lang=lang,
            chunk_tokens=chunk_tokens,
            chunk_overlap=chunk_overlap,
        )
    else:
        prepared = corpus_index.prepare_index(
            handle,
            requested_lang=lang,
            chunk_tokens=chunk_tokens,
            chunk_overlap=chunk_overlap,
        )
        if semantic == "auto":
            warnings.append(
                "semantic retrieval is not locally available; prepared the immutable "
                "lexical index instead. Install the 'semantic' extra and run with "
                "semantic='required' once to initialize the configured local model."
            )
    coverage = _coverage_warning(prepared)
    if coverage:
        warnings.append(coverage)
    return prepared, warnings


def corpus_prepare(
    handle: str,
    lang: str = "en",
    chunk_tokens: int = corpus_index.DEFAULT_CHUNK_TOKENS,
    chunk_overlap: int = corpus_index.DEFAULT_CHUNK_OVERLAP,
    semantic: SemanticMode = "auto",
) -> dict[str, Any]:
    """Prepare an immutable local evidence index from cached corpus transcripts.

    `semantic='auto'` uses a locally cached dense model when available but never
    downloads one. `required` explicitly opts into semantic model initialization;
    `off` builds the dependency-free lexical index. No mode performs live YouTube
    transcript acquisition.
    """
    try:
        prepared, warnings = _prepare_for_mode(
            handle,
            lang=lang,
            chunk_tokens=chunk_tokens,
            chunk_overlap=chunk_overlap,
            semantic=semantic,
        )
    except FileNotFoundError as exc:
        return envelope.fail("corpus_not_found", str(exc), recoverable=False)
    except embeddings.EmbeddingUnavailable as exc:
        return envelope.fail("semantic_unavailable", str(exc))
    except embeddings.EmbeddingError as exc:
        return envelope.fail("semantic_failed", str(exc))
    except corpus_index.CorpusIndexError as exc:
        return envelope.fail("corpus_prepare_failed", str(exc), recoverable=False)
    except Exception as exc:
        return envelope.fail("corpus_prepare_failed", str(exc))

    return envelope.ok(
        prepared.as_dict(),
        source="cache",
        validated=(prepared.indexed_videos > 0 and not prepared.missing_videos),
        warnings=warnings,
    )


def _load_pinned(
    handle: str,
    index_revision: str,
) -> corpus_index.PreparedIndex:
    path = corpus_index.index_path(handle, index_revision)
    return corpus_index._prepared_from_path(path, cached=True)


def corpus_search(
    handle: str,
    query: str,
    top_k: int = 10,
    lang: str = "en",
    index_revision: str | None = None,
    validated_only: bool = False,
    auto_prepare: bool = True,
    semantic: SemanticMode = "auto",
) -> dict[str, Any]:
    """Retrieve timestamped transcript evidence from one frozen corpus revision.

    Semantic implementation details remain hidden behind `off|auto|required`.
    Supplying an explicit index revision pins both evidence inputs and retrieval
    model identity; auto mode will not download a missing semantic model.
    """
    warnings: list[str] = []
    try:
        if semantic not in {"off", "auto", "required"}:
            raise embeddings.EmbeddingError(f"invalid semantic mode: {semantic!r}")

        prepared: corpus_index.PreparedIndex | None = None
        backend: embeddings.EmbeddingBackend | None = None

        if index_revision is not None:
            prepared = _load_pinned(handle, index_revision)
            if prepared.requested_lang != lang:
                raise corpus_index.CorpusIndexError(
                    f"index {index_revision} was built for {prepared.requested_lang!r}, "
                    f"not {lang!r}"
                )
            if prepared.backend == "hybrid":
                if semantic == "off":
                    raise corpus_index.CorpusIndexError(
                        "the pinned index is hybrid; semantic='off' would change its "
                        "retrieval algorithm. Pin a lexical index revision instead."
                    )
                backend = embeddings.resolve_backend(
                    "required" if semantic == "required" else "auto",
                    model_id=prepared.model_id,
                )
                if backend is None:
                    raise embeddings.EmbeddingUnavailable(
                        f"pinned hybrid index requires local embedding model "
                        f"{prepared.model_id!r}; auto mode will not download it"
                    )
            elif prepared.backend == "lexical":
                if semantic == "required":
                    raise corpus_index.CorpusIndexError(
                        "the pinned index is lexical but semantic='required'; prepare "
                        "and pin a hybrid index revision"
                    )
            else:
                raise corpus_index.CorpusIndexError(
                    f"unsupported pinned index backend: {prepared.backend!r}"
                )
        elif semantic == "off":
            prepared = hybrid_index.latest_lexical_index(
                handle, requested_lang=lang
            )
            if prepared is None:
                if not auto_prepare:
                    return envelope.fail(
                        "index_missing",
                        "no lexical corpus index exists; call corpus.prepare with "
                        "semantic='off' or set auto_prepare=true",
                    )
                prepared = corpus_index.prepare_index(handle, requested_lang=lang)
        else:
            backend = embeddings.resolve_backend(semantic)
            if backend is not None:
                prepared = hybrid_index.latest_hybrid_index(
                    handle,
                    requested_lang=lang,
                    model_id=backend.model_id,
                )
                if prepared is None:
                    if not auto_prepare:
                        return envelope.fail(
                            "index_missing",
                            "no hybrid corpus index exists for the configured local "
                            "embedding model; call corpus.prepare or set auto_prepare=true",
                        )
                    prepared = hybrid_index.prepare_hybrid_index(
                        handle,
                        backend=backend,
                        requested_lang=lang,
                    )
            else:
                prepared = hybrid_index.latest_lexical_index(
                    handle, requested_lang=lang
                )
                if prepared is None:
                    if not auto_prepare:
                        return envelope.fail(
                            "index_missing",
                            "semantic retrieval is not locally available and no lexical "
                            "index exists; call corpus.prepare or set auto_prepare=true",
                        )
                    prepared = corpus_index.prepare_index(handle, requested_lang=lang)
                warnings.append(
                    "semantic retrieval is not locally available; searched the immutable "
                    "lexical index instead"
                )

        if prepared is None:
            raise corpus_index.CorpusIndexNotFound("no corpus index was resolved")

        coverage = _coverage_warning(prepared)
        if coverage:
            warnings.append(coverage)

        if prepared.backend == "hybrid":
            if backend is None:
                raise embeddings.EmbeddingUnavailable(
                    f"hybrid index requires embedding model {prepared.model_id!r}"
                )
            result = hybrid_index.search_hybrid_index(
                handle,
                query,
                backend=backend,
                top_k=top_k,
                index_revision=prepared.index_revision,
                requested_lang=lang,
                validated_only=validated_only,
            )
        else:
            result = corpus_index.search_index(
                handle,
                query,
                top_k=top_k,
                index_revision=prepared.index_revision,
                requested_lang=lang,
                validated_only=validated_only,
            )
    except FileNotFoundError as exc:
        return envelope.fail("corpus_not_found", str(exc), recoverable=False)
    except embeddings.EmbeddingUnavailable as exc:
        return envelope.fail("semantic_unavailable", str(exc))
    except embeddings.EmbeddingError as exc:
        return envelope.fail("semantic_failed", str(exc))
    except corpus_index.CorpusIndexNotFound as exc:
        return envelope.fail("index_missing", str(exc))
    except corpus_index.CorpusIndexError as exc:
        return envelope.fail("corpus_search_failed", str(exc), recoverable=False)
    except Exception as exc:
        return envelope.fail("corpus_search_failed", str(exc))

    return envelope.ok(result, source="cache", warnings=warnings)
