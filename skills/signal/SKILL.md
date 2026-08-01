---
name: signal
description: |
  Sends a short Signal message to one specific person on a pre-approved
  allowlist — "tell Robbie I'm running twenty minutes late", "let Nadia know
  the call moved to four", "message Robbie that I'll bring the drive". Use ONLY
  when the request names a person and asks for them to be told something, and
  only for a message the operator would recognise as their own words. Produces
  an intent file; the pipeline sends it, and only after the operator confirms.
  Do NOT use it to reply to, read, or check Signal messages (not implemented).
  Do NOT use it to notify the operator themselves — the pipeline already pushes
  a notification. Do NOT use it to broadcast, to message someone whose name is
  not on the allowlist, or as a way to report your own results: your HTML
  report is how the operator finds out what you did.
---

# signal

You have been asked to tell a person something. **You cannot send anything**,
and you are not the last check before it goes out — the operator confirms it
first. Your job is to write the request precisely enough that they can approve
it at a glance, and to refuse rather than guess when you are not sure who is
meant.

Everything about this skill follows from one fact: **the request came from a
microphone worn in public, through a speech-to-text model, and names are the
words transcription gets wrong most often.** A wrong recipient here is not a bad
report someone ignores. It is a private message, delivered to a real person,
immediately, with no recall.

## The request

One file per message, in `./output/outbox/`:

```json
{"verb": "signal.send", "to": "Robbie", "body": "Running about twenty minutes late — start without me."}
```

- `to` — the recipient's name **exactly as the allowlist spells it**, or their
  number in `+15551234567` form. One recipient. Never a list.
- `body` — the message itself, under 1000 characters.

## Who you may write to

The pipeline holds an allowlist of permitted recipients. **A name that is not on
it is refused, not looked up and not approximated** — there is no fuzzy
matching, and "Nadya" will not resolve to "Nadia". There is no contact directory
yet, so you cannot discover a number, and you must not put one in `to` that you
did not get from the operator's own words.

The allowlist is not visible from in here, so you will not always know whether a
name is on it. That is fine and it is the right way round: write the request for
the person who was actually named, and if it is refused the receipt says so
plainly and the operator sees exactly who was meant.

**What you must not do is substitute.** If the transcript is unclear about who —
two names, a half-caught name, "tell her", a name that seems to be someone else
misheard — do not pick the most likely person. Write **no** outbox file, and say
in your report what you heard and what you would have sent. An unsent message
costs the operator ten seconds. A message to the wrong person cannot be undone.

## Writing the message

**It is the operator's message, not yours.** First person, their register, the
words they used. No "Atticus here", no signature, no "Gregg asked me to tell
you", no explaining that this was automated — the recipient sees a normal
message from a person they know.

**Send only what was asked.** Do not add greetings, apologies, emoji, context
the operator did not give, or a follow-up question that invites a reply nobody
will read. If they said "tell Robbie I'm running twenty minutes late", the
message is that and nothing else.

**Clean up the speech, keep the meaning.** Drop the false starts and the "um".
Fix the punctuation. Do not upgrade the tone, soften a refusal, or resolve an
ambiguity the operator left in — if what they said is ambiguous, the ambiguity
is theirs to keep, and if it is *unintelligible*, do not send it at all.

**Short.** Messages over 1000 characters are refused outright rather than cut,
because half a message to a person can mean the opposite of the whole. If the
content is genuinely long, it belongs in the report, and the message says the
report exists.

## Several people

There is no way to send one message to several recipients, deliberately. If the
operator clearly named more than one person and clearly wanted each told, write
**one file per person** — `001-signal.send.json`, `002-signal.send.json` — with
each recipient named individually. A combined `to` is refused.

Be conservative about this: fan-out is how one misheard sentence becomes several
mistakes, and the pipeline caps the number of actions per pass, so extra files
may be refused anyway. Two is plausible. Five means you have misread the
request.

## In your report

The message is **pending**, not sent. Say so in those words. Write out the
recipient and the exact text you asked to be sent, so the operator can check it
against what they meant without opening the receipt — and note anything you were
unsure about, especially about who.

## Setup, for the operator reading this

`signal-cli` is not installed on this host, so `signal.send` currently refuses
with instructions rather than sending. Install it, then either link this box as
a secondary device to your own account (recipients recognise the sender; the box
can send as you) or register a dedicated number (cleaner containment; nobody
recognises it). Full steps, the trade-off, and the containment note about
signal-cli's state *directory* are in the module docstring of
`processor/handlers/signal.py`. `ATTICUS_SIGNAL_RECIPIENTS` is mandatory: with
it empty, every send is refused.

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
