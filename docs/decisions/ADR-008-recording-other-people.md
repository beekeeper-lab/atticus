# ADR-008 — Recording other people: the consent policy meeting mode needs

**Status:** **Proposed — needs the operator's decision before meeting mode is enabled**
**Date:** 2026-08-02
**Issue:** [#86](https://github.com/beekeeper-lab/atticus/issues/86)
**Related:** ADR-004 (truncation), `ops/retention.py`, `SECURITY.md`

## Why this ADR exists before the feature does

Meeting mode is the highest-value item in the 2026-08-02 functional review —
action items from client calls landing on a todo list is worth more than most
of the rest of that document combined, and the machinery is nearly all built
already (chunked transcription exists, `todo.add` exists, the extraction is a
prompt).

**The barrier is not technical and it is not cost.** Sixty minutes of audio at
`gpt-4o-transcribe` is about $0.36; the earlier estimate that cost would be the
obstacle does not survive arithmetic. The barrier is that a meeting contains
**people who did not agree to any of this**: to being transcribed by an AI, to
being summarised by an autonomous agent, to being committed to a git repository,
or to being published to a browser.

Everything else in this system records the operator talking to themselves.
Meeting mode is the first feature whose input is other people, so it is the
first that needs a policy rather than a setting.

## What the current system would do with a meeting, today

Worth stating plainly, because parts of it are already true:

- **The vault already contains one 40-minute ambient recording** whose
  transcript drifts into conversation after the command. That was accidental
  and it is still there.
- `ATTICUS_AUDIO_RETENTION_DAYS=30` removes audio **from the working tree, not
  from git history**. `ops/retention.py` says so in its own comments. So
  "expires after 30 days" is not what a reasonable person would understand by
  that phrase.
- Transcripts and agent output are **never** expired, by design.
- The vault browser publishes to the tailnet, and any document is searchable
  from any tailnet peer.

So the honest description of today's behaviour is: *anything the pin hears may
be transcribed to a private repository and kept permanently.*

## Decision (proposed)

Meeting mode ships **off**, and turning it on is an explicit act with these
conditions attached.

### 1. Announce, always

The operator states that the meeting is being recorded and transcribed by an AI
assistant, before recording, to everyone present. Not a legal position — a
courtesy that also happens to be the law in two-party-consent jurisdictions,
which include California, where a West Coast consulting practice will hold
calls.

This is not something software can enforce, so it is written down here as the
condition under which the feature was built rather than pretended to be a
control.

### 2. The audio is never committed

Meeting audio is transcribed and then **deleted**, not retained for 30 days and
not written into git history. This is the one condition with real engineering
consequence, and it is the reason `ATTICUS_MEETING_KEEP_AUDIO` exists and
defaults to `false`.

Rationale: the 30-day retention story is already weaker than it sounds for the
operator's own voice. For third parties it would be indefensible — a permanent,
searchable recording of someone else's speech, in a repository they cannot see,
have not agreed to, and cannot ask to be removed from without a history rewrite
the README forbids.

### 3. Transcripts stay, and that is a deliberate line

The transcript and the derived report are retained like any other Atticus
output. This is the trade: the value of the feature is the durable record, so
retaining nothing would mean building nothing. A transcript is also
meaningfully less exposing than audio — it carries the words but not the voice,
the tone, or the biometric identifiability.

### 4. Never a shared or public channel by default

Meeting output is `internal` risk. Action items may flow to the operator's own
todo list unattended; nothing derived from a meeting may reach Slack, a GitHub
issue, an ADO work item or a message to anyone else without passing the
approval queue (#83). A misheard sentence from someone else's mouth must not
become a post in a channel they are in.

### 5. Off by default, per-recording opt-in

Meeting mode requires both `ATTICUS_MEETING_MODE=on` **and** an explicit spoken
opening ("Atticus, meeting mode" / "take notes on this meeting"). Duration is
deliberately **not** a trigger: a long recording today is a truncated command
(ADR-004), and silently switching behaviour on length would turn a mis-fired
command into a transcribed meeting nobody asked for.

## Alternatives considered

**Ship it with the ordinary 30-day audio retention.** Rejected on the grounds
in §2: for a third party, "removed from the working tree but present in git
history forever" is not retention, it is filing.

**Require per-attendee consent capture.** Considered and rejected as theatre in
this context: a solo operator with a wearable cannot meaningfully collect and
store consent records, and building a UI for it would suggest a rigour the
system does not have.

**Do not build it.** Genuinely on the table, and the reason this ADR is
Proposed rather than Accepted. The operator may reasonably decide the value
does not justify recording clients at all. The feature is built to be inert
until that decision is made.

## Consequences

- One new setting pair: `ATTICUS_MEETING_MODE` (off) and
  `ATTICUS_MEETING_KEEP_AUDIO` (false).
- `retention.py` gains nothing: meeting audio is deleted at the end of the
  transcribe stage rather than expiring, so it never enters the retention
  system at all.
- The `meeting` skill is `internal` risk and its action items route only to
  `todo.add`.
- **If this ADR is rejected**, delete `skills/meeting/`, the meeting branch in
  `processor/transcribe.py`, and both settings. Nothing else depends on them.

## Open question for the operator

Do you have the standing to record the meetings you would use this for, and are
you willing to announce it every time? If yes, this becomes Accepted and the
setting goes on. If no, the honest outcome is deleting the feature rather than
leaving it configurable.
