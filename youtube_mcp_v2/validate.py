"""Transcript validation gate. Returns (validated, warnings)."""

from __future__ import annotations

# Word-rate plausibility band (words per second of video).
# 0.3 wps = very sparse (background music vid with occasional speech)
# 6.0 wps = unrealistically fast (auctioneer/rapper); typical fast speech ~4 wps
WPS_MIN = 0.3
WPS_MAX = 6.0


def validate_transcript(
    *,
    text: str,
    segments: list[dict] | None,
    duration_s: int | float | None,
    requested_lang: str,
    actual_lang: str | None,
) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    ok = True

    word_count = len(text.split()) if text else 0

    if word_count == 0:
        warnings.append("transcript empty")
        ok = False
        return ok, warnings

    if duration_s and duration_s > 0:
        wps = word_count / duration_s
        if wps < WPS_MIN:
            warnings.append(
                f"low word-rate: {wps:.2f} wps "
                f"({word_count} words / {duration_s}s) — possible truncation"
            )
            ok = False
        elif wps > WPS_MAX:
            warnings.append(
                f"high word-rate: {wps:.2f} wps "
                f"({word_count} words / {duration_s}s) — possible duplication"
            )
            ok = False
    else:
        # The word-rate gate is one of the core transcript integrity checks. If
        # duration could not be resolved, do not claim the transcript is fully
        # validated merely because the checks we *could* run happened to pass.
        warnings.append("duration unavailable — word-rate validation skipped")
        ok = False

    if actual_lang and actual_lang != requested_lang:
        warnings.append(
            f"lang fallback: requested={requested_lang} got={actual_lang}"
        )
        # Lang fallback is informational, not a hard fail by itself.

    if "[...]" in text or "[…]" in text:
        warnings.append("truncation marker found in text")
        ok = False

    if segments:
        last = -1.0
        for i, seg in enumerate(segments):
            start = seg.get("start_s", seg.get("start", 0))
            if start < last:
                warnings.append(f"non-monotonic timestamp at segment {i}")
                ok = False
                break
            last = start

    return ok, warnings
