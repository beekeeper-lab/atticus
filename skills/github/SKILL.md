---
name: github
description: |
  Files a GitHub issue, comments on an existing issue by number, or CLOSES an
  open issue, from a spoken request. Use when the transcript asks to open,
  file, raise, log or create an issue or ticket, to write something down as a
  bug or a follow-up, to add a comment or a note to a numbered issue, or to
  close, resolve, finish or "mark done" an issue — "open an issue on atticus
  about the budget guard", "file a bug that the timer stops firing", "add a
  comment to issue forty-two saying it also happens on Forge", "close the
  issue about the Slack integration", "mark issue twelve as not planned".
  Closing accepts either a number or the words from the issue's title; the
  pipeline resolves those words and refuses if they match no open issue or
  more than one. You do NOT perform any of it: you write an intent file and
  the pipeline runs `gh` after you exit. Do NOT use this skill to ANSWER a
  question about GitHub — the state of the open PRs, whether an issue is
  still open, what changed in a repo, who is assigned — nothing here can read
  from GitHub back to you, by design. Do NOT use it to push, branch, merge,
  review, reopen or delete anything, to edit an issue's title or body, or to
  touch a repository that is not on the operator's configured allowlist. For
  research that happens to mention GitHub, use deep-research.
verbs: [github.issue, github.comment, github.close]
requires: [ATTICUS_GITHUB_REPOS]
risk: tracked
outputs: [html]
cost: low
---

# github

The request was "write this down where the work lives." You cannot reach GitHub,
so your job is to compose the issue well and declare the intent. The pipeline has
the credential and runs `gh` after you exit.

## What exists, and what deliberately does not

Three verbs, and no fourth:

| verb | does | required | optional |
|---|---|---|---|
| `github.issue` | `gh issue create` | `title` | `body`, `repo` |
| `github.comment` | `gh issue comment` | `issue` (a number), `body` | `repo` |
| `github.close` | `gh issue close` | `issue` (a number) **or** `match` (text) | `reason`, `comment`, `repo` |

**There is no read.** You cannot list issues, check whether something is already
filed, look up a number by its title, or report the state of a PR. This is not an
oversight: an answer would have to arrive *during* your run, and the outbox only
carries intent *out*. If the request was a question about GitHub state, this skill
is the wrong one — say plainly in your report that Atticus cannot look things up on
GitHub yet, and do not guess an answer.

The one thing that looks like an exception is not one: `github.close` takes
`match`, and the **pipeline** resolves those words to an issue number after you
exit. You still never see the result — you cannot learn from it, report it, or
branch on it. Write the words you were given and let the receipt say what
happened.

**There is no push, merge, branch, workflow run, label edit or settings change,**
and asking for one is refused by name. The credential on the other side is a
write-capable token on the operator's own account; issue-filing is the slice of it
that is worth exposing to a microphone. Do not attempt to work around this with
`Bash` — `gh` is not installed inside your sandbox and no token is reachable from
it.

## The repository is not yours to choose

The operator configures an allowlist of repositories. **Omit `repo` and the default
one is used, which is the right move almost always.**

Include `repo` only when the request clearly names a project — "open an issue *on
the vault repo*" — and then pass exactly what was said, a bare name (`atticus`) or
`owner/name`. It is matched against the allowlist. Anything not on the list is
refused with a receipt naming what was permitted, and that is working correctly,
not a bug to route around: a misheard sentence must not be able to file into every
repository a token can reach. Never construct an owner you were not told, and never
guess a repo because it seemed related to the topic.

## Write the issue somebody else will read

A voice-filed issue arrives with no reporter to interrogate, so the body has to
carry everything. This is most of the value you add.

- **Title: one specific line, in the imperative or as a symptom.** "Budget guard
  compares the wrong two numbers", not "budget issue". It is what shows up in a
  list of forty.
- **Body: what was observed, where, and why it matters.** Say what the operator
  said, in plain prose. If the transcript is fragmentary — and it will be — write
  what it supports and stop.
- **Quote the request when the wording carries information.** A phrase like "it
  only happens after a reboot" is a clue, and paraphrasing burns it.
- **Mark what you inferred.** "Spoken as 'the budget guard', which I read as
  `ATTICUS_MAX_BUDGET_USD` in `processor/execute.py`" is useful and honest.
  Presenting a guess as an observation is how a wrong ticket costs an afternoon.
- **Do not invent reproduction steps, versions, stack traces or line numbers.** An
  issue that looks precise and is wrong is worse than a vague one.
- **One issue per problem.** Two unrelated complaints in one sentence are two
  files, `001-github.issue.json` and `002-github.issue.json`.

The pipeline appends a footer noting a machine filed it from a voice command, so
you do not need to explain that yourself.

## Commenting

`issue` must be a **number** — `42`, or `"42"`, or `"#42"`. You cannot look one up,
so if the request does not contain a number, do not file a comment on a guess. File
a new issue instead, or say in your report that the number was missing.

## Closing

Use `github.close` when the request is that something is **done, handled, or not
happening** — "close the issue about the flaky test", "mark that one complete",
"we're not doing the Slack thing, close it".

```json
{"verb": "github.close", "match": "flaky test", "reason": "completed",
 "comment": "Closed by voice: the retry was removed in #91."}
```

- **Give `issue` when the request contains a number**; otherwise put the
  identifying words in `match`. Never invent a number — a wrong one closes
  somebody else's work.
- **`match` is matched against OPEN issues in the allowlisted repo**, and the
  pipeline **refuses rather than guesses**: no match and several matches both
  fail, with the candidates named in the receipt. So prefer the distinctive
  words of the title over generic ones — `"Slack integration"` finds it,
  `"the issue"` will not.
- **`reason`** is `completed` (the default) or `not planned`. Use `not planned`
  when the request means "we're not doing this" — filing that as *completed*
  quietly tells everyone the work got done.
- **`comment`** is optional and usually worth adding: a machine closing an issue
  with no explanation is how people learn to distrust the tracker. One sentence
  saying what was said and why it is closed.
- **There is no reopen**, and no delete. If the request is to reopen something,
  say in your report that Atticus cannot, and why: closing is one click to undo
  by hand.

## Then write your report

The action is almost certainly **pending, not done** — tracked actions are held for
the operator to confirm by default. Say what you asked for and what it will do
once approved. Never write "I filed issue #57"; you do not know the number and
cannot know it.

```json
{"verb": "github.issue",
 "title": "Backlog alarm never fires because the timer is monotonic",
 "body": "Reported by voice on the drive home...\n\nWhat was said: ...\n\nWhy it matters: ..."}
```

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
