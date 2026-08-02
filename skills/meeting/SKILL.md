---
name: meeting
description: |
  Turns a recorded meeting into a written record — decisions, action items,
  open questions — and files the operator's own action items on his todo list.
  Use ONLY when the transcript is a MEETING or a long multi-person
  conversation, which in practice means it opened with "meeting mode", "take
  notes on this meeting", or similar, and then continues into other people
  speaking. Do NOT use it for an ordinary spoken command, however long; do NOT
  use it for a solo dictation or a voice memo (those are notes); do NOT use it
  to research anything mentioned in the meeting (that is deep-research, and
  only if asked); and do NOT send anything derived from a meeting to Slack, a
  GitHub issue, an ADO work item or any other person — a misheard sentence from
  somebody else's mouth must never become a message in a channel they are in.
verbs: [todo.add]
requires: [ATTICUS_MEETING_MODE]
risk: internal
outputs: [html]
cost: medium
---

# meeting

Several people talked for a long time and one of them was wearing a recorder.
Your job is the written record they would have wanted and nobody made: what was
decided, what someone now has to do, and what is still open.

## Read ADR-008 first, in spirit

This is the only skill whose input is **other people**. They did not agree to
be transcribed by an AI, filed in a git repository, or summarised by an
autonomous agent. Two consequences for how you write:

- **Attribute carefully or not at all.** Transcription confuses speakers. If
  you are not confident who said something, write the decision without a name
  rather than putting words in the wrong mouth.
- **Do not editorialise about people.** Record what was decided and what is
  owed. Not who seemed unprepared, not who disagreed with whom, not tone.

The audio is deleted once transcribed; your report and the transcript are the
only lasting record, which is a reason to make the report accurate rather than
vivid.

## The report

One self-contained HTML file in `./output/`, in this order — most useful first,
because it will be read on a phone within the hour:

1. **Decisions** — what was actually settled. If nothing was, say so; a meeting
   with no decisions is a real and useful finding.
2. **Action items** — one line each: what, who, by when. Mark clearly which are
   the operator's own.
3. **Open questions** — things raised and not resolved, with enough context to
   pick up later.
4. **Notes** — the substance, organised by topic rather than chronologically.
   Nobody wants a transcript reflowed; they want the shape of the discussion.
5. **Anything you could not make out.** Say so explicitly. A meeting transcript
   has crosstalk and half-audible passages, and a gap you flag is far better
   than a confident guess.

Lead with a two-sentence summary. Do not pad — a short accurate record beats a
long one, and length here is a cost the operator pays in reading time.

## Action items go on the operator's list, and only the operator's

For each action item **that is the operator's own**, write one `todo.add`:

```json
{"verb": "todo.add",
 "title": "Send the migration timeline to the client",
 "note": "meeting 2026-08-02: agreed to send before Friday's call",
 "due": "2026-08-07",
 "list": "Client work"}
```

- **Only their items.** Do not create todos for other attendees' commitments.
  Mention those in the report; they belong to people who did not ask for a todo
  list.
- **Resolve dates the same way the todo skill does** — a real `YYYY-MM-DD` or
  nothing, with the spoken wording echoed in the note.
- **Number them in order**, `001-todo.add.json` onward. A meeting may legitimately
  produce a dozen; the cap is raised for this path, so do not silently drop any.
  If there really are more than twenty, file the most important twenty and say
  in the report which were left out and why.

## Then write your report as pending

The todos are performed by the pipeline after you exit, so say "filed N action
items to the todo list" rather than claiming they exist. The report is the
deliverable; the todos are a convenience on top of it.

## Causing something to happen outside this sandbox

You hold no credentials and you cannot reach any external service. To make
something happen, declare the intent and the pipeline performs it after you exit.

Write one JSON file per action into `./output/outbox/`, named `NNN-verb.json`
where `NNN` is a zero-padded sequence number that sets the order they run in:

```json
{"verb": "<service>.<action>", "...": "action-specific fields"}
```

Rules:

- **One action per file.** Never a list.
- **The verb must be one the pipeline knows.** An unknown verb is refused and
  reported, not silently dropped — so do not invent one.
- **Ordering is the filename.** `001-` runs before `002-`.
- Anything outward-facing may be **held for confirmation** rather than performed
  immediately. That is normal and not a failure. Write your report as though the
  action is pending, never as though it is done.
- Also write your usual HTML deliverable. The outbox is in addition to it, not
  instead of it: the report is what the operator reads to find out what you did.

`todo.add` is the **only** verb this skill may write. Everything else a meeting
might seem to call for — telling somebody, filing a ticket, sending a summary —
involves other people, and ADR-008 §4 keeps meeting-derived content off those
channels. Put it in the report and let the operator decide.
