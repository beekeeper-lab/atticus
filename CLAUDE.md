# Atticus

Voice-to-agent pipeline. Speak a task into a Plaud NotePin S; an agent on Forge
executes it and publishes the result to a private repo. No interaction between
"stop recording" and "read the output."

**Read `docs/SPEC.md` first.** It is the source of truth for architecture,
task breakdown, and open questions. This file is orientation only.

## Current state

Spec drafted. Fetcher contract + recon tool written. Hardware not yet in hand.

**The official Plaud CLI is paywalled** — we use a Plaud Web fetcher instead.
See [ADR-002](docs/decisions/ADR-002-plaud-web-fetcher.md).

**Recon is done** (T-06, 2026-07-28). A new Plaud account seeds three demo
files, which was enough to observe the API without a device:

- Auth is `Authorization: Bearer <workspace_token>`, harvested off a live
  request rather than reimplementing Plaud's three-step token dance. **The
  browser refreshes the token for us** — practical session life is the 30-day
  refresh window, not 24h.
- List: `GET /file/simple/web?skip&limit&is_trash=0&sort_by=start_time&is_desc=true`
- `_normalize()` is where Plaud's vocabulary stops. `duration` and `start_time`
  are **milliseconds** — the obvious trap.
- Application errors arrive as **HTTP 200** with non-zero `status`.

`ingest/plaud_web.py` implements auth, `list`, and `whoami`, unit-tested against
captured payloads. **`audio_url()` deliberately raises** — the download path was
never observed because the account has nothing exportable.

Arrival day, in order:

1. **Q5 / T-30, T-32** — do sub-30-second recordings exist and export at all?
   Stop everything if not; the design is wrong.
2. **T-06b** — re-run `ingest/plaud_discover.py` against a real recording and
   **export its original audio**. Every demo file had `ori_ready: false`, so
   the original probably needs server-side preparation.
3. **T-07b** — implement `audio_url()` from that report. Do not guess: a
   fetcher that downloads a not-ready object writes truncated files that look
   successful, and the ledger then never retries them.
4. **Q2 / T-22** — does the session work headless on WarDog?

## Shape of the system

```
NotePin S ──wifi/BLE──► Plaud Cloud ──poll──► WARDOG ──► atticus-vault (git)
                                              ingest          │
                                                              │ git pull
                                                              ▼
                                                            FORGE
                                              transcribe → route → execute
                                                              │
                                                              ▼
                                                     atticus-vault (git)
```

**Git is the queue, not just storage.** The two halves never talk directly —
only through commits.

| Host | Role | Holds |
|------|------|-------|
| **WarDog** | Ingest. Plaud Cloud → vault. Must be always-on. | `~/.plaud/tokens.json` |
| **Forge** | Everything downstream. Can be offline; work waits in git. | whisper, `claude` |

Two repos: `atticus` (this, code) and `atticus-vault` (private, audio + output).
Two deploy keys, one per host, both with write access.

## Standing decisions

- **No iPhone app in v1.** The pin's own Wi-Fi reaches Plaud Cloud without a
  phone. See `docs/decisions/ADR-001-no-iphone-app-v1.md`. `ios/` stays empty
  unless the ADR's trigger conditions fire.
- **No Mac needed** for anything in v1. Only the contingent W8 iOS work
  requires one, and even then only realistically for BLE debugging.
- **Plaud Web fetcher, not the CLI or MCP** — both are paywalled (ADR-002).
  The free Starter tier still exposes original audio, and we consume zero
  transcription minutes because AutoFlow needs >200 words to fire.
  Avoid the community reverse-engineered toolkits: they want a plaintext
  password. Ours uses a 1Password-backed browser session and stores none.
- **Authenticate with Playwright, then call the JSON API** — do not scrape the
  DOM. Narrower and more stable target.
- **Transcription is OpenAI `gpt-4o-mini-transcribe`, not Plaud and not local.**
  AutoFlow needs >200 words to fire, so short commands arrive with no Plaud
  transcript at all. We use the *same* endpoint, model and steering prompt as
  the machine's existing dictation (hyprwhspr) — one transcription stack, not
  two. Key from `~/.config/ai/env`, never in this repo. This reverses an
  earlier "local faster-whisper" decision; see SPEC §2.3 for why, including the
  privacy consequence.
- **Routing is the model's job, not ours.** `claude -p` runs in a workspace
  with `.claude/skills/` linked to `skills/` and picks a matching skill from
  its description. Adding a capability = adding a skill directory. There is no
  routing table and there should not be one.
- **The agent never touches git.** It writes to a scratch dir; the pipeline
  copies and commits. No deploy key in its environment.
- **`ios/` is a plain directory, not a submodule.** Split it out later if it
  ever ships independently.
- **Poll, don't wait for webhooks.** Twice over: WarDog polls Plaud (no webhook
  documented for personal accounts) and Forge polls git (a GitHub webhook would
  mean exposing an inbound endpoint on the box that runs an autonomous agent).
  2-minute intervals; device sync latency dominates anyway.
- **The Plaud credential never touches Forge.** `tokens.json` lives on WarDog.
- **Both hosts push to one repo.** Every push is `pull --rebase` + bounded
  retry. They own disjoint paths — WarDog `inbox/` + `.state/`, Forge
  `processed/` + `failures/`.

## Git workflow — read before committing

**This repo (`atticus`): never push to `main` directly. Use `ops/pr.sh`.**

```bash
./ops/pr.sh "Short title" "optional longer body"
```

It pulls latest, branches, commits, pushes, opens a PR, squash-merges, and
returns you to an up-to-date `main`. No approval needed — the point is the
pull-and-merge discipline, because WarDog and Forge both edit this repo.

It also refuses to commit `ops/.env`, `docs/recon/`, `.scratch-vault/`, or any
credential-shaped string, regardless of what `.gitignore` says.

**The vault (`atticus-vault`): the opposite — commit directly, no PRs.** It is
machine-written every few minutes from two hosts and *is* the pipeline's queue.
A PR per commit would add a merge step to every message and stall the handoff.
Concurrency there is handled by `pull --rebase` + bounded retry in
`processor/vault.py`. `ops/pr.sh` detects the vault and refuses to run.

## Conventions

- Python 3.11+ on both hosts. Shelling out to `plaud` is the supported path.
- Timestamps are UTC ISO-8601 in filenames: `2026-07-28T142211Z_<plaud_id>`.
- Pipeline status lives in each recording's metadata JSON, so every stage is
  resumable and idempotent after a crash. Never process an ID twice.
- Durable decisions go in `docs/decisions/` as ADRs and get reflected in
  `docs/SPEC.md`. HTML renderings in `docs/` are generated views — the Markdown
  is authoritative.

## Security posture

The processor executes text derived from ambient audio. Anything spoken near
the pin can become an instruction. Keep the blast radius small: unprivileged
user, vault-scoped working directory, deny-by-default tool permissions, no
credentials in scope beyond the vault deploy key. Every executed prompt is
committed, so git history is the audit trail.
