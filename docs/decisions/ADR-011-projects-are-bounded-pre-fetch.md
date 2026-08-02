# ADR-011 — A project brief is bounded pre-fetch, and the pipeline resolves the referent

**Status:** Accepted
**Date:** 2026-08-02
**Issues:** [#84](https://github.com/beekeeper-lab/atticus/issues/84), [#88](https://github.com/beekeeper-lab/atticus/issues/88), and a partial answer to [#63](https://github.com/beekeeper-lab/atticus/issues/63)
**Related:** ADR-006 (contact resolution — the same split), `processor/projects.py`, `processor/recordings.py`

## Context

Two capabilities landed together because they turn out to be one idea.

**Continuity (#84).** Every recording was an island: one transcript, a scratch
workspace, no memory. "Continue the consulting research", "add this to the DDI
project" and "revise that report" were unsayable — not because the words were
hard to parse, but because there was nothing for them to refer to.

**Lifecycle (#82).** "Cancel that", "what happened to this morning's request".
Same shape: a phrase that needs a referent the agent cannot look up.

The agent has no reads, by design (#63). So both needed the same answer.

## Decision 1 — the pipeline resolves referents, not the agent

This is ADR-006's split applied twice more. A lookup the AGENT cannot do is
perfectly fine for a HANDLER, which runs pipeline-side where the data already
is. The agent writes down the words it heard; the pipeline resolves them after
the agent exits.

That pattern is now used four times, and the rules are identical every time:

| what | resolves to | refuses when |
|---|---|---|
| `contacts.py` (ADR-006) | a person's address | ambiguous or low confidence |
| `github.close` | an open issue number | no match, or several |
| `recordings.resolve` (#82) | one recent recording | no match, or several |
| `projects.resolve_from_text` (#84) | one named project | two named at once |

**Refuse rather than guess** is the invariant. Nobody is present to
disambiguate, and every one of these has a bad failure available to it:
messaging the wrong person, closing the wrong issue, cancelling work that was
wanted, filing work in a project where it will never be found.

Two bounds fall out of the same reasoning. Recordings are searchable for seven
days only — "that thing" means something recent, and a wider window makes
ambiguity certain. And a recording can never resolve to **itself**: "cancel
that" is spoken into a run the pipeline is executing, and without the exclusion
the cancel kills the run performing it, which then never records the
cancellation.

## Decision 2 — a project brief is #63's pre-fetch option, in its safest form

#63 asks how a sandboxed agent reads external data and names two candidates: a
credential-holding loopback broker (powerful, the largest new prompt-injection
surface this project could add) and pipeline-side pre-fetch (safe, but cannot
answer an unanticipated question).

**A project brief is pre-fetch with every dial turned down:**

- **operator-authored** — `projects/<slug>/brief.md`, written by hand, not
  fetched from anywhere;
- **size-capped** — `ATTICUS_PROJECT_CONTEXT_CHARS`, default 2000;
- **pipeline-assembled** — the agent cannot ask for it, vary it, or query for
  more;
- **scoped by the operator's own words** — only a project the transcript names,
  and two names at once refuses rather than picking;
- **fenced as reference material**, exactly like the transcript.

That last point is not ceremony. The brief is the operator's prose, but the
artifact titles beside it were written by earlier agent runs, and one of those
runs may have ingested a hostile web page. Treating the whole block as data
costs nothing and removes the question. The block is also placed **before** the
preamble, so the output contract and the act-only-on-the-first-request rule
remain the last framing the model reads before the transcript.

**This does not settle #63.** Answering an unanticipated question — "summarise
my inbox" — still requires a broker or a per-question pre-fetch, and the
broker's objection is unchanged. What it does mean is that the safe half is no
longer hypothetical: it is running, and it demonstrably works. The agent's first
project deliverable used a revenue target and a positioning statement that
appeared only in the brief.

## Decision 3 — versions belong to artifacts, not recordings

A recording is **immutable**: it is a thing that was said at a time. So "revise
that report" cannot produce a second version *of a recording*. It produces a
second version of a project **artifact**:

    projects/<slug>/artifacts/<name>/v1.html, v2.html, …

The recording's own copy in `processed/` is never touched — it stays the record
of what that run produced — and the project is where history accumulates. This
is why #88's versioning half lives in `projects.py` rather than in the
pipeline's publish step, and why versioning was not built before projects
existed: it had nothing to attach to.

## Consequences

- `projects/` is a new top-level directory in the vault, owned by the processor.
- Creating a project is a manual act (`brief.md` + `index.json`), deliberately:
  there is no `project.create` verb yet, because nothing has yet shown that
  speaking a new project into existence is a thing the operator wants.
- The scope is far smaller than the review proposed — no tasks, contacts,
  preferences or context packs. Every capability here that has worked started as
  the narrowest version that made one sentence sayable.
- Two new terminal statuses, `cancelled` and `superseded`, rank **above**
  published in the conflict-resolution table: if two hosts disagree because one
  cancelled a record, the cancellation must win.
