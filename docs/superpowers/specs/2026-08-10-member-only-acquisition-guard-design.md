# Member-only acquisition guard

## Purpose

Do not attempt local audio extraction or Whisper transcription after yt-dlp reports
that a video requires a paid channel membership.

## Contract

Affected tools: `transcript.get` and `corpus.hydrate` through their shared
`acquire_transcript` waterfall.

Inputs and result envelope stay unchanged. For an explicit membership rejection,
the yt-dlp attempt is recorded as `outcome: "unavailable"` and
`detail_code: "membership_required"`. No `whisper.cpp` attempt is recorded and
no media is downloaded. The terminal failure remains recoverable because a future
authorized session could access the video.

## Design

Add a narrow membership predicate in `transcript_acquisition` that recognizes
the explicit yt-dlp YouTube phrases `members-only`, `members only`, and
`join this channel to get access`. It is intentionally evaluated before generic
429/sign-in classification. When the fallback failure has this classification,
return a `TranscriptAcquisitionFailed` immediately after recording the fallback
attempt. Existing no-transcript, rate-limit, and local-STT behavior is unchanged.

## Verification

A regression test will simulate a primary caption failure followed by a
members-only yt-dlp failure with local Whisper configured. It must assert the
two-attempt provenance, `membership_required`, and that neither local settings
nor local transcription is called.
