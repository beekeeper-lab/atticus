# ADR-007 — Todos live in the vault, not Microsoft To Do

**Status:** Accepted
**Date:** 2026-08-01
**Issue:** [#51](https://github.com/beekeeper-lab/atticus/issues/51)
**Related:** `processor/todos.py`, `processor/handlers/todo.py`, the vault repo's `site/todos.py` / `site/build.py` / `site/api.py`

## Context

"Atticus, add picking up the prescription to my list" needs a list. Issue #51
opened as "pick a todo app" and narrowed to two candidates:

- **Microsoft To Do**, which already existed on the operator's account with a
  first-class phone app, Graph API, and a token on this host. The `todo.add`
  handler was first built against it, and its own docstring argued for it:
  *"a list you cannot see on your phone is worse than paper."*
- **A ledger in the vault**, rendered by the vault site — no credential, no new
  app, git history for free.

Evidence gathered on 2026-08-01 changed the weights. `m365 tasks --all`
returned **nothing**: the operator's To Do is empty, so integrating with it
would not be meeting them where they are — it would be asking them to adopt an
app and build a habit, which was the strongest argument for it ("tasks created
elsewhere show up too") gone. Meanwhile the phone objection had already
dissolved in practice: the vault browser was used *from the phone* that same
day to queue an audio episode over Tailscale.

The operator decided: **the vault.**

## Decision

The list is an append-only JSONL ledger, `.state/todo.jsonl`, exactly the
reminders pattern:

- **`processor/todos.py`** (this repo) owns the format: first event carries the
  item, later events carry only what changed, fold newest-last for current
  state. The id is deterministic in *(recording stem, list, title)* so
  `pipeline.py --retry` cannot double an item, while the same words in a fresh
  recording legitimately create a fresh one. A small CLI (`list`/`add`/`done`/
  `drop`) is the terminal view.
- **`todo.add`** keeps its verb, request shape (`title`, `note`, `due`,
  `list`), INTERNAL risk class, and receipt format — only the backend changed.
  The Graph implementation is in git history at the same path.
- **The vault repo renders and checks off**: `site/build.py` builds
  `todo.html` from the ledger, and `site/api.py` gains `POST /todo`
  (`done`/`reopen`/`drop`, existing ids only — items are created by the
  pipeline, never by the browser). The ledger folding is deliberately
  re-implemented vault-side (~40 lines, same choice as `audioreq.py`): the two
  repos deploy independently, and the event format is the cross-repo contract,
  pinned by tests on both sides.

Three writers, no coordination — the handler, the API, a human with an editor —
which is what append-only buys. `.gitattributes` gives the ledger `merge=union`
like every other ledger.

## Why not To Do, recorded properly

1. **The phone requirement was already met** by the vault browser over
   Tailscale. Adopting To Do would add a second app for a view that existed.
2. **Capture is the pin.** The classic weakness of a self-hosted list — adding
   to it is slow — does not apply when adding is speaking. To Do would be a
   second input path and a standing sync question between the two lists.
3. **No scope widening.** The Graph route needed `Tasks.ReadWrite` consent on a
   token whose advertised contract is read-only ("never send, reply, delete, or
   modify" — a sentence other sessions rely on), or a separately registered
   client. The vault route needs no credential at all, which also means one
   less thing inside any future sandbox-reads decision (#63).

**The honest cost:** no offline access. If Forge or Tailscale is down, the list
is unreachable; To Do works on a plane. The operator accepted this knowingly.

## Consequences

- `ATTICUS_TODO_ACCOUNT`, `ATTICUS_TODO_TOKEN_FILE`, `ATTICUS_TODO_LIST` and
  `ATTICUS_TODO_TIMEOUT` are removed; the feature has **zero configuration**.
- `list` names are now plain grouping labels rather than Graph lists validated
  against existing ones. The refusal that protected against a misheard
  "Groseries" is unnecessary: on one flat page a stray label is visible two
  lines from everything else, not a separate list nobody opens.
- "When did I finish that" is `git log` on the ledger.
- If Graph consent is ever wanted after all (trigger: the operator starts using
  To Do from Outlook/phone and wants one list), revive the handler from history
  at `processor/handlers/todo.py` — the verb contract still fits.
