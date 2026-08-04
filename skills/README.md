# skills

Voice-command capabilities. `claude -p` runs with `.claude/skills/` linked
here, reads each `SKILL.md` description, and picks a match. **Adding a
capability means adding a directory** — there is no routing table.

No skill matches → the agent just does what was asked. That is the intended
fallback, not a failure.

## Project skill or global skill?

Two homes, and the split is not arbitrary:

> **Project skills are *intents*. Global skills are *capabilities*.**

| | Project (`atticus/skills/`) | Global (Forge `~/.claude/skills/`) |
|---|---|---|
| Answers | "What did Gregg ask for?" | "How is this done well here?" |
| Triggered by | A spoken request | Another skill, or any session |
| Examples | `deep-research`, `price-scout` | `html-artifact-output`, `dataviz` |
| Versioned with | This repo | Forge's own config |

If it maps to something you'd say out loud, it belongs here. If it is a house
standard or an integration that several intents reuse, it belongs global —
otherwise every intent skill re-specifies it and they drift.

## Writing one

Frontmatter needs `name` and a `description` that says both when to use it
**and when not to** — that negative half is what stops the model picking the
wrong skill when two overlap.

```yaml
---
name: price-scout
description: |
  Researches purchase options and produces an HTML comparison...
  Use when the transcript asks to find, buy, price, or compare products...
  Do NOT use for general research (use deep-research) or for adding
  something to a list (use capture-task).
verbs: [price.watch]                    # outbox verbs this skill may emit
requires: [ATTICUS_PRICE_API_KEY]       # config that must be set, or it is hidden
risk: tracked                           # the highest risk among its verbs
outputs: [html]
cost: medium                            # agent turns, not dollars: low | medium | high
---
```

### The metadata block, and why it is enforced (#89)

Everything below `description` is machine-read, and two things consume it.

**`requires:` decides whether the skill exists at all on this host.** Each entry
names an environment variable; if the corresponding setting is empty, the skill
is **not copied into the agent's workspace** and the agent never learns it
exists. That is deliberate: every credential ships blank, so on a fresh install
an offered-but-unconfigured skill means the agent routes to it, composes a
request, and writes a confident report about an action the handler then refuses.
"I have no way to do that" is a better answer than a pending action that never
had a chance. The mapping is mechanical — `ATTICUS_SLACK_BOT_TOKEN` →
`cfg.slack_bot_token` — and a `requires:` naming a setting that does not exist
fails the test suite rather than hiding the skill forever.

**`verbs:` must agree with the handlers, in both directions.** The suite asserts
every declared verb is registered and every registered verb is declared exactly
once. It also asserts the `description` does not *prohibit* a verb the skill
declares — which is not a documentation nit:

> On 2026-08-02 `github.close` shipped with the verb implemented, the body
> documenting it, and the description still saying "Do NOT use it to … close
> anything". A spoken request reached the skill and the agent refused, citing
> its own instructions. **Routing reads the description; a capability the
> description denies does not exist.**

A skill that produces only a document declares `verbs: []`.

Then write for the actual situation: **the person dictated a request and walked
away.** Nobody can answer a clarifying question, the transcript may contain
errors, and the result will probably be read on a phone. Every skill here
should lead with the answer, take a position, and say what it could not
establish.

Output goes to the directory named in the task preamble, as one
self-contained HTML file. Name it for the topic.

## Status

| Skill | State |
|---|---|
| `deep-research` | ✅ built |
| `podcast-companion` | ✅ built — companion only, never routed to directly |
| `ai-brief` | ✅ built — driven by a timer, not by speech |
| `github` | ✅ built — files an issue, comments, or CLOSES one through the outbox (#50). No push, merge, workflow or settings, and no reads |
| `atticus` | ✅ built — status, cancel and retry for earlier recordings (#82). The only skill that acts on the pipeline itself |
| `meeting` | ⏸ built but **inert** — needs `ATTICUS_MEETING_MODE=on`, which needs [ADR-008](../docs/decisions/ADR-008-recording-other-people.md) accepted. Not offered to the agent while off |
| `reminders` | ✅ built — a push at a time, via ntfy and a one-minute timer (#52). Needs NO external service and no credential; one-shot only, and a REMINDER (delivery at a time) is deliberately distinct from a `todo` (no deadline behaviour) |
| `illustrate` | ✅ built — companion only, never routed to directly. Needs `ATTICUS_IMAGES=on`, which ships **off**: `image.generate` is the only verb that spends money, on a provider `ATTICUS_MAX_BUDGET_USD` does not cover. Diagrams stay inline SVG; this is for pictures that must be drawn |
| `price-scout` | planned — SPEC W9 |
| `idea-to-spec` | planned — SPEC W9 |
| `capture-task` | planned — SPEC W9, needs credentials |
