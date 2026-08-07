"""Corpus evidence-search and hydration tools.

Legacy `skeleton.*` handles are accepted as corpus revision identifiers. This is
intentional: the public vocabulary can evolve toward `corpus.*` without making
existing frozen research snapshots unreadable.
"""

from __future__ import annotations

from typing import Any, Literal

from .. import corpus_hydration, corpus_index, envelope


def corpus_hydrate(
    handle: str,
    lang: str = "en",
    cursor: str | None = None,
    batch_size: int = corpus_hydration.DEFAULT_BATCH_SIZE,
    max_workers: int = corpus_hydration.DEFAULT_MAX_WORKERS,
    policy: Literal["missing", "fresh"] = "missing",
) -> dict[str, Any]:
    """Populate transcript cache for one bounded/resumable corpus batch.

    Returns compact acquisition status only; transcript bodies remain in the
    append-only cache for later `corpus.prepare` / `corpus.search`.
    """
    try:
        result = corpus_hydration.hydrate_corpus(
            handle,
            lang=lang,
            cursor=cursor,
            batch_size=batch_size,
            max_workers=max_workers,
            policy=policy,
        )
    except FileNotFoundError as exc:
        return envelope.fail("corpus_not_found", str(exc), recoverable=False)
    except corpus_hydration.CorpusHydrationError as exc:
        return envelope.fail("corpus_hydrate_failed", str(exc), recoverable=False)
    except Exception as exc:
        return envelope.fail("corpus_hydrate_failed", str(exc))

    warnings: list[str] = []
    if result["failed"]:
        warnings.append(
            f"{result['failed']} transcript acquisition attempt(s) failed in this batch; "
            "restart from cursor 0 later to retry unresolved members while cached "
            "successes are skipped"
        )
    return envelope.ok(
        result,
        source="cache",
        validated=(result["failed"] == 0),
        warnings=warnings,
    )


def corpus_prepare(
    handle: str,
    lang: str = "en",
    chunk_tokens: int = corpus_index.DEFAULT_CHUNK_TOKENS,
    chunk_overlap: int = corpus_index.DEFAULT_CHUNK_OVERLAP,
) -> dict[str, Any]:
    """Prepare an immutable local search index from cached corpus transcripts.

    This operation never performs live transcript acquisition. Missing videos are
    reported explicitly so the caller can choose whether/how to acquire them.
    """
    try:
        prepared = corpus_index.prepare_index(
            handle,
            requested_lang=lang,
            chunk_tokens=chunk_tokens,
            chunk_overlap=chunk_overlap,
        )
    except FileNotFoundError as exc:
        return envelope.fail("corpus_not_found", str(exc), recoverable=False)
    except corpus_index.CorpusIndexError as exc:
        return envelope.fail("corpus_prepare_failed", str(exc), recoverable=False)
    except Exception as exc:
        return envelope.fail("corpus_prepare_failed", str(exc))

    warnings: list[str] = []
    if prepared.missing_videos:
        warnings.append(
            f"{len(prepared.missing_videos)} of {prepared.total_videos} corpus videos "
            "have no cached transcript revision for the requested language; they were "
            "not included in this immutable index revision"
        )
    return envelope.ok(
        prepared.as_dict(),
        source="cache",
        validated=(prepared.indexed_videos > 0 and not prepared.missing_videos),
        warnings=warnings,
    )


def corpus_search(
    handle: str,
    query: str,
    top_k: int = 10,
    lang: str = "en",
    index_revision: str | None = None,
    validated_only: bool = False,
    auto_prepare: bool = True,
) -> dict[str, Any]:
    """Retrieve timestamped transcript evidence from one frozen corpus revision.

    When `index_revision` is omitted, the newest prepared index for `handle` and
    `lang` is used. If none exists and `auto_prepare=True`, a content-addressed
    lexical index is built from transcripts already present in the append-only
    cache. Supplying an explicit index revision pins retrieval inputs exactly.
    """
    warnings: list[str] = []
    try:
        if index_revision is None and corpus_index.latest_index(
            handle, requested_lang=lang
        ) is None:
            if not auto_prepare:
                return envelope.fail(
                    "index_missing",
                    "no prepared corpus index exists; call corpus.prepare or set "
                    "auto_prepare=true",
                )
            prepared = corpus_index.prepare_index(handle, requested_lang=lang)
            if prepared.missing_videos:
                warnings.append(
                    f"search coverage is partial: {prepared.indexed_videos}/"
                    f"{prepared.total_videos} videos have cached transcripts"
                )

        result = corpus_index.search_index(
            handle,
            query,
            top_k=top_k,
            index_revision=index_revision,
            requested_lang=lang,
            validated_only=validated_only,
        )
    except FileNotFoundError as exc:
        return envelope.fail("corpus_not_found", str(exc), recoverable=False)
    except corpus_index.CorpusIndexNotFound as exc:
        return envelope.fail("index_missing", str(exc))
    except corpus_index.CorpusIndexError as exc:
        return envelope.fail("corpus_search_failed", str(exc), recoverable=False)
    except Exception as exc:
        return envelope.fail("corpus_search_failed", str(exc))

    coverage = result.get("coverage") or {}
    missing = coverage.get("missing_videos") or []
    if missing and not warnings:
        warnings.append(
            f"search coverage is partial: {coverage.get('indexed_videos', 0)}/"
            f"{coverage.get('total_videos', 0)} videos are indexed"
        )
    return envelope.ok(result, source="cache", warnings=warnings)
