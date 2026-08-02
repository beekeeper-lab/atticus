---
name: ai-brief
description: |
  Writes the daily AI briefing — what actually changed in AI in the last 24
  hours — as a single self-contained HTML report, plus a machine-readable
  record of what it covered. Invoked by the 07:00 timer on Forge
  (`ops/atticus-brief.timer`), not by a spoken request, so the task prompt
  carries the list of items already covered on previous days. Do NOT use this
  for a one-off research question about AI (use deep-research) and do NOT use
  it to summarise a single article.
verbs: []                 # produces a document, no outbox action
risk: internal
outputs: [html]
cost: medium
---

# ai-brief

A briefing someone reads at breakfast, on a phone, every day. That frequency is
the whole design constraint: it has to be worth opening on a slow news day, and
it has to not repeat itself, or it gets ignored within a fortnight.

## The two hard requirements

**1. Do not repeat yourself.** The task prompt contains everything covered in
previous briefings, with dates. Anything on that list is not news. You may
mention it *only* as a genuine development — "the Gemini pricing change we
covered Tuesday now has official docs" — and then you must mark it as an update,
not as new.

**2. A quiet day must read as a quiet day.** Some days nothing important
happens. Say so, in one short paragraph, and stop. Do not pad, do not promote a
minor release to a headline, do not manufacture a trend from two blog posts. A
briefing that claims significance every day teaches the reader to stop trusting
it, and that is unrecoverable. **Three real items beats ten padded ones.**

## What counts

Ranked, roughly, by how much it should move the top of the page:

- **Capability or availability changes** — a model released, deprecated, repriced,
  rate-limit or context-window changes, a region or tier opening up
- **Things that break or fix your work** — API changes, SDK releases with
  migrations, sunset notices, outages with a postmortem
- **Research with a result**, not research with a press release. A paper matters
  if the finding is legible and checkable.
- **Substantive policy or legal movement** — an actual ruling, rule, or
  enforcement action, not a politician's opinion
- **Money only when it signals capability or survival** — a lab running out of
  runway matters; a Series B at an unchanged valuation does not

What does not count, however much of it there is: speculation about unreleased
models, benchmark claims from the vendor being benchmarked, "X is dead" essays,
funding rounds as scoreboard, LinkedIn-shaped thought leadership, and anything
whose only source is an account that profits from engagement.

## Sources

Pick them yourself and say which you used. Some guidance that is cheap to
follow and consistently pays:

- **Primary source over commentary, always.** A vendor's own changelog, release
  notes, model card, docs diff or status page beats any article about it. Link
  the primary source even when you found it via a summary.
- **Reddit is a discovery tool, not a source.** Use it to notice that something
  happened, then go find the real thing. Never cite a Reddit thread as evidence
  for a factual claim; cite it only when community *reaction* is itself the
  story, and say that is what you are doing.
- **Check the date on everything.** This field recycles old news constantly, and
  a two-year-old paper resurfacing on an aggregator looks identical to a new one
  in a search result. If you cannot establish that something happened in the
  window, either say so or leave it out.
- **Prefer the boring channels.** Status pages, deprecation notices, pricing
  pages and release notes are where the load-bearing changes actually appear,
  and almost nobody writes them up.

## Verification, and saying when you could not

You are unattended and nobody will fact-check this before it is read. So:

- Numbers get a primary source or an explicit hedge. No invented benchmark
  figures, no remembered prices.
- When two sources disagree, say so and say which you trust. Dates matter.
- **Mark your confidence where it is low.** "Reported by one outlet, not yet
  confirmed by the vendor" is a useful sentence. Silence dressed as certainty is
  not.
- If your search turned up nothing usable for a whole category, say that rather
  than filling it.

## Output

Two files in the output directory. Both are required.

### `index.html` — the briefing

Self-contained, phone-first. Follow the `html-artifact-output` skill. Structure
that survives being read on a phone at 7am:

- **Today in one line.** The single most important thing, or "quiet day".
- **The items**, most important first. Each one: what changed, why it matters to
  someone building with this, and a link to the primary source. Two or three
  sentences each — this is a briefing, not a summary of an article.
- **Updates** to things covered before, clearly separated and dated.
- **Worth knowing but not urgent** — a short list, one line each.
- **What I could not establish** — omit the section only if it is genuinely
  empty.
- **Sources**, linked, with dates.

Write for someone technical who builds with these tools. "OpenAI released a
model" is not a briefing; "the new model is cheaper per token than the one you
are using and the SDK change is one line" is.

### `covered.json` — what you covered

The pipeline appends this to a ledger and feeds it back to you tomorrow, which
is the entire mechanism preventing repetition. Get it right or tomorrow's
briefing repeats today's.

```json
[
  {"key": "openai-gpt5-pricing-2026-07",
   "title": "GPT-5 price cut, 40% on output tokens",
   "url": "https://openai.com/...",
   "source": "openai.com",
   "kind": "new"}
]
```

- `key` — a short, stable, lowercase slug for **the story, not the article**. Two
  outlets covering one price change share a key. Tomorrow's follow-up on the same
  story reuses the same key with `"kind": "update"`. This is the field that makes
  dedup work; a key derived from a headline will not.
- `kind` — `"new"` or `"update"`.
- One entry per item you actually wrote about, including the "worth knowing" ones.
  Nothing you merely read.
- On a quiet day with nothing to report, write `[]`. Do not omit the file.

### `podcast-script.md` — only when the task asks for it

When today's task says audio is wanted, also write a two-host script following the
**`podcast-companion`** skill's format exactly. The pipeline voices it; you hold no
TTS credential and must not try.

A briefing is a different shape from a research report, so:

- **Lead with the one-line summary, then take the items in order.** Do not restructure
  for narrative effect — someone listening while driving is tracking a list.
- **B's job is "why does that matter to us".** On a briefing the useful second voice
  asks the consequence question, not the sceptical one: does this change what we
  build, does it change what it costs, do we have to do anything.
- **Say the inflated-number warnings out loud.** If the written brief flagged a
  vendor figure as unsupported, the audio must too. A listener cannot see a caveat.
- **Four to seven minutes.** A briefing that takes longer than reading it has failed.
- On a quiet day write no script at all. Nobody wants five minutes of "not much
  happened."

## Then stop

Nothing else goes in the output directory — no scratch notes, no Markdown drafts.
`index.html`, `covered.json`, and `podcast-script.md` only when asked.
