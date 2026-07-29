# Atticus

Speak a task into a wearable recorder; an agent executes it and publishes the
result. No interaction between "stop recording" and "read the output."

```
NotePin S ──wifi/BLE──► Plaud Cloud ──poll──► Forge ──► atticus-vault (git)
                                               │
                                    ingest → transcribe → route → execute
```

**Start with [`docs/SPEC.md`](docs/SPEC.md).** Architecture, full task
breakdown with owners and dependencies, and the open questions that gate the
build.

## Layout

| Path | What |
|------|------|
| `docs/` | Spec (source of truth) and ADRs |
| `ingest/` | Plaud → vault poller. Runs on Forge. |
| `processor/` | Transcribe → route → execute. Runs on Forge. |
| `ops/` | systemd units, env templates, install scripts |
| `ios/` | Contingent iOS app. Empty — see ADR-001. |

Audio and generated output live in a separate private repo, `atticus-vault`.

## Status

Spec drafted; nothing built; hardware not yet in hand. Three arrival-day tests
gate the work — see SPEC §8.

## Requirements

Forge (Linux): Python 3.11+, Node ≥20, `@plaud-ai/cli`, `faster-whisper`, git.

No Mac required for v1.
