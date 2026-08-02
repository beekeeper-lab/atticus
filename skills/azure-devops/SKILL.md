---
name: azure-devops
description: |
  Files and updates Azure DevOps work items from a spoken request, and writes an
  HTML record of what was filed. Use when the transcript asks to file, open,
  raise, create, or log a ticket / bug / task / story / work item — "file a
  ticket on the DDI project that does X", "open a bug for the login timeout" —
  or to add a comment or update to an existing item by number ("add a note to
  work item 4711 saying we shipped it"). Produces one JSON intent file per
  action in `output/outbox/` plus the HTML record; the pipeline performs the
  action after you exit, because you hold no credentials. Do NOT use it for
  GitHub issues, for a personal reminder or todo that nobody else needs to see,
  or to look anything up in Azure DevOps — you cannot read from ADO at all.
verbs: [ado.workitem, ado.comment]
requires: [ATTICUS_ADO_PAT, ATTICUS_ADO_ORG, ATTICUS_ADO_PROJECT]
risk: tracked
outputs: [html]
cost: low
---

# azure-devops

Someone said "file a ticket that does X" and walked away. Two things have to come
out of this: **the ticket gets filed**, and **there is a record saying what was
filed** that they can read later on a phone without opening Azure DevOps.

You do the second one and *declare* the first.

## What you can and cannot do

**You cannot reach Azure DevOps.** No PAT, no network path, and no amount of
`curl` will change that — it is the design, not an oversight. You write an intent
file; the pipeline holds the credential and performs it afterwards.

**You cannot read from ADO either.** Not "what's assigned to me", not "is 4711
still open", not "does this duplicate an existing item". If the request depends on
looking something up, say so plainly in the record and file the best item you can
from what was said, or file nothing and explain why. Do not guess at the answer to
a query you could not run.

**You do not choose the project.** Neither does the person speaking, in effect —
the organisation, project, area path and iteration all come from the operator's
configuration. You cannot see them and must not try to set them. If the transcript
names a project ("on the DDI project"), treat it as *confirmation of intent*, not
as a routing instruction: repeat it in the record, and note that the item goes to
the configured project. This is deliberate. A misheard project name would file
into some other team's backlog, which is the one way a recoverable mistake becomes
an expensive one.

## Deciding what to file

**One request, one work item.** A sentence that describes one piece of work is one
ticket. Do not split "fix the login timeout and add a test" into two items unless
the speaker clearly asked for two — the person who reads the backlog can split it,
and duplicates are worse than a slightly broad title.

**A new item or a comment on an existing one?** If the transcript names a number
("add to work item 4711", "update 4711 saying…"), that is `ado.comment`. Numbers
survive transcription poorly, so require an explicit digit string; "the deploy key
one" is not an id and cannot be resolved from here — file nothing and say so.

**The title is the deliverable.** It is what everyone sees in a backlog list.
Imperative, specific, under about 80 characters, no "Atticus:" prefix, no
transcription artefacts. "Rotate the vault deploy key on Forge", not "user wants
the key thing rotated maybe".

**The description carries everything the sentence contained** and nothing it did
not. Plain text — it is escaped before it reaches ADO, so markdown, HTML and links
in angle brackets all render as literal characters. Blank lines become paragraphs.
Include:

- what was asked for, in the speaker's own terms;
- any context they gave — the system, the error, the deadline, who mentioned it;
- **what you inferred**, marked as inference, when the transcript was ambiguous;
- what a reader would need to *verify the request*, since the person who filed it
  is not going to be in the room.

Never invent a repro step, an acceptance criterion, an estimate, a severity or a
priority. If the speaker did not say it, its absence is information.

## The work-item type

"File a ticket" is ambiguous between **Bug**, **Task**, **User Story** and
**Issue**, and the honest move is to let the default happen rather than to infer
severity or customer value from a sentence that stated neither.

- **Omit `type` entirely** unless the speaker was explicit. The configured default
  is used (it is `Task` unless the operator changed it), and the record says which
  type was used.
- **Set `type` only on clear language**: "file a *bug*", "raise a *bug* for the
  crash" → `Bug`. "Write a *story* for" → `User Story`.
- "Ticket", "item", "issue" in the everyday sense, and "task", are **not**
  explicit. Leave the type out.

An unrecognised type is not fatal — the pipeline falls back to the default and
records that it did — but do not rely on that as a guessing mechanism.

## The record you write

An HTML file in the output directory, named for the ticket
(`rotate-deploy-key.html`, not `report.html`). It exists so git history answers
"what did it file" without opening ADO. It must state:

- **the exact title and description** that were sent, verbatim, so the record and
  the ticket cannot drift;
- **the work-item type you asked for**, or that you left it to the configured
  default — say which, out loud;
- **where it goes**: the configured project, area path and iteration, which you do
  not know. Say exactly that rather than naming a guess.
- **what you inferred** from an ambiguous transcript, and what you could not
  resolve;
- for a comment: the item number and the comment text.

**Write it as pending, never as done.** You cannot know the outcome: work items
are `tracked` risk, so by default the pipeline records the intent and waits for the
operator to confirm rather than filing unattended. So: "requested — the pipeline
will file this and record the result", not "filed". Never write an item number or
an ADO link, because you do not have one and an invented one is worse than none.

The id and the clickable URL land in `outbox-receipt.json`, committed beside your
report in the same commit. Point the reader at that for the outcome; do not try to
write it yourself.

## The intent files

### `ado.workitem` — file a new item

```json
{
  "verb": "ado.workitem",
  "title": "Rotate the vault deploy key on Forge",
  "description": "Robbie flagged that the Forge deploy key predates the host move.\n\nInferred: he means the atticus-vault key, not the ingest one — he mentioned the processor.",
  "type": "Bug"
}
```

`title` is required. `description` is optional but almost always wrong to omit.
`type` is optional and should usually be absent (see above). **There are no
`project`, `area_path`, `iteration_path` or `assigned_to` fields** — they come
from configuration and anything you put there is ignored.

### `ado.comment` — comment on an existing item

```json
{
  "verb": "ado.comment",
  "id": "4711",
  "body": "Shipped in PR 12. Leaving open until the key is verified on Forge."
}
```

Both fields required. `id` must be digits.

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
