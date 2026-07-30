# ADR-004 — Truncate over-long recordings; never reject them

**Status:** Accepted
**Date:** 2026-07-29

## Context

A recording arrived that was **39.6 minutes long** (9.5 MB). Transcription failed:

```
audio duration 2376.44 seconds is longer than 1400 seconds
which is the maximum for this model
```

The record was quarantined as `failed` and, by design, never retried.

The cause was ordinary operator error — the recorder was started and not
stopped. **Only the first ~12 seconds was an actual command**; the remaining 39
minutes was ambient conversation that happened near the device.

Three separate problems were exposed:

1. **No duration guard.** Both the processor and the standalone transcription CLI
   checked file *size* against the 25 MB API limit but not *duration*, so a 9.5 MB
   upload was spent to be told the audio was too long. The upstream listing
   already reports duration, so this check was available for free.
2. **A real command was lost.** The recording contained a genuine instruction and
   the system discarded all of it.
3. **Unbounded exposure.** Thirty-nine minutes of a person's day was uploaded to
   a third-party transcription API, and would have been handed to an autonomous
   agent as its prompt.

## Decision

**Truncate to the first `ATTICUS_MAX_COMMAND_SECONDS` (default 180) and carry on.
Do not reject.**

A second guard, `ATTICUS_MAX_INGEST_SECONDS` (default 7200), refuses to download
pathologically long recordings at all — the listing reports duration before any
bytes are fetched.

The cut is made with `ffmpeg -c copy` into a temporary file. The original is
never modified, the temp file is removed on exit including on failure, and the
record's metadata gains `truncated_from_seconds` and `transcribed_seconds` so
the event is auditable rather than invisible. A truncation raises a notification.

## Why truncate rather than reject

**Rejecting silently discards real instructions.** This is the same failure class
as a misheard wake word: a genuine command disappears and nothing tells the
operator. That failure had already been observed once. Building a second path to
it would have been a mistake.

**Truncation is lossless for this workload.** A command is 10–30 seconds and the
**wake phrase must come first** — the gate only inspects the opening words. So
the first N seconds contains all of the signal by construction, and everything
after it is silence or ambient audio by definition.

**It is also a security bound, and that is the stronger argument.** The premise
of this system is that anything spoken near the device can become an instruction
to an autonomous agent. Without a cap, a runaway recording determines how much
of the operator's life reaches the transcription API and the agent's prompt.
With one, that quantity is bounded by configuration rather than by whether
someone remembered to press stop.

Validated on the real 39.6-minute file: 9,506,384 bytes / 2376 s → 720,512
bytes / 180.1 s, original untouched, temp cleaned, and the previously-lost
command recovered and executed successfully.

## Consequences

**Known incomplete.** Truncation bounds the *audio*, not the *prompt*. The
recovered recording produced a 389-word transcript of which roughly 25 words
were the command; the remaining ~365 were ambient conversation, and all of it
reached the agent. In that instance the agent correctly ignored the digression —
the published report contained zero references to it — but that is a favourable
observation, not a guarantee. The transcript even contained the phrase *"hey
Atticus, send a signal message to Bill"*, spoken as an example of a future
capability. No Signal skill exists yet, so nothing could act on it. **Once one
does, that sentence becomes an executable instruction sitting in an unrelated
task's prompt.**

Scoping the prompt — a sentence cap after the wake phrase — is the natural
follow-up. The full transcript is always retained on disk, so bounding what
reaches the agent costs nothing.

**Chunking is deferred, not rejected.** A 40-minute meeting is a legitimate thing
to hand this system, and truncation is the wrong answer for it. Splitting on time
with overlap is the right one. Until then a long recording is treated as a
command with a long tail, which is the common case, rather than as a document,
which is not yet supported.

**180 seconds is a judgement, not a measurement.** It is generous for a 10–30
second command. A tighter cap saves a few cents per accident; the thing it risks
is clipping a genuinely long dictated task. It is one environment variable.
