"""Deterministic synthetic research corpus for evidence-retrieval benchmarks.

No network, no model downloads. The generator writes a frozen corpus revision
(skeleton handle) plus append-only transcript revisions into an isolated cache,
with annotated evidence windows ("needles") across several domain strata and
near-miss distractors ("hard negatives") elsewhere in the corpus.

It is shared by `bench/retrieval_suite.py` (metrics) and the pytest acceptance
tests (100-hour scale, reproducibility, local-STT entry).
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

FILLER_WORDS = tuple(
    "the a this that these those we you they it our their some many few more most "
    "then now later today again also still just really quite rather very often "
    "look see show watch notice consider think know say explain describe compare "
    "part piece section step stage point moment example case result reason idea "
    "question answer problem detail summary outline overview approach method "
    "process practice pattern structure system series sequence version setting "
    "early late first second next last final previous following middle same "
    "different similar simple complex useful common rare typical general specific "
    "people person group team audience viewer reader student teacher expert "
    "story history context background source record document material evidence "
    "work build make take give keep hold turn move bring put place leave find "
    "time day week month year hour minute place world country city region area "
    "small large long short high low wide narrow deep light heavy fast slow "
    "careful quick clear rough smooth bright dark open closed full empty warm cool"
    .split()
)
_FILLER_SET = frozenset(FILLER_WORDS)

# (stratum, needle sentence, query, hard-negative sentence or None)
NEEDLES: tuple[tuple[str, str, str, str | None], ...] = (
    (
        "product_identifier",
        "The TPS62132-Q1 regulator held the rail steady under the step load.",
        "TPS62132-Q1",
        "The TPS62132-Q2 variant ships in a different package entirely.",
    ),
    (
        "legal_identifier",
        "The filing in case 21-cv-04573 cited the earlier appellate ruling.",
        "case 21-cv-04573",
        "A separate matter, case 21-cv-04578, was dismissed without comment.",
    ),
    (
        "quantity_price",
        "The final quote for the kitchen remodel came to $18,450 including labor.",
        "$18,450 remodel quote",
        "Materials alone for the bathroom were about $8,450 before labor.",
    ),
    (
        "citation",
        "As Okafor and Lindqvist showed in 2019, the effect vanishes at scale.",
        "Okafor Lindqvist 2019",
        "Lindqvist later revisited the question in a 2021 follow-up.",
    ),
    (
        "scientific_notation",
        "Avogadro's constant, roughly 6.022e23 per mole, anchors the calculation.",
        "6.022e23",
        "The Boltzmann constant near 1.380e-23 appears in the next derivation.",
    ),
    (
        "sports_score",
        "They closed the match three to two after a late penalty in stoppage time.",
        '"three to two"',
        "Earlier in the season they had lost three to one away from home.",
    ),
    (
        "ui_label",
        "Open Settings > Privacy > Location Services and switch it off.",
        "Settings > Privacy > Location Services",
        "Open Settings > Display and lower the brightness first.",
    ),
    (
        "name_date",
        "Ada Lovelace published her notes on the Analytical Engine in 1843.",
        "Ada Lovelace 1843",
        "Charles Babbage first described the engine to colleagues in 1837.",
    ),
    (
        "error_code",
        "The deploy failed with ERR_CONN_RESET until the proxy timeout was raised.",
        "ERR_CONN_RESET",
        "A later run failed with ERR_CONN_REFUSED because the port was closed.",
    ),
    (
        "concept_paraphrase",
        "The voltage briefly overshoots the rated ceiling before settling back down.",
        "momentary surge past the limit",
        None,
    ),
    (
        "cooking_procedure",
        "Rest the dough for forty minutes so the gluten relaxes before shaping.",
        "how long to rest the dough before shaping",
        "Chill the butter for ten minutes before cutting it into the flour.",
    ),
    (
        "history_quote",
        '"Nothing in life is to be feared, it is only to be understood," she wrote.',
        '"only to be understood"',
        "She wrote that fear often comes from what we do not yet understand.",
    ),
)

# Concept synonyms for the deterministic stand-in embedder: queries phrased with
# different words map to the same dimensions as their passages.
_CONCEPTS = {
    "surge": "overshoot", "surges": "overshoot", "spike": "overshoot",
    "overshoots": "overshoot", "excursion": "overshoot", "momentary": "brief",
    "briefly": "brief", "limit": "ceiling", "ceiling": "ceiling", "rated": "ceiling",
    "rest": "rest", "relaxes": "rest", "long": "duration", "minutes": "duration",
    "dough": "dough", "shaping": "shape",
}

HANDLE = "topic-synthetic-evidence-20260901-000000"


@dataclass(frozen=True)
class GoldWindow:
    video_id: str
    start_s: float
    end_s: float


@dataclass
class RetrievalCase:
    case_id: str
    stratum: str
    query: str
    gold: list[GoldWindow]


@dataclass
class SyntheticCorpus:
    handle: str
    video_ids: list[str]
    duration_s: float
    cases: list[RetrievalCase] = field(default_factory=list)
    segment_count: int = 0


def video_id_for(index: int) -> str:
    digest = hashlib.sha256(f"synthetic-video-{index}".encode()).digest()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return "".join(alphabet[b % 64] for b in digest[:11])


def _filler_sentence(rng: random.Random, words: int) -> str:
    chosen = [rng.choice(FILLER_WORDS) for _ in range(words)]
    chosen[0] = chosen[0].capitalize()
    return " ".join(chosen) + "."


def configure_storage(root: Path, setattr_fn: Callable[[Any, str, Any], None] = setattr) -> None:
    """Point every persisted artifact at `root` (pass `monkeypatch.setattr` in tests)."""
    from youtube_mcp_v2 import cache, corpus_index, skeleton
    from youtube_mcp_v2.adapters import audio_extract

    base = Path(root) / "youtube-mcp"
    setattr_fn(cache, "CACHE_DIR", base)
    setattr_fn(cache, "CACHE_PATH", base / "v2.sqlite")
    setattr_fn(skeleton, "CACHE_DIR", base)
    setattr_fn(skeleton, "SKELETON_DIR", base / "skeletons")
    setattr_fn(skeleton, "VECTOR_DIR", base / "vectors")
    setattr_fn(skeleton, "MODEL_DIR", base / "models")
    setattr_fn(corpus_index, "VECTOR_DIR", base / "vectors")
    setattr_fn(corpus_index, "INDEX_ROOT", base / "vectors" / "corpus")
    setattr_fn(audio_extract, "CACHE_DIR", base)
    setattr_fn(audio_extract, "AUDIO_DIR", base / "audio")


def build_corpus(
    *,
    videos: int = 24,
    video_duration_s: float = 1200.0,
    segment_s: float = 6.0,
    words_per_segment: int = 14,
    seed: int = 7,
    handle: str = HANDLE,
    stt_every: int = 5,
) -> SyntheticCorpus:
    """Write a frozen corpus + transcript revisions; return annotated cases.

    Every `stt_every`-th video is recorded as a local-STT transcript (captionless
    source) so strata mix acquisition methods. Needles are planted in distinct
    videos; each needle except the paraphrase also gets a second gold mention in
    another video, so recall@k is meaningful (two gold windows per case).
    """
    from youtube_mcp_v2 import cache, skeleton

    rng = random.Random(seed)
    ids = [video_id_for(i) for i in range(videos)]
    per_video = max(1, int(video_duration_s // segment_s))
    plan: dict[tuple[str, int], str] = {}
    corpus = SyntheticCorpus(handle=handle, video_ids=ids, duration_s=videos * per_video * segment_s)

    for n, (stratum, needle, query, negative) in enumerate(NEEDLES):
        mentions = 1 if stratum == "concept_paraphrase" else 2
        gold: list[GoldWindow] = []
        for m in range(mentions):
            video_index = (n * 3 + m * 7 + 1) % videos
            segment_index = rng.randrange(2, per_video - 2)
            while (ids[video_index], segment_index) in plan:
                segment_index = rng.randrange(2, per_video - 2)
            plan[(ids[video_index], segment_index)] = needle
            start = segment_index * segment_s
            gold.append(GoldWindow(ids[video_index], start, start + segment_s))
        if negative is not None:
            video_index = (n * 3 + 4) % videos
            segment_index = rng.randrange(2, per_video - 2)
            while (ids[video_index], segment_index) in plan:
                segment_index = rng.randrange(2, per_video - 2)
            plan[(ids[video_index], segment_index)] = negative
        corpus.cases.append(RetrievalCase(f"needle-{stratum}", stratum, query, gold))

    members = []
    for index, video_id in enumerate(ids):
        segments = []
        for seg in range(per_video):
            text = plan.get((video_id, seg)) or _filler_sentence(rng, words_per_segment)
            start = seg * segment_s
            segments.append(
                {"start_s": start, "duration_s": segment_s, "end_s": start + segment_s, "text": text}
            )
        corpus.segment_count += len(segments)
        text = " ".join(s["text"] for s in segments)
        local = stt_every > 0 and index % stt_every == stt_every - 1
        cache.put_transcript(
            video_id=video_id,
            lang="en",
            actual_lang="en",
            is_generated=local,
            text=text,
            segments=segments,
            word_count=len(text.split()),
            validated=True,
            warnings=["transcript fallback used: whisper.cpp"] if local else [],
            provider="whisper.cpp" if local else "youtube-transcript-api",
            method="local_stt" if local else "captions",
            attempts=[],
            details=(
                {
                    "schema": "youtube-mcp.local-stt/1",
                    "engine": "whisper.cpp",
                    "model": {"name": "ggml-fixture.bin", "revision": None, "sha256": "0" * 64},
                    "decode": {"language": "auto", "task": "transcribe"},
                    "audio": {"sha256": hashlib.sha256(video_id.encode()).hexdigest()},
                }
                if local
                else None
            ),
        )
        members.append({"id": video_id, "title": f"Synthetic source {index:03d}", "channel": f"Channel {index % 6}"})

    skeleton.save_skeleton(
        {
            "handle": handle,
            "target": "topic",
            "value": "synthetic evidence",
            "built_at": "2026-09-01T00:00:00Z",
            "expired_at": None,
            "source": "scrape",
            "videos": members,
        }
    )
    return corpus


class HashingEmbedder:
    """Deterministic stand-in for a dense text model (NOT a quality model).

    Feature-hashes normalized tokens (with a tiny concept map so paraphrases
    share dimensions) into `dim` buckets. It lets benchmarks and tests exercise
    hybrid indexing/fusion mechanics offline; real semantic quality comes from
    the optional `semantic` extra.
    """

    def __init__(self, dim: int = 64, model_id: str = "fixture/hashing-concepts-v1") -> None:
        self.dim = dim
        self.model_id = model_id

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            if token in _FILLER_SET:
                continue
            token = _CONCEPTS.get(token, token)
            bucket = int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dim
            vector[bucket] += 1.0
        if not any(vector):
            vector[0] = 1e-3
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector]

    def embed_passages(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)
