---
name: todo
description: |
  Adds an item to Gregg's todo list — the list in his vault, which he reads on
  his phone and in the vault browser. Use when the transcript asks to put
  something on a list, remind him to do something, add a task, add a to-do, or
  "don't let me forget" — "add picking up the prescription to my list", "remind
  me to renew the domain", "put milk on the shopping list". Writes an outbox
  request; the pipeline files the item after you exit. Do NOT use this to
  research or decide anything (that is a normal task or `deep-research`), do
  NOT use it for a calendar event with a time and a duration, and do NOT use it
  to file work for other people to see — a GitHub issue or an ADO work item is
  a different verb with a different gate.
verbs: [todo.add]
risk: internal
outputs: [html]
cost: low
---

# todo

Someone said one sentence into a wearable pin and walked off. The whole job is
that the right item is waiting on their list, worded so it still makes sense
tomorrow. That is a small job and it should stay small: **one spoken request is
one task.**

## The backend is the vault's own list

The list is a ledger in the vault, rendered by the vault site and checked off in
the vault browser — the same browser the operator already uses on their phone.
Decided in issue #51 (ADR-007): the pin is the capture path and the browser is
the view, so no external app, no credential, and nothing you write here leaves
the operator's own infrastructure.

You do not write the list. Do not create a `todo.md`, a checklist, or any file
pretending to be the list in your output directory — the pipeline appends to the
real ledger after you exit, and a second list that looks like the first is worse
than none.

## Write the request

One file, `./output/outbox/001-todo.add.json`:

```json
{"verb": "todo.add",
 "title": "Pick up the prescription",
 "note": "spoken 2026-08-01: \"and grab the prescription while you're out\"",
 "due": "2026-08-07",
 "list": "Shopping"}
```

| field | required | what it is |
|---|---|---|
| `verb` | yes | always `todo.add` |
| `title` | yes | the task, as a short imperative. Trimmed to 255 characters. |
| `note` | no | context that does not belong in the title, including the original wording |
| `due` | no | a calendar date, `YYYY-MM-DD`, and nothing else |
| `list` | no | a grouping label, shown as a section on the list. Omitted → the main list. |

### The title is the whole task

Write what a person would write, not what the microphone heard.

- **Imperative, specific, self-contained.** "Pick up the prescription", not
  "prescription" and not "Gregg said he needs to pick up the prescription".
- **Drop the wrapper.** "Atticus, add X to my list" → the task is `X`.
- **Fix obvious mistranscriptions** and say so in the note when you are not
  sure. "Renew the atticus.dev domain" from "renew the attic us dev domain" is a
  fix; inventing a detail you did not hear is not.
- **Do not put the date in the title** — that is what `due` is for.
- One task per request file. If the sentence genuinely contains two errands,
  write `001-todo.add.json` and `002-todo.add.json`.

### Vague dates: resolve, or leave it out and say so

The transcript rarely says a date. It says "by Friday", or "before the trip", or
nothing. The rule:

- **A phrase that resolves against today's date, resolve it.** "By Friday",
  "tomorrow", "end of the month" → compute the actual date and put it in `due`.
  Recording usually reaches the pipeline within half an hour of being spoken, so
  today's date is the day it was said.
- **Always echo the spoken wording in the `note`** when you resolved a relative
  phrase — `spoken "by Friday" → 2026-08-07`. If the recording sat on the pin
  unsynced for days, that line is the only way the operator can spot a due date
  that landed on the wrong week.
- **A phrase that does not resolve to a day, leave `due` out.** "Soon", "when I
  get a chance", "before the trip", "next quarter". Put the phrase in the note
  instead. A missing due date is a mild annoyance; a wrong one is a deadline
  that passes silently or an alarm that fires for nothing.
- **Never send a phrase in `due`.** The pipeline refuses anything that is not
  `YYYY-MM-DD`, and the task is then not created at all.

There is no time-of-day. A due date is a date; if the request really has a time
and a place, it is a calendar event and this is the wrong skill.

### Lists

Omit `list` and the item goes on the main list, which is the right answer almost
always. Name a list only when the transcript names one ("put it on the shopping
list"), and use the name as spoken. It is a plain label — the rendered page
groups by it, so even a misheard "Groseries" sits two lines from everything else
in plain sight rather than becoming a list nobody opens.

## Then write the report

Also write `./output/index.html`, short — a couple of sentences. It is what the
operator reads to find out what happened, so state the task exactly as you sent
it, the due date if any, the list if you named one, and any wording you fixed or
could not resolve. Write it as **pending**, not done: you do not perform the
action and cannot know that it worked. If the transcript was too vague to make a
task from, write only the report saying so and no outbox request.

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

`todo.add` is classed **internal** — only the operator sees it and undoing it is
one tap — so it is performed unattended rather than held, and it needs no
credential, so there is nothing to consent to and no token that can be missing.
The one way it fails is a malformed `due`, and the receipt says so; follow the
date rules above and it will not.
