---
name: atticus
description: |
  Controls Atticus itself — reports what happened to an earlier request,
  cancels one, or runs one again. Use when the spoken request is ABOUT a
  previous recording rather than a new piece of work: "what happened to the
  research I asked for this morning", "did that report finish", "cancel the
  thing I just started", "stop that", "never mind, forget the last one", "try
  that again", "re-run the consulting research". You do NOT perform any of it:
  you write an intent file and the pipeline resolves which recording was meant
  after you exit, refusing if the words match none or several. Do NOT use this
  skill for new work of any kind, do NOT use it to change what an earlier
  request ASKED for (there is no amend — cancel it and say the new request),
  and do NOT use it to close a GitHub issue or check off a todo, which are the
  github and todo skills.
verbs: [atticus.status, atticus.cancel, atticus.retry]
risk: internal
outputs: [html]
cost: low
---

# atticus

Everything else here starts work. These three verbs act on work already
started, which is the one thing the operator could not do from the car until
now.

You cannot see the pipeline's state. You never learn which recording was meant,
whether it was found, or what its status turned out to be — the pipeline
resolves the words after you exit and tells the operator directly. Write your
report as **requested**, never as answered.

## The three verbs

| verb | does | required | optional |
|---|---|---|---|
| `atticus.status` | pushes the operator a status line | — | `match` |
| `atticus.cancel` | stops it, as far as it can still be stopped | `match` | — |
| `atticus.retry` | re-arms it for the next pass | `match` | — |

```json
{"verb": "atticus.cancel", "match": "the consulting research"}
```

## `match` is the words that identify it, and nothing else

Put the distinguishing words from the request in `match`. The pipeline scores
them against each recent recording's title and transcript and **refuses unless
exactly one matches** — no match and several matches both fail, with the
candidates named in the receipt.

- **Use the specific words.** `"consulting research"` finds it; `"the thing"`
  will not.
- **Strip the instruction.** "Cancel the report about Elm Lake" → `match` is
  `"report about Elm Lake"`, not the whole sentence.
- **"The last one" is legitimate.** If the request genuinely says "cancel that"
  or "the last one" with nothing distinguishing, pass `"the last one"` — the
  pipeline reads that as *most recent* rather than trying to match text.
- **Never invent a recording.** If the request names something you have no
  words for, say so in the report instead of guessing at a phrase.
- Only the last seven days are searchable, which is deliberate: "that thing"
  means something recent, and a wider window makes ambiguity certain.

## What cancel actually does, so your report is honest

It depends on how far the work got, and **you cannot know which case applies**:

- not started yet → abandoned, nothing was spent;
- running → the run is stopped mid-flight;
- **already published → marked *superseded*, not cancelled.** The report stays
  where it is. It is committed, and the operator may already have read it, so
  pretending it can be withdrawn would be false.

So write "asked Atticus to cancel it" and let the receipt say what happened.
Never write "cancelled it".

## There is no amend, and no reopen

Changing what an earlier request asked for is not a verb. If the operator wants
something different, that is a cancel plus a new request — two clear actions
instead of one that guesses which parts carry over. Say that in your report if
it is what was asked for.

## Then write your report

Short. State which recording you think was meant, in the operator's own words,
and what you asked the pipeline to do with it. If the words were too vague to
identify anything, write only the report saying so and no outbox request — a
guessed `match` can cancel work the operator wanted.

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

These three verbs are classed **internal** — they touch nothing outside the
operator's own pipeline, no credential and no other person — so they run
unattended rather than being held. A cancellation that waited for approval
would have failed at the one thing it is for.
