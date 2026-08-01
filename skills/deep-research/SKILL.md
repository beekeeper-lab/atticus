---
name: deep-research
description: |
  Researches a topic from a spoken request and produces a single
  self-contained HTML report. Use when the transcript asks to research,
  investigate, look into, explain, compare, or evaluate something — e.g.
  "research what an agentic harness is", "compare the best local models for
  my Framework laptop", "look into alternatives to X and tell me which is
  better". Searches the web, reads primary sources, resolves disagreements
  between them, and writes a findings document with a clear recommendation.
  Do NOT use for shopping or price comparison (use price-scout), for turning
  an idea into a spec (use idea-to-spec), or for anything that just needs a
  short factual answer.
---

# deep-research

Spoken request in, HTML report out. The person dictated a question and walked
away — they will read the result later, probably on a phone. There is nobody to
ask a follow-up.

## What that implies

**Answer the question that was meant, not the one that was said.** The input is
a transcript. "Agentic harness" may arrive as "agent tick harness." Infer from
context. If the topic is genuinely ambiguous, research the most plausible
reading and note the alternative in a short caveat — do not stall.

**Take a position.** A survey with no recommendation makes the reader do the
work they delegated. Say which option you would pick and why, then show what
would change your mind.

**Say what you could not establish.** An honest gap is useful; a confident
guess dressed as a finding is worse than nothing.

## Method

1. **Frame it.** Write down the actual question, plus the two or three
   sub-questions that would settle it. This shapes the searching.
2. **Search broadly, then narrow.** Several angles — the term itself, the
   problem it solves, critiques of it, what practitioners say. Vendor pages
   state capabilities; they do not evaluate them.
3. **Read primary sources.** Fetch the docs, spec, or repo rather than relying
   on a summary of a summary.
4. **Reconcile conflicts explicitly.** When sources disagree, say so and say
   which you trust and why. Dates matter — this field moves.
5. **Write.** Follow the `html-artifact-output` skill for structure and style.
   Use the research-report template.

## Write early, then improve it

**You are running under a hard spend ceiling and you will not be warned before it
stops you.** When it fires, the run ends instantly and anything not yet written to
a file is lost. Measured 2026-08-01: a run spent 60 turns and produced no file at
all, because it researched exhaustively and intended to write at the end.

So:

1. Do enough searching to answer the question **at all** — usually three or four
   sources.
2. **Write the complete HTML file now.** Rough, but whole: every section present,
   the recommendation stated, sources listed.
3. Then improve it in place — more sources, fill the gaps, sharpen the
   recommendation. Rewrite the file after each meaningful addition.

A finished-but-shallow report beats a killed run every time. If the ceiling stops
you at step 3, the reader still has something useful and it says what it does not
cover.

**Scope a sprawling request rather than answering all of it.** "Which models, which
should I switch to, which should I download, and is my hardware enough" is four
questions. Answer the one that subsumes the others, note explicitly which parts you
did not cover, and stop. Trying to cover everything is what exhausts the ceiling
before anything is written.

## Output

One self-contained HTML file in the output directory. Name it for the topic —
`agentic-harness.html`, not `report.html`.

Structure that works for reading on a phone:

- **Answer first.** Two or three sentences. If the reader stops here they
  should still have gotten what they asked for.
- **Key findings** — scannable, most important first.
- **The detail** — organised by sub-question, not by source.
- **Recommendation** with reasoning, and what would change it.
- **Open questions** — what you could not resolve.
- **Sources** — linked, with dates where they matter.

Length follows the question. A definition needs a page. "Compare local models
for my hardware" needs a table and real numbers.

## If the request also asked to listen to it

Phrases like "and make me a podcast", "I want to listen to this on the drive",
or "give me an audio version" mean the report is wanted *and* a spoken version.
Write the report first, then follow the **`podcast-companion`** skill to add
`output/podcast-script.md`. Do not attempt audio yourself — you hold no TTS
credential and the pipeline voices the script after you exit.

Say nothing about audio in the HTML. The pipeline injects the player.

## Notes

- Include concrete specifics: version numbers, prices, benchmark figures, dates.
  Vague research is unactionable research.
- When the request mentions the user's own hardware or setup, tailor to it
  rather than answering generically.
- No filler. No "in today's fast-paced world." Open with the answer.
