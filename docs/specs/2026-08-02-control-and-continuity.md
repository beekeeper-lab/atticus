# Control and continuity — implementation spec

**Date:** 2026-08-02
**Source:** the functional requirements review (`docs/reviews/2026-08-02-*`), issues #82–#92
**Covers:** the six items we agreed to build. #85, #87 and #90 are deliberately absent — see "Not in this spec".

The review's principle governs every decision below:

> Increase the operator's ability to control, continue, correct, audit and
> retrieve what Atticus does — not the number of things it can do.

## How to use this document

Work is organised into **five threads**, not ten issues. A thread is a unit of
work one session can hold in its head and land as one or two PRs. Threads are
scoped by *the files they touch*, because that is what actually determines
whether two sessions can run at once without fighting.

Each thread states: what it is for, the files it owns, the design, acceptance
criteria, and — critically — **what it must not touch** so a parallel thread
stays safe.

---

## Parallelism plan

| Wave | Threads in parallel | Why they are safe together |
|---|---|---|
| 1 | **T1 Control plane** · **T2 Skill governance** | Zero file overlap. T1 lives in `notify.py`/`outbox.py`/new `approvals.py`; T2 lives in `execute.py`'s skill-copy region and every `SKILL.md` frontmatter. |
| 2 | **T3 Command lifecycle** · **T4 Continuity** | T3 owns `vault.py` statuses + `pipeline.py` stage guards + a new handler. T4 owns a new `projects.py`, `execute.py`'s *prompt* region, and the vault site. Overlap is one file (`execute.py`) in two different functions. |
| 3 | **T5 Meeting mode** | Gated on a consent decision, and touches `pipeline.py`'s transcribe stage, which T3 also edits. Do it after T3 lands. |

**Run T2 before T3 and T5.** T2 defines the skill frontmatter schema; T3 and T5
each add a new skill, and it is cheaper for them to be born conforming than
retrofitted.

### Conflict matrix

| | T1 | T2 | T3 | T4 | T5 |
|---|---|---|---|---|---|
| **T1** | — | none | `notify.py` (T3 sends one push; use the existing API, do not refactor) | none | none |
| **T2** | none | — | new SKILL.md must conform | none | new SKILL.md must conform |
| **T3** | see above | — | — | `pipeline.py` (different stages) | `pipeline.py` (transcribe vs status guards) |
| **T4** | none | `execute.py` — T2 edits `wrap_sandbox`, T4 edits prompt assembly. **Different functions; coordinate only if one refactors the file's shape.** | — | — | none |

### Rules for parallel threads

1. **Never refactor a shared file's structure** — add functions, do not move
   them. A rename in `execute.py` costs the other thread an afternoon.
2. **One PR per thread per wave.** `ops/pr.sh` rebases, so land early rather
   than accumulating.
3. **If you need something another thread owns, stub it and note it** rather
   than reaching across.

---

## T1 — Control plane: notification routing, then approvals

**Issues:** #91, then #83. **Effort:** 2–3 sessions. **Owns:**
`processor/notify.py`, `processor/outbox.py` (gate branch only), new
`processor/approvals.py`, new `processor/calendar_alert.py`, call sites in
`ops/heartbeat.py` and `ingest/poller.py`.

These are one thread because both rewrite how Atticus talks to the operator,
and the approval flow *is* a notification with a reply path. Build #91 first:
it is the substrate #83 stands on.

### T1a — severity routing and quiet hours (#91)

**The problem, proven twice this week.** Ingest died for 2d6h (#77) and the
site watcher died for 1.5 days; both alarmed correctly, into ntfy, where they
drowned. #66 established that a calendar event with an alert at its start does
break through iOS Focus. So the machinery to shout exists — what is missing is
a policy that decides when to use it.

**Design.**

1. **Extract the calendar mechanism.** `processor/handlers/reminders.py`
   currently owns `_calendar_companion()`. Move the Graph call into
   `processor/calendar_alert.py`:

   ```python
   def create_alert(cfg, *, when, subject, body, minutes=15, log=print) -> dict
   ```

   Reminders keeps its behaviour and calls this; nothing about #66 changes.
   The function must stay best-effort — no consent, no calendar, no failure.

2. **Three severities**, chosen so the boundary is about *consequence*, not
   feeling:
   - `critical` — the system is broken or silently losing work and only a human
     can fix it. Dead Plaud session, upstream auth change, a core timer or path
     watcher down, a failed push, an expired agent token.
   - `alert` — needs a decision but nothing is being lost. Budget thresholds,
     backlog over the alarm minutes, **an action awaiting approval**.
   - `routine` — a result. "Your report is ready."

3. **Channel table** (config-overridable, these are the defaults):

   | severity | ntfy | calendar alert | quiet hours |
   |---|---|---|---|
   | critical | yes, priority high | **yes**, event at now+2min | ignores them |
   | alert | yes | no | deferred to the morning |
   | routine | yes | no | deferred to the morning |

4. **Escalation is throttled harder than ntfy.** A 15-minute timer failing all
   night must not produce 96 calendar events. Reuse the existing per-condition
   stamp in `~/.cache/atticus/`, with a separate longer window for the calendar
   channel (`ATTICUS_ESCALATE_THROTTLE_HOURS`, default 12). First occurrence
   escalates; repeats inside the window stay on ntfy alone.

5. **Escalate on persistence, not on one failure.** A single failed pass is
   usually transient. Add `ATTICUS_ESCALATE_AFTER_FAILURES` (default 3):
   `notify()` gains an optional `streak=` and only escalates at or above it.
   The heartbeat already knows consecutive-failure state; the poller needs a
   small counter in the stamp file.

6. **Quiet hours** — `ATTICUS_QUIET_HOURS=22:00-07:00`, local zone, blank
   disables. Routine and alert pushes inside the window are **not dropped**:
   append to `.state/deferred-notifications.jsonl` and let the existing 07:00
   `ai-brief` open with "3 things happened overnight". Critical ignores quiet
   hours entirely — that is the point of the class.

**Call sites to update.** `ingest/poller.py`: the `F_CHANGED` branch currently
prints to stderr and returns with **no alarm at all** — this is #77 item 1, and
it is the reason the 2d6h outage was silent. Make it critical.
`ops/heartbeat.py`: unit failures and dead path watchers become critical.
`processor/pipeline.py`: results stay routine.

**Acceptance.**
- Simulated three consecutive ingest failures → one ntfy push *and* one calendar
  event; the fourth through tenth produce ntfy only.
- A routine result at 23:00 produces no push; the 07:00 brief names it.
- A critical at 23:00 produces both channels immediately.
- `F_CHANGED` alarms. Add a test — it currently has none.

### T1b — the approval queue (#83)

**The problem.** `outbox.gate()` returns `auto | confirm | off`, and `confirm`
means *held forever*: the intent is recorded in the receipt, the report says
pending, and **nothing can ever approve it**. In an unattended pipeline that
makes the middle setting identical to `off` with better paperwork. It is why
every verb we enabled is `auto` — an incentive to over-grant, which per-verb
gates existed to prevent.

**The security constraint that shapes the design.** The obvious build is an
approvals view in the vault browser: the write API and check-off UI already
exist from the todo work. **Do not build that.** The vault API answers on
loopback, the sandbox shares the host network namespace, and the write token is
embedded in every published page (#69). An agent acting on injected
instructions could scrape the token and *approve its own held actions* —
inverting the entire control.

Approval must happen on a channel the sandbox cannot reach. It can:

- The agent's environment is a strict allowlist (`LANG, LC_ALL, TZ, TERM, HOME,
  PATH, ATTICUS_OUTPUT_DIR`, plus its auth token). `ATTICUS_NOTIFY_URL` is
  **not** in it, so the agent cannot discover the topic.
- Approval arrives by the operator publishing to a **second ntfy topic** that
  the pipeline polls. No inbound endpoint on the agent host — which is the same
  reasoning that made this project poll rather than accept webhooks.

**Design.**

1. **`processor/approvals.py`** — an append-only ledger at
   `.state/approvals.jsonl`, the same shape as `todos.py` and `reminders.py`
   (third use of the pattern; copy it, do not abstract it yet).

   Event fields: `id, status, verb, request, stem, outdir, summary, risk,
   created_at, expires_at, decided_at, decided_by, result`.
   Statuses: `pending → approved|denied|expired → performed|failed`.

   `id = sha256(stem|file|verb)[:12]` — deterministic, so a `--retry` of the
   recording cannot enqueue the same action twice.

2. **`outbox.process()` change** — one branch. When `decision == "confirm"`,
   call `approvals.enqueue(...)` and set the receipt status to `held` **with
   the approval id and its expiry**. When `decision == "off"`, behaviour is
   unchanged: off means off, and must not silently become a queue.

3. **The push.** On enqueue, send an `alert`-severity notification carrying
   ntfy [action buttons](https://docs.ntfy.sh/publish/#action-buttons):

   ```
   Actions: http, Approve, https://ntfy.sh/<approval-topic>, method=POST,
            body='{"id":"a1b2c3","decision":"approve","nonce":"…"}';
            http, Deny, …
   ```

   The nonce is minted at enqueue and stored with the event; the drain rejects
   a decision whose nonce does not match. That stops a replayed or guessed
   message from approving something.

4. **The drain** runs inside the existing processor pass — **no new timer**.
   Poll the approval topic with ntfy's JSON poll endpoint
   (`GET https://ntfy.sh/<topic>/json?poll=1&since=<last>`), match ids, and for
   each approval replay the request through `outbox.validate()` +
   `handler["fn"]` — the same code path an `auto` action takes, so there is one
   implementation of every verb. Append the result; notify the outcome
   (routine).

5. **Expiry.** `ATTICUS_APPROVAL_TTL_HOURS` (default 24). Approving a
   three-day-old "post to Slack" is rarely right. Expired items produce one
   grouped notification, never silence.

6. **The vault browser gets a read-only view** of the queue — you can *see*
   what is pending, you *act* from the push. Reading is safe; the agent can
   already read the site.

**Config.** `ATTICUS_APPROVAL_TOPIC_URL` (blank disables the whole queue and
restores today's hold-forever behaviour), `ATTICUS_APPROVAL_TTL_HOURS`.

**Acceptance.**
- Set `ATTICUS_OUTBOX_VERB_OUTLOOK_DRAFT=confirm`, speak a draft request: a push
  arrives with Approve/Deny; tapping Approve performs the draft within one
  processor pass; the receipt and the vault view both show it performed.
- Deny marks denied and performs nothing.
- A replayed approval message with a stale nonce is refused.
- With `ATTICUS_APPROVAL_TOPIC_URL` blank, behaviour is exactly as today.

**Then open the gates that were waiting on this**: `outlook.draft` and
`outlook.event` move from `auto`-or-nothing to a real `confirm`.

---

## T2 — Skill governance

**Issue:** #89. **Effort:** 1 session. **Owns:** every `skills/*/SKILL.md`
frontmatter, `processor/execute.py`'s skill-copy region, `skills/README.md`,
a new test module.

**Why it is worth a session.** Today a skill's frontmatter carries a name and a
description. Risk class, required credentials and refusal behaviour live in the
handler — correct for enforcement, useless for *routing*. The consequence is
that every unconfigured skill still advertises itself, the agent routes to it,
and the handler refuses after the fact. On a fresh install that is every skill.

And on 2026-08-02 `github.close` shipped with the body documenting the verb and
the description still saying "Do NOT use it to close anything". The agent obeyed
the description and refused — correctly. **Routing reads the description; a
capability the description denies does not exist.** One hand-written test now
guards that; a schema guards it structurally.

**Design.**

1. **Frontmatter schema** (additive; `name` and `description` unchanged):

   ```yaml
   verbs: [github.issue, github.comment, github.close]
   requires: [ATTICUS_GITHUB_REPOS]      # config keys that must be non-empty
   risk: tracked                          # highest risk among its verbs
   outputs: [html]
   cost: low                              # low | medium | high — agent turns, not dollars
   ```

2. **Hide unconfigured skills from routing.** In `execute.py`, where
   `skills_dir` is copied into the workspace, read each `SKILL.md`'s
   `requires:` and skip the copy when any named config value is empty. Log
   `skill 'slack' not copied — ATTICUS_SLACK_BOT_TOKEN is unset`. The agent
   then reports "I have no capability for that" instead of producing a
   confident intent that dies in a receipt.

   Keep it fail-open on a parse error: an unreadable frontmatter must not
   silently disarm a working skill.

3. **The consistency test** — this is the real deliverable:
   - every verb declared in any `SKILL.md` is registered by a handler;
   - every registered handler verb is declared by exactly one skill;
   - each skill's `risk` matches the highest risk of its registered verbs;
   - each skill's `description` does not *prohibit* a verb it declares
     (a keyword check for the "do NOT … close/comment/file" shape).

**Acceptance.** Unset `ATTICUS_SLACK_BOT_TOKEN`; a spoken "post to Slack"
produces a report saying the capability is unavailable, and no Slack intent file
exists. Add a verb to a handler without declaring it → tests fail.

---

## T3 — Command lifecycle: status, cancel, retry

**Issue:** #82 (minus amend). **Effort:** 2 sessions. **Owns:**
`processor/vault.py` (status constants), `processor/pipeline.py` (stage guards),
new `processor/recordings.py`, new `processor/handlers/atticus.py`, new
`skills/atticus/SKILL.md`.

**Why amend is dropped.** "Make it about X instead" is cancel-plus-restate with
extra ambiguity about what carries over. Two verbs that each do one thing beat
one verb that guesses. If it is still wanted after living with cancel, it is its
own issue.

**Design.**

1. **Two new statuses** in `vault.py`: `CANCELLED = "cancelled"`,
   `SUPERSEDED = "superseded"`. Add both to `_PROGRESS` (terminal, same rank as
   `published`). `load_records()` consumers must skip them the way they skip
   `published`.

2. **`processor/recordings.py`** — resolve speech to one recording:

   ```python
   def resolve(vault, phrase, *, within_days=7, exclude_stem="") -> dict
   ```

   Matches against the transcript text, the deliverable title, and simple time
   words ("this morning", "yesterday", "the last one"). **Refuses on
   ambiguity**, naming candidates — the rule now used for repos, contacts,
   issues and todos. `exclude_stem` exists for the guard in point 4.

3. **`processor/handlers/atticus.py`**, all `INTERNAL` (they act only on the
   operator's own pipeline):

   - **`atticus.status {match?}`** — resolve, then send the operator a push
     describing stage, timing, output and link. **The agent never sees the
     answer**, so this is not a read and does not touch #63. Omit `match` to
     describe the most recent recording.
   - **`atticus.cancel {match}`** — resolve, then:
     - status in `raw|transcribed|routed` → mark `cancelled`; the next pass
       skips it.
     - status `executing` → read `executing_by.pid` (already recorded), verify
       `/proc/<pid>/cmdline` still contains `pipeline.py` **and** the stem
       before signalling — PID reuse is real — then `SIGTERM` the process
       group. bwrap runs with `--die-with-parent`, so the whole tree dies. Mark
       `cancelled`.
     - status `published` → mark `superseded`; the artifact stays (it is
       already committed and possibly published), but the vault view greys it.
   - **`atticus.retry {match}`** — resolve, call the existing `rec.rearm()` that
     `pipeline.py --retry` uses, and let the next pass run it.

4. **The guard that matters: a run must not cancel itself.** Pass the
   pipeline's `_stem` as `exclude_stem`. Without it, "cancel that" spoken into
   a recording that is itself executing kills the run performing the
   cancellation, which then never records the cancellation — a genuinely
   confusing failure.

5. **Cap at one recording per request.** No "cancel everything".

6. **`skills/atticus/SKILL.md`** — conforming to T2's schema. The description
   must route "what happened to", "cancel that", "try that again" here and
   *away* from the other skills.

**Acceptance.** Start a long research run; speak "cancel the research I just
started"; within one pass the process is gone, the record is `cancelled`, and a
push says so. Speak "what happened to the consulting research" → a push with
its status. Cancelling the currently-executing command is refused by name.

---

## T4 — Continuity: named projects and artifact versions

**Issues:** #84, #88 (versioning half only). **Effort:** 2–3 sessions.
**Owns:** new `processor/projects.py`, `processor/execute.py`'s prompt-assembly
region, `processor/pipeline.py`'s publish step, and the vault repo's
`site/build.py`.

These are one thread because versioning without projects has nothing to attach
to: a recording is immutable, so "v2 of that report" is a property of a
*project artifact*, not of a recording.

**Scope discipline.** The review proposes projects holding tasks, artifacts,
repos, documents, contacts, instructions and preferences. **Build the smallest
thing that makes continuation real** — a brief and a list of artifacts. Every
capability here that has worked well started as the narrowest version.

**Design.**

1. **Vault layout** (operator-authored to begin with; no creation verb yet):

   ```
   projects/<slug>/brief.md          what this project is, in the operator's words
   projects/<slug>/index.json        {name, aliases[], created, artifacts[]}
   projects/<slug>/artifacts/<artifact-slug>/v1.html, v2.html, …
   ```

2. **`processor/projects.py`** — `load()`, `resolve_from_text(transcript)`
   (name and alias matching, refuse on ambiguity), `brief_text(slug, cap)`,
   `link_artifact(slug, rec, artifact_slug=None)`.

3. **Prompt assembly** — when a project resolves, prepend a bounded block
   *before* the transcript:

   ```
   ## Project context: <name>   (reference only — the instruction is below)
   <brief.md, capped at ATTICUS_PROJECT_CONTEXT_CHARS, default 2000>
   Recent artifacts: <up to 5 titles>
   ```

   **Fence it exactly as the transcript is fenced.** The brief is
   operator-written, but artifact titles are agent-written and a previous run
   may have ingested a hostile web page. Treat all of it as data, and say so in
   the preamble. This is #63's *pre-fetch* option, in its safest possible form:
   bounded, pipeline-assembled, operator-scoped. **Note it on #63** — it is a
   concrete case to decide against rather than an abstraction.

4. **Versioning.** An outbox request or metadata field `revises:
   <artifact-slug>` makes the publish step write `v<n+1>` into the project's
   artifact directory and update `latest`. Absent that, a new artifact is `v1`.
   Recordings stay immutable; the project is where history accumulates.

5. **Vault site** — a projects view listing projects, their briefs, and their
   artifacts with version history. Reuse the existing card/CSS vocabulary; this
   is the same shape as the todo page added on 2026-08-01.

**Acceptance.** Create `projects/consulting/brief.md`; speak "add to the
consulting project: research X"; the run's prompt contains the brief, the
deliverable is linked into the project, and the vault shows it. Speak "revise
that with Y" → `v2` beside `v1`, both reachable.

---

## T5 — Meeting mode (gated)

**Issue:** #86. **Effort:** 1–2 sessions *after* the gate clears.
**Owns:** `processor/transcribe.py`, `processor/pipeline.py`'s transcribe stage,
new `skills/meeting/SKILL.md`.

**The gate is not technical.** A meeting recording contains people who did not
agree to be transcribed by an AI, committed to git, and published to a browser.
The vault already permanently holds one 40-minute ambient recording. Retention
expires audio from the working tree at 30 days but **git history keeps it**.
Before any code:

- a written policy — who may be recorded, what is retained, what is published,
  what is announced to the room — as an ADR;
- a retention decision specific to meeting audio (likely: never commit the
  audio, only the transcript, or expire faster);
- confirmation that this is used where the operator has the standing to record.

**Cost is not the barrier**, contrary to the review's framing: 60 minutes at
`gpt-4o-transcribe` is about $0.36. The barrier is consent.

**Design, once cleared.**

1. **Mode selection** — explicit and spoken: "Atticus, meeting mode" or "take
   notes on this meeting" as the opening phrase. Do **not** infer from duration;
   a long recording is currently a truncated command, and silently switching
   behaviour on length would make a mis-fired command into a transcribed
   meeting.
2. **Chunked transcription** becomes the default for this mode (the machinery
   exists and is opt-in today), with the existing `plan_chunks` cost guards
   made load-bearing rather than advisory.
3. **Extraction skill** producing a structured report: summary, decisions,
   action items with owners, follow-ups, open questions.
4. **Action items become `todo.add` requests** — the verb already exists. Watch
   the outbox per-pass cap (`ATTICUS_OUTBOX_MAX_ACTIONS`, default 5): a real
   meeting yields more than five. Either raise it for this path or batch items
   into one request. **Do not silently drop the sixth.**

**Acceptance.** A recorded meeting produces a report whose action items appear
on the todo list, and whose audio obeys the retention decision made at the gate.

---

## Not in this spec, and why

- **#85 multi-task recordings** — the outbox already runs several *actions* per
  recording. Whether real speech contains multiple *tasks* is an empirical
  question; 38 real transcripts now exist and should be grepped before any
  design. Deferred pending evidence.
- **#87 question-answering over artifacts** — not separable from #63, and T4's
  context pack is the same mechanism. It will be decided by T4's outcome.
- **#90 workload priorities** — speculative. No contention has been observed at
  a handful of recordings a day and a 13-minute pipeline. Revisit if it ever is.
- **Amend** (part of #82) — dropped in favour of cancel-plus-restate.
- **Additional output formats** (part of #88) — each new publishable suffix is a
  decision for the vault publisher's allowlist, which is a live security
  control. Add them one at a time, on demand, with their sanitising story.

## Sequencing summary

```
wave 1   T1 control plane  ────────────────►  (T1a routing, then T1b approvals)
         T2 skill governance ──►

wave 2                          T3 lifecycle ──────►
                                T4 continuity ─────────►

wave 3                                          T5 meeting mode (after consent)
```

T2 is the shortest and unblocks the two threads that add skills. T1a is the
highest-urgency single item, because two silent multi-day outages this week were
both delivery failures rather than detection failures.
