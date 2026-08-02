# ADR-008 — Recording other people: the consent policy meeting mode needs

**Status:** Accepted (2026-08-02)
**Decision by:** the operator, on the reasoning in "The operator's decision" below
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

## The operator's decision

Recorded verbatim in substance, because it changes what §1 says and it is the
reason this ADR is Accepted rather than abandoned:

> "I don't worry about any type of announcement. The device is not capable of
> implementing that itself anyway. It's the responsibility of the recorder to
> follow all local laws in any recording situation. It is probably not my
> personal attempt to record meetings, but I like the functionality in the
> application in case others want to use it. In pretty much every indication in
> every incident, I'll be using Teams or Zoom to record meetings, not a device
> like this. This device is mostly intended for personal to-dos, reminders,
> investigations, that sort of thing, to capture those when I'm on the go."

Two things follow. **The announcement is the recorder's responsibility and this
software will not pretend to enforce it** — a pin has no screen, no speaker and
no way to tell a room anything, so any claim to the contrary would be theatre.
And **the primary use of this device is not meetings**; meeting mode exists
because it is a reasonable thing for someone else to want, not because it is
the workload this system is tuned for. Teams and Zoom already record meetings,
with the participant notice built into the platform, and they are the better
tool for it.

## Decision

Meeting mode ships **off**, and turning it on is an explicit act with these
conditions attached.

### 1. Announcing is the operator's responsibility, and is not enforced

Whoever runs this is responsible for complying with the recording law wherever
they are — which in two-party-consent jurisdictions, California among them,
means telling the room. **Atticus does not and cannot enforce that.** The device
has no way to announce anything, no software here checks, and no setting
attests to it.

That is stated rather than solved because the honest alternative is a checkbox
that changes nothing while implying diligence. If you switch this on, the
obligation is yours.

Worth knowing about the tool you are choosing: a wearable pin is a poor fit for
meeting capture, and the platforms that host the meeting are a better one. Teams
and Zoom record with the participant notice built in, produce cleaner audio with
per-speaker separation, and leave the recording where the other attendees can
see that it exists.

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

## Why it stays off by default despite being Accepted

Accepted means the feature is legitimate and may be enabled. It does not mean it
should be on for this operator, who has said plainly he will use Teams or Zoom
instead. `ATTICUS_MEETING_MODE=off` remains the shipped default, the skill is
not even offered to the agent while it is off, and enabling it is one line in
`ops/.env` for whoever wants it.

## Revisit if

- the operator starts using the pin for meetings in practice, in which case the
  retention rule in §2 is the part to re-examine first;
- a jurisdiction the operator works in requires a recording *artifact* of
  consent rather than a spoken announcement — this ADR's position would then be
  insufficient rather than merely unenforced.
