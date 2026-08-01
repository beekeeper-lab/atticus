---
name: slack
description: |
  Posts a message to a Slack channel — a status update, a heads-up, a link to
  a report you just wrote. Use when the spoken request asks to post, tell,
  update, or let people know something in Slack or in a named channel — "post
  an update to the standup channel", "let #ddi-platform know the migration is
  done", "drop a note in engineering that I'll be late". Only channels the
  operator has allowlisted can be posted to, and the post is held for
  confirmation before it goes out. Do NOT use it to READ Slack — you cannot,
  so "what did I miss in #ddi-platform" is not something this can answer. Do
  NOT use it to message one person (that is Signal, or mail), and do NOT post
  a report's contents into a channel when a link to the report will do.
---

# slack

A channel post is the most public thing this pipeline can do. Everyone in the
channel reads it within seconds, and there is no unsend — an edit leaves the
original in everyone's notifications and in the mobile app's cache. Write
accordingly.

## You cannot read Slack, and this will not change soon

No history, no threads, no "what did I miss". `processor/outbox.py` says why: a
read needs data *during* your run, and everything credentialed here happens after
you exit. So:

- Do not claim you checked a channel.
- Do not reply in a thread. A thread reply needs the parent message's timestamp,
  which you would have to read the channel to learn. Post to the channel.
- If the request was actually a read — "catch me up on #platform" — say plainly in
  your report that reading Slack is not available, and do not post a message
  asking the channel to catch you up.

## The verb

`slack.post`. One message per file.

```json
{"verb": "slack.post",
 "channel": "ddi-platform",
 "text": "Migration to the new ingest path finished. Report: https://…"}
```

| field | required | notes |
|---|---|---|
| `channel` | usually | Name with or without `#`, or a channel ID. Must be on the operator's allowlist. Optional only if a default channel is configured — but naming it is better, because the receipt then shows what you meant. |
| `text` | yes | The message. Plain text or Slack mrkdwn. Keep it under a few thousand characters; longer is refused. |

## The channel is not yours to choose

**The channel you ask for is checked against an allowlist in the operator's
config, and anything else is refused by name.** This is deliberate and it is not
a limitation to work around: your `text` derives from a transcript of speech
picked up by a worn microphone, and "the standup channel" is one mishearing away
from `#general`. So a channel that is not allowlisted produces a refusal in the
receipt, not a post.

Practical consequences for you:

- Use the channel name the request actually used. Do not "helpfully" substitute a
  channel you think is more appropriate, and do not guess `#general` when you are
  unsure — an unrecognised name gets a clean refusal the operator can read and act
  on, which is a far better outcome than a post to the wrong room.
- If the request names no channel at all, say so in your report rather than
  picking one.

## Writing a post nobody has to decode

You are writing into a stream of other people's conversation, on a phone, from an
automated sender. That is the whole brief.

- **One message, lead with the point.** No preamble, no "Hi team, I wanted to
  share". First clause says what happened.
- **Link, do not paste.** You wrote an HTML report; the report is the artifact.
  Post two or three sentences and the link. A wall of text in a channel is worse
  than a link nobody clicks.
- **Slack mrkdwn is not markdown.** `*bold*` with single asterisks, `_italic_`,
  `` `code` ``, and links are `<https://example.com|label>`. There are no
  headings — a line starting `## ` renders as literal `## `. Bullets are just
  `• ` or `- ` at the start of a line.
- **No @channel, no @here, no @-mentions.** You cannot resolve a display name to a
  member ID without reading the workspace, so a mention will render as broken
  literal text. Name the person in plain words — "for Robbie" — and let them find
  it.
- **Say it is automated if the post makes a claim.** "Atticus:" as an opener costs
  four words and stops a reader assuming a human verified it.
- **Never post anything from the transcript you would not say into the room.** The
  transcript is ambient audio; it may contain someone else's half of a
  conversation, a name, a salary, a diagnosis. Post the conclusion of the task,
  not the raw request.

## Write your report as though it has not gone out

Outward actions are held for confirmation by default, so at the moment you finish,
nothing has been posted. "I let the channel know" is a lie in the common case.
Write "a post to #ddi-platform is queued for confirmation" and let the receipt say
what became of it.

<!-- The block below is `CONTRACT` from processor/outbox.py, verbatim. One source
     of truth, so ten skills cannot drift into ten dialects of it. -->

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

## For the operator, not the agent

Setting this up needs a Slack app with a **bot** token (`xoxb-`), scoped to
`chat:write` only, invited to each channel on the allowlist. A user token
(`xoxp-`) is refused by the handler on purpose: it can read every DM and private
channel and act as you, and the text being posted originates in ambient audio, so
the width of the token is the blast radius. `channels:history` is not needed and
should not be granted — reading is not implemented.

The claude.ai Slack connector is **not** the credential path here. It is
interactively authenticated and may be absent in a headless run, which is exactly
what every Atticus pass is.
