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
---
```

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
| `price-scout` | planned — SPEC W9 |
| `idea-to-spec` | planned — SPEC W9 |
| `capture-task` | planned — SPEC W9, needs credentials |
