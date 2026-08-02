# Functional requirements review — 2026-08-02

Source: `2026-08-02-functional-requirements-review.pdf` (in this directory).
Verbatim text below, then an assessment against what the system actually does.

**This file is the design record for that review. It is not a plan** — the
actionable residue lives in GitHub issues labelled `from-review`, tracked from
the issue linked at the bottom.

## The guiding principle, which is the part worth keeping

> Do not merely increase the number of things Atticus can do. Increase the
> operator's ability to control, continue, correct, audit, and retrieve what
> Atticus does.

That is the right frame and it is worth quoting back at any future feature. The
central recommendation — evolve from "voice-triggered batch processor" into a
"durable asynchronous personal operations system" — is a direction, not a task.

## The ten priorities, as written

1. **Command lifecycle controls** — cancel, amend, retry, status inquiries;
   explicit states (received, transcribed, queued, running, awaiting approval,
   completed, failed, cancelled, superseded).
2. **Controlled external actions** — risk tiers (read-only, reversible write,
   externally visible write, destructive), approval queues, drafts, previews,
   dry-run, idempotent execution.
3. **Persistent context and projects** — named projects holding tasks,
   artifacts, repos, documents, contacts, instructions, preferences; commands
   that continue or revise earlier work; bounded context packs.
4. **Multi-step command composition** — several tasks per recording;
   sequential, parallel and conditional execution; resolving "that report" or
   "the second option" before splitting work.
5. **Capture modes** — notes, ideas, meetings, dictation. Meeting mode extracts
   action items, decisions, follow-ups. Idea mode produces idea cards.
6. **Search and retrieval** — search transcripts, reports, projects, artifacts;
   answer questions grounded in prior artifacts; daily and weekly summaries;
   artifact collections.
7. **Rich output contracts** — HTML stays canonical, plus PDF, Markdown, DOCX,
   CSV, JSON, images, source, calendar files; artifact versioning; standard
   report sections.
8. **Skill governance** — skill metadata for permissions, risk, required
   credentials, runtime, cost, output types; health checks; permission scoping.
9. **Cost and resource controls** — budgets, runtime limits, workload
   priorities, detailed cost accounting.
10. **Notification improvements** — routing, escalation policies, quiet hours,
    completion summaries.

Recommended new skills: project capture, revise artifact, status and recovery,
meeting actioner, repository work, document transform, watch condition, contact
resolver.

Proposed command model:

    Recording -> Command -> Tasks -> Project Context -> Risk Classification
              -> Approval State -> External Actions -> Artifact Versions

with relationships: amends, retries, cancels, continues, references,
derived_from, supersedes.

Proposed order: status/cancel/retry/amend → approval queue and risk tiers →
named projects → multi-task recordings → search → meeting mode → conditional
monitoring → integrations → multi-format output → cost and notification policy.

## Assessment against the system as it stands on 2026-08-02

The review reads as though written against an earlier snapshot. Several
priorities describe work that is already merged, and one of its "recommended
new skills" shipped two days ago. Recorded here so nobody rebuilds them.

| # | Status | What actually exists |
|---|---|---|
| 1 | **Partly** | Explicit states exist and are the pipeline's spine: `raw, transcribed, routed, executing, executed, published, failed, retry_wait`, committed per stage, resumable, idempotent. `EXECUTING` was added specifically to make execute exactly-once. Missing: `cancelled` / `superseded`, and **any voice control at all** over an in-flight or past command. |
| 2 | **Largely done** | The outbox (#42, merged 2026-08-01) is exactly this framework: three risk tiers (`internal`, `tracked`, `outward`), per-class and per-verb gates, a per-pass action cap, committed receipts, idempotency keys on todo and reminders, `--dry-run`. Drafts exist literally (`outlook.draft` never sends). **The real gap is the approval queue**: `confirm` currently means "held forever" — nothing can ever approve a held action after the fact. |
| 3 | **New** | Nothing like it. The largest item in the review. |
| 4 | **Partly** | The outbox already carries several ordered actions per recording (`001-`, `002-`). Missing: several *tasks* with dependencies, and reference resolution. |
| 5 | **Partly** | Notes are the default path for anything the wake gate declines; long-recording chunking exists but is opt-in. Meeting/idea/dictation modes do not exist. |
| 6 | **Partly** | The vault browser does full-text search over every document and transcript, with tags and read state. The 07:00 `ai-brief` produces a daily summary. Missing: *answering questions* about prior work, which is a read during the run — that is #63, not a new item. |
| 7 | **Partly** | HTML is canonical and sanitised; the doc bar has a working PDF path; audio episodes exist. Missing: the other formats and artifact versioning. |
| 8 | **Partly** | Risk class, credential requirements and refusal behaviour are real but live in the *handler*, not in skill metadata; `atticus doctor` and the heartbeat cover some health. Missing: the metadata block and health checks per skill. |
| 9 | **Largely done** | Split per-service budgets (transcription, TTS), a usage ledger written after every priced call, a cost page in the vault browser with per-interaction accounting, percentage budget alarms, per-run ceilings, `ATTICUS_EXEC_TIMEOUT`. Missing: workload priorities. |
| 10 | **Partly** | Result vs alarm topics, detail levels, per-condition throttling, clear-on-recovery. **And the review's premise is now proven right by incident**: ntfy alone is too weak — #66 established a calendar-alert channel that breaks through Focus, and #77 records an ingest outage whose only alarm went to the channel the operator cannot hear. Escalation policy is the live question. |

Of the recommended skills, **contact resolver already shipped** (#43, ADR-006)
— as pipeline-side infrastructure rather than a skill, deliberately.

## The one architectural idea worth taking seriously

The **command model** is the review's real contribution, and it is upstream of
priorities 1, 3 and 4. Today a *recording* is the only first-class object; there
is no `Command` distinct from it, no relationships between commands, and so no
way to say "cancel that", "amend that", or "continue the thing from Tuesday".
Adding that object is what would make the lifecycle verbs possible rather than
each being a special case.

Whether it is worth the weight is the open question — see the tracking issue.
