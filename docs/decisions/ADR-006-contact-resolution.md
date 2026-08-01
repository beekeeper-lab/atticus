# ADR-006 — Contact resolution is pipeline-side infrastructure, not a skill

**Status:** Accepted
**Date:** 2026-08-01
**Issue:** [#43](https://github.com/beekeeper-lab/atticus/issues/43)
**Related:** [#42](https://github.com/beekeeper-lab/atticus/issues/42) (the outbox), `processor/outbox.py`

## Context

"Atticus, tell Robbie I'll be late" is unactionable until "Robbie" becomes an
addressable handle. Every messaging skill depends on that, and a wrong resolution
sends a private message to the wrong person — the highest-consequence failure on
the roadmap.

Issue #43 frames this as a lookup the agent performs. It cannot be, and the
reason is already written down in `processor/outbox.py`:

> **Reads are NOT solved by this file, deliberately.** […] The options are a
> credential-holding loopback broker the agent can query — powerful, and a large
> new prompt-injection surface — or pipeline-side pre-fetch, which is safe but
> cannot answer an arbitrary question.

A resolver is a read. The agent runs under `bwrap` with its own `HOME`: the
`claude` binary and two named skill directories are bound in and nothing else —
no `~/.secrets/m365*.json`, so no working `m365`, so nothing for a skill to call.
Writing `skills/contacts/` would produce a skill that fails in the sandbox and
works only in the developer's shell, which is worse than not having it.

## Decision

**Build the resolver as pipeline-side infrastructure that outbox handlers call,
in `processor/contacts.py`. No skill, no broker, no new architecture.**

```
agent, sandboxed        writes  {"verb": "signal.send", "to": "Robbie", …}
pipeline, credentialed  contacts.resolve("Robbie", channel="signal")
                          → one confident match  → send
                          → zero, or several     → refuse, and say why in the receipt
```

This works today. Three things make it the right split rather than a workaround:

1. **The intent boundary already sits in exactly the right place.** The agent
   states *who* it means in the words the speaker used. It does not need to *see*
   the address book to do that — resolution is the credentialed side's job, like
   actually sending the message is.
2. **It is a pre-fetch, not a broker.** The lookup is one narrow question
   ("which person is this name?") answered *after* the agent exits, so it adds no
   prompt-injection surface: nothing the resolver reads is ever fed back into the
   agent's context. A broker able to answer "what's on my calendar" mid-run is a
   materially different risk and stays deferred with #42's reasoning intact.
3. **The refusal lands where a human can see it.** Ambiguity has to be handled by
   *something*, and the outbox already has the vocabulary for it: a held or
   refused request with a reason, recorded in `outbox-receipt.json` and committed
   to the vault. An agent that resolved names itself would have to invent that.

The cost is honest and worth naming: the agent cannot *ask* "which Robbie?" while
it is running, so it cannot phrase its report around the answer. It writes the
report as though the send is pending — which `outbox.CONTRACT` already requires
of it for every outward action.

## Design

### Never a bare handle

`resolve(name, channel=None, cfg=None) -> list[Match]`, ranked best-first. A
`Match` carries `name`, `handle`, `channel`, `source`, `confidence`, `tier`,
`matched_on`, `handles`, `rank`, `last_interaction`, `company`, `also_seen`. The
caller decides. `unambiguous(matches, cfg) -> (Match | None, reason)` is the
decision a sender wants, and it refuses by default.

An empty list is a normal answer, not an error, and an unavailable source
degrades the result instead of raising: a resolver that threw would take a whole
outbox pass down with it.

### Ambiguity is refused, never guessed

Two Robbies is the normal case. `unambiguous()` yields a match only when all
three hold:

* top confidence ≥ `min_confidence` (0.75), **and**
* it is clear of the runner-up by ≥ `ambiguity_margin` (0.15), **and**
* it actually carries a handle for the requested channel.

Otherwise it returns `None` with a reason a receipt can quote — "2 candidates
within 0.15: Robbie Page, Robbie Chen". Nobody is present to disambiguate, so
refusing is the correct outcome; guessing is the failure this whole module exists
to prevent.

A person we know but cannot reach on the asked-for channel is returned
**unaddressable** (`handle=""`, with a note) rather than dropped. "We know who
Robbie is but cannot reach him on Signal" and "there is no Robbie" need different
fixes, and a caller that cannot tell them apart will report the wrong one.

### Tiers are disjoint bands, so phonetics can never beat spelling

The input is a transcript, so the name itself may be wrong: this project has
logged "Atticus" arriving as "Advocates", "Abacus" and "Artemis", and a mangled
*person's* name has no wake-word adjudicator behind it. So phonetic matching is
required — and it is scored in its own band:

| Tier | Band | Means |
|------|------|-------|
| `exact` | 0.75 – 0.99 | the spoken name **is** the person's name, or one whole token of it, or their email localpart |
| `partial` | 0.45 – 0.65 | a prefix ("Rob" → "Robbie"), or near-spelling (ratio ≥ 0.82) |
| `phonetic` | 0.20 – 0.39 | metaphone key matches, spelling does not ("Robby" → "Robbie", "Catherine" → "Kathryn") |

Quality signals — name similarity, source priority, the source's own relevance
rank, recency — only move a candidate *within* its band. So a phonetic hit cannot
outrank an exact match however much the best source likes it. And because
`min_confidence` **equals the floor of the exact band**, nothing below exact tier
is ever auto-chosen for a send. That is the line between "confident enough to
draft" and "confident enough to deliver", expressed as a number rather than a
convention.

Metaphone is implemented locally (~90 lines, stdlib only) rather than adding a
dependency for one algorithm. It is a sound key, not a full Double Metaphone; the
banding is what makes that acceptable — an imperfect key can only add or miss a
low-confidence candidate the caller must already disambiguate.

### Sources, ordered by value and pluggable

| Source | Status | Why |
|--------|--------|-----|
| `m365:people` | on | Graph `/me/people` — relevance-ranked by real interaction, better than an address book, and the only ranked source we have |
| `m365:contacts` | on | the Outlook address book; unranked, so scored neutrally |
| `git:log` | off until repos are configured | `git log --format='%aN <%aE>'` is a genuinely good source for colleagues; ranks by commit count and reports a real last-interaction date |
| Signal (#47), Slack (#48), ADO/GitHub org | future | each is `register_source(name, fn)` plus a name in `ATTICUS_CONTACTS_SOURCES` |

m365 only, to start: it is the one source that works today, it covers contacts
and interaction history, and it is the cheapest way to find out whether the
ranking model is any good before adding more. Note that `m365 contacts` has no
`--json` (unlike its mail/cal commands), so the adapter parses its text output
field-by-content rather than positionally — an empty middle field collapses the
separator, and a positional split reads the company as an email address.

No source available today produces a phone number — `m365 contacts` prints only
name, emails and company — so **Signal sends will refuse on "no handle" until #47
lands a phone source.** That is the correct behaviour, not a gap to paper over:
the resolver knows who was meant and says exactly why it cannot reach them.

### Two things only live data showed

Running the CLI against the real tenants immediately produced ambiguity the
sources had invented rather than reality:

* **Graph returns `displayName` == the address** for directory entries with no
  display name, which arrived as a *third* candidate for the same human beside
  their two real rows. A name is now derived from the localpart so the row
  dedupes against the one it duplicates.
* **One person, two mailboxes** (the same human in both tenants) is a tie, and it
  refuses — correctly, because "which address" is a real question — but the reason
  now says "same name, different addresses" rather than listing a name twice.
  When *nobody* in the tie has a handle for the channel, the reason names the
  missing handle instead, because the missing source is the actionable fact.

Both are regression-tested with the shapes the CLI actually printed.

### The cache exists to make bad resolutions diagnosable

`~/.cache/atticus/contacts.json`, keyed by `name|channel`, TTL 168h. Each entry
stores the query, the full ranked list, per-source status **and a `winner` record
naming the source and tier that produced the top match**. When a message reaches
the wrong person, the first question is which source said so, and reconstructing
that after the fact from a ranked list is guesswork. `python -m contacts resolve
"Robbie" --channel signal --json` prints the same structure live.

## Settings

All read with `getattr(cfg, "contacts_…", default)`, so the module runs correctly
against a `Config` that knows nothing about them — which is the state today.
**Until `processor/config.py` maps these env vars onto `contacts_*` attributes,
every value below is a compile-time default and setting the environment variable
does nothing.** That wiring is a one-line-per-setting change in a file this ADR
does not own.

| Setting | Default | Meaning |
|---------|---------|---------|
| `ATTICUS_CONTACTS_SOURCES` | `m365:people,m365:contacts` | ordered; earlier sources score higher |
| `ATTICUS_CONTACTS_M365_ACCOUNTS` | `default,organservices` | both tenants are consulted; a colleague may exist in one |
| `ATTICUS_CONTACTS_M365_LIMIT` | `25` | `-n` per query |
| `ATTICUS_CONTACTS_TIMEOUT` | `20` | seconds per source call |
| `ATTICUS_CONTACTS_CACHE_TTL_HOURS` | `168` | `0` disables the cache entirely |
| `ATTICUS_CONTACTS_MIN_CONFIDENCE` | `0.75` | floor for `unambiguous()`; equals the bottom of the exact band |
| `ATTICUS_CONTACTS_AMBIGUITY_MARGIN` | `0.15` | how far clear of the runner-up the winner must be |
| `ATTICUS_CONTACTS_PHONETIC` | `on` | `off` disables metaphone matching |
| `ATTICUS_CONTACTS_MAX_RESULTS` | `8` | length cap on the returned list |
| `ATTICUS_CONTACTS_GIT_REPOS` | *(empty)* | comma/colon-separated repo paths; empty disables `git:log` |
| `ATTICUS_CONTACTS_GIT_MAX_COMMITS` | `2000` | history depth scanned per repo |
| `ATTICUS_CONTACTS_CACHE_PATH` | *(unset)* | override the cache location; tests use it |

## Options considered

**A skill the agent calls (the issue's framing).** Rejected: the sandbox binds no
`~/.secrets`, so `m365` cannot run there. It would work in a developer shell and
fail in production, which is the worst of the three outcomes.

**A credential-holding loopback broker.** Rejected for now, on `outbox.py`'s
stated grounds: it is a large new prompt-injection surface, and it is not needed
for this. Revisit only if a skill genuinely needs *arbitrary* reads mid-run — and
then as its own ADR, not as a side effect of contact lookup.

**Pre-resolving every name in the transcript before the agent runs.** Rejected:
it would hand the agent a list of real people and addresses derived from ambient
audio, which widens exposure for no benefit — the agent never needs the handle,
only the name it already heard.

**Returning a single best guess with a confidence attached.** Rejected. It is the
same API as returning a string, because callers use `result.handle` and ignore
the number. The list is the point: two candidates must be structurally different
from one.

## Consequences

* Every messaging handler gains a resolution step it must handle three ways —
  one match, none, several — and the receipt must say which happened.
* Signal and Slack sends will refuse on "no handle" until those sources exist
  (#47, #48). Email sends work now.
* A misheard name reaching the right person requires an exact-tier hit; a
  phonetic-only hit will always refuse at the defaults. If that proves too strict
  in practice, the knob to move is `min_confidence`, and moving it below 0.75
  deliberately admits non-exact matches — do not do it without re-reading the
  band table above.
* `docs/configuration.md` needs the twelve settings above; that file is generated
  from the code and is owned elsewhere, so it is not updated here.
