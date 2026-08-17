# ADR-012 — Radar is a lead source for the briefing, consumed through its export contract

**Status:** Accepted
**Date:** 2026-08-17
**Related:** ADR-011 (bounded pre-fetch — the same shape), [#63](https://github.com/beekeeper-lab/atticus/issues/63) (how a sandboxed agent reads external data), `processor/radar.py`, `skills/ai-brief/SKILL.md`

## Context

Radar is a separate pipeline in `~/workspace/radar` on Forge. It collects
practitioner signals from 14 sources twice a day — Hacker News and Reddit,
GitHub issues across 23 repos, 79 ATS boards, YouTube and podcast transcripts,
practitioner newsletters, Hugging Face and arXiv, the Federal Register and
CourtListener dockets, EU AI Act milestones, vendor changelogs, and the
operator's own typed notes — and exposes them through a versioned read contract
(`uv run radar export`, `export_version: 1`).

Its own purpose is finding material for classes and blog posts. But it collects
exactly the channels the `ai-brief` skill already tells the briefing to prefer
and which are tedious to search live: vendor changelogs, dockets, and bug
threads where a release visibly broke someone's work.

The daily briefing is working — it surfaces things the operator would not
otherwise have seen. So the question was how to add a source **without changing
the briefing**, which is the constraint that shaped everything below.

## Decision 1 — leads into the prompt, not a source of record

Radar signals enter the task as a fenced block of **leads**. A signal means
something is being discussed; it is never evidence that a thing happened, and
never a citation. The briefing chases a lead to a primary source and cites that,
or leaves it out — the rule the skill already gives for Reddit, applied to
everything Radar carries.

Nothing else about the briefing moves: the window is still 24 hours, the bar for
what counts is unchanged, a quiet day is still a quiet day, and the block is
explicitly not a quota. Most days most of it will not clear the bar.

## Decision 2 — pipeline-side pre-fetch, again

This is ADR-011's answer to #63 a second time. The agent runs in a mount
namespace with no vault, no credentials and nothing of this host on disk, so it
cannot run `radar export` and must not be given a way to. The driver runs the
export, bounds it, fences it, and puts it in the prompt. No query is possible;
no credential goes near the agent.

Bounds, all in `config.py` and all defaulted: a 3-day lead window, a per-family
cap that is also an **allowlist**, a total cap, a body-snippet cap, and a hard
character cap on the rendered block. A family Radar adds later is skipped and
logged rather than silently included.

The window is 3 days and not 1 because of what the store actually contains.
Measured on 2026-08-17 against a healthy Radar: a 1-day window is forum and jobs
only; `code` and `media` appear at 2; `vendor` and `research` do not appear until
3. A vendor changelog publishes when it publishes, and Radar collects twice a
day, so a 24-hour view of exactly the channels worth having is empty. Every lead
carries its own publication date and the briefing's 24-hour rule is untouched —
a wider **lead** window is not a wider briefing.

The per-family weighting is where the "additional content" intent lives: vendor,
regulatory, research and code lead, because that is what Radar has and the
briefing's own searching does not. Forum volume — which the briefing already
covers — is capped low, and Radar's Reddit is deduplicated against the covered
ledger by permalink and by Reddit's own `t3_`/`t1_` id, so one thread found by
two pipelines is presented once.

## Decision 3 — read-only, and the store's WAL mode is the one wrinkle

We run `radar export` and nothing else. Never a collector, never a prune —
`radar-collect.timer` and `radar-prune.timer` own those and prune takes a write
lock to VACUUM at 04:50/06:20/18:20. An export near those times is slow, not
broken, which is why the timeout is 180s and a timeout costs the briefing its
leads and nothing more.

The wrinkle: Radar's store lives at `~/.local/share/radar/signals.db` in **WAL
mode**, so even a pure reader has to create the `-wal`/`-shm` sidecars. Under the
brief unit's `ProtectHome=read-only` the export dies with "unable to open
database file" before reading a row. Verified with `systemd-run` on 2026-08-17:
the same command fails without the grant and succeeds with it. So
`atticus-brief.service` carries `-%h/.local/share/radar` on `ReadWritePaths`.
Radar's repo stays read-only. That grant is narrower than it looks — it is the
raw signal cache, not the versioned ledger, which lives in the repo.

## Decision 4 — every signal is hostile text

Titles and bodies were written by strangers on forums, in job postings, and in
court filings, and they reach a prompt that drives an autonomous agent. The block
is fenced as UNTRUSTED DATA with the fence markers defused first, exactly as
`execute.py` treats a transcript, and it says in the prompt that instruction-shaped
text is content to report rather than a command.

It is also placed **before** the already-covered list rather than after it. The
do-not-repeat-yourself rule is the requirement that has to survive everything
else in the prompt, so it keeps the final position; the largest block of
stranger-written text does not get it.

## Decision 5 — Radar can never break the briefing

Radar absent, `uv` off PATH, a timeout, malformed JSON, an unrecognised
`export_version` — every one of them degrades to "no Radar block today", logged
loudly, and the briefing is written. An unknown `export_version` is refused
rather than parsed optimistically, per Radar's own contract, because a shape
change under text we treat as hostile is not something to guess at.

Loudly, but not as an alarm: the reason is logged and carried in `run()`'s return
value. A lead source that quietly stopped working for a month is the failure this
project cares about most, and a push notification for it would train the operator
to ignore the channel that carries real ones.

## Consequences

- The briefing gains the boring channels — changelogs, dockets, bug threads —
  without a new credential, a new network path for the agent, or a change to what
  the briefing is.
- Radar's 60-day retention is Radar's business. Anything the briefing wants to
  keep is already in its own covered ledger.
- If Radar's contract reaches version 2, the briefing loses its leads until
  `SUPPORTED_VERSIONS` in `processor/radar.py` is updated deliberately. That is
  the intended trade.
