# Product thesis — evidence-grade access to knowledge encoded in video

youtube-mcp-v2 is not an engineering MCP, a creator-automation suite, or a collection of YouTube API wrappers.

Its product goal is:

> **Give an AI a trustworthy, efficient, domain-neutral research interface to knowledge that happens to be encoded in video.**

YouTube is the source medium. The object of research may be anything.

## Domain neutrality

No discipline receives privileged architectural treatment. The same acquisition, freezing, retrieval, provenance, and multimodal primitives should work for:

- history, religion, philosophy, and primary-source interpretation,
- science, mathematics, technology, and technical explanation,
- politics and current-affairs source recovery,
- cooking and craft technique comparison,
- product/reviewer consensus and disagreement,
- literature, film, music, games, art, and creative research,
- practical tutorials, repair, construction, and demonstration,
- sports and performance analysis,
- interviews, podcasts, testimony, and long-form discourse,
- cross-domain synthesis around concepts such as wisdom, failure, frontier behavior, trust, resilience, beauty, or institutional change.

A capability is valuable when it improves evidence retrieval across these classes, not because it serves one favored workflow.

## Research model

The intended loop is:

```text
discover sources
    ↓
freeze or compose a research corpus
    ↓
hydrate only the evidence needed
    ↓
build immutable transcript / visual indexes
    ↓
retrieve a small timestamped evidence set
    ↓
reason outside the MCP
```

The MCP retrieves and structures evidence. The calling model performs synthesis, judgment, interpretation, and argument.

## Why corpus composition matters

Interesting questions often cross channel and disciplinary boundaries. A research set may intentionally combine:

- a historian's lecture,
- an archival documentary,
- a political speech,
- a philosopher's interview,
- a cooking demonstration,
- a sports coach's explanation,
- and a game-design retrospective.

`corpus.compose` exists so this is a first-class operation rather than an accidental use of a topic-search snapshot.

Composed corpora are immutable revisions. They can include existing frozen corpora and direct video IDs/URLs, derive from an earlier collection, remove selected members, and preserve composition provenance without making live network calls.

## Evidence types

The system should preserve distinctions between evidence rather than flattening everything into one opaque score.

### Spoken evidence

What was said, when it was said, how the transcript was acquired, and how trustworthy the transcript appears.

### Visual evidence

What was shown at a timestamp: people, objects, maps, demonstrations, artwork, charts, slides, game/UI states, physical processes, and other imagery.

### On-screen symbolic/text evidence

Exact strings visible in pixels: names, dates, quotations, citations, prices, ingredient quantities, statute/case numbers, scientific notation, model identifiers, scores, UI labels, subtitles, error codes, and similar text.

This is why lexical retrieval remains important even in a semantic system. Exact evidence is not an engineering-specific requirement.

## Product invariants

- **Evidence before summaries.** Generated prose is not a substitute for the source moment.
- **Domain-neutral primitives.** Core behavior must not assume a subject matter.
- **Frozen research state.** Multi-step work should be reproducible against explicit corpus/index revisions.
- **No silent fallback.** Acquisition changes may be automatic, but provenance must reveal them.
- **No surprise heavy work.** Search should not unexpectedly trigger model downloads or unbounded media processing.
- **Separate evidence modalities.** Speech, pixels, OCR text, and metadata retain their own provenance/confidence.
- **Small agent-facing surface.** Tool count is not a competitive objective.
- **Local-first, remote-capable.** Local media/ML is an advantage; protocol-native resources keep evidence usable remotely.
- **Benchmark breadth.** Market-leadership claims require strong results across domains as well as difficult media conditions.

## What success looks like

A model should be able to ask questions such as:

- "How do these historians disagree about why this revolt began?"
- "Find every place this guest changed their position over five years of interviews."
- "Across these recipes, when is the acidic ingredient added and what effect do the cooks claim it has?"
- "Which reviewers independently demonstrate the same product failure rather than merely repeating a spec sheet?"
- "Find the map where the documentary shows the border being discussed."
- "Where does the score change immediately after this tactical decision?"
- "Compare how a philosopher, coach, entrepreneur, and biologist describe productive failure."

The result should be a bounded set of source-linked, timestamped, provenance-bearing evidence—not an opaque answer that cannot be checked.
