---
name: podcast-companion
description: |
  Writes a two-host audio-overview script alongside an HTML report, so the
  report can also be listened to. Use ONLY as a companion to another skill
  that produced an HTML deliverable, and ONLY when the spoken request asked
  for audio — "and make me a podcast", "I want to listen to this", "give me
  an audio version", "read it to me on the drive". Produces
  `output/podcast-script.md`; it does NOT produce audio. The pipeline voices
  the script after the agent exits, because the agent has no credentials.
  Do NOT use it when no HTML report was written, and do NOT use it just
  because a report happens to be long.
verbs: []                 # produces a document, no outbox action
risk: internal
outputs: [html, mp3]
cost: high
---

# podcast-companion

You have written an HTML report. The request also asked to *listen* to it. Write
a spoken script; something outside this sandbox will voice it.

**You cannot make audio and must not try.** No TTS credential is reachable from
here, by design — this agent runs on text derived from ambient audio, so it holds
no keys. Your entire job is `output/podcast-script.md`. If you find yourself
reaching for an API, stop: you are doing the wrong half of the task.

## The format is a contract

A parser reads this file. Deviating from the format means no audio gets made.

```markdown
# <episode title>

**A:** First host's opening line.
**B:** Second host's reply.
**A:** And so on, alternating.
```

Rules the parser enforces:

- Exactly two speakers, labelled `**A:**` and `**B:**`, at the start of a line.
- One turn per line. No blank lines inside a turn, no line continuations.
- The `# ` heading on line 1 is the episode title. Everything that is not a
  heading or a turn is ignored, so notes to yourself are harmless but pointless.
- **Plain prose only inside a turn.** No markdown, no lists, no code, no URLs,
  no parentheticals like `(laughs)`. Every character gets spoken aloud, so `**`
  or `https://…` is read out as literal noise.
- Alternate A and B. Two turns from the same speaker in a row is not an error
  but it sounds broken.

## Writing for the ear

This is the part that is actually hard, and the reason this is a skill rather
than a template.

**Spell out anything the eye handles silently.** "$1.71" is "a dollar
seventy-one". "portVersion = 20" is "port version twenty". "~30 min" is "about
thirty minutes". "ADR-005" is "A-D-R five". A number with units, a hex value or
an identifier read out raw is the single most common way these scripts sound
wrong.

**Do not read the document.** A written report front-loads its conclusion and
lets the eye skip. Audio cannot skip. So open with why the listener should care,
put the recommendation early, then walk the reasoning. Sections, tables and
footnotes have no spoken equivalent — turn a table into "the cheapest was X at
this price, the fastest was Y" and drop the rest.

**Two hosts, two jobs, not two voices saying the same thing.** A explains and
advocates; B is the person who has not read it — asks the obvious question,
pushes on the weak step, asks "so what would change your mind?". If B's turns
are all "Right" and "Interesting", collapse to one host and rewrite.

**Keep the report's honesty.** If the report flagged something unverified, low
confidence, or contradicted by a source, say so out loud. An audio summary that
sands off the caveats is worse than no audio, because it sounds authoritative
and the listener is not looking at your hedges.

**Length.** Aim for six to ten minutes — roughly 900 to 1,500 words total.
Under 400 words there is no reason not to just read the report. Over about
2,500 words you are re-narrating rather than summarising, and it costs
proportionally more.

**End on the recommendation, not a farewell.** "So: X, unless Y." No "thanks for
listening", no invented sponsor, no next-episode tease.

## Then stop

Write the file and finish your normal report work. Do not mention the script in
the HTML — the pipeline injects the player itself, and a hand-written link would
point at a file that does not exist yet.
