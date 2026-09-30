"""Adapter-neutral exact-phrase evidence location over timed transcript segments.

Both the in-repo adapter and competitor adapters feed their server's *timed*
transcript output through the same matcher, so temporal-window scoring measures
the server's timestamp fidelity rather than differences in client-side search.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def normalize(text: str) -> str:
    return _NON_WORD.sub(" ", text.lower()).strip()


def locate(segments: Iterable[dict[str, Any]], query: str, *, limit: int = 10) -> list[dict[str, float]]:
    """Return ranked hits `{start_s, end_s}` for every occurrence of `query`.

    Segments must provide `start_s`, `end_s` (or `duration_s`), and `text`.
    A phrase may span segment boundaries; the hit covers every segment it
    touches. Hits are ranked by transcript order (first occurrence = rank 1).
    """
    needle = normalize(query)
    if not needle:
        return []
    pieces: list[str] = []
    spans: list[tuple[int, int, float, float]] = []  # char_start, char_end, start_s, end_s
    cursor = 0
    for seg in segments:
        text = normalize(str(seg.get("text", "")))
        if not text:
            continue
        start_s = float(seg["start_s"])
        end_s = float(seg.get("end_s", start_s + float(seg.get("duration_s", 0.0))))
        if pieces:
            pieces.append(" ")
            cursor += 1
        pieces.append(text)
        spans.append((cursor, cursor + len(text), start_s, end_s))
        cursor += len(text)
    # Pad so matches respect word boundaries ("trunk" must not match "trunks").
    haystack = " " + "".join(pieces) + " "
    padded = f" {needle} "

    hits: list[dict[str, float]] = []
    pos = haystack.find(padded)
    while pos != -1 and len(hits) < limit:
        start = pos  # index into unpadded text (leading pad shifts by one)
        end = start + len(needle)
        touched = [s for s in spans if s[0] < end and s[1] > start]
        if touched:
            hits.append({"start_s": touched[0][2], "end_s": touched[-1][3]})
        pos = haystack.find(padded, pos + len(needle) + 1)
    return hits
