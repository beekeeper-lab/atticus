# Atticus

Voice-to-agent pipeline. Speak a task into a Plaud NotePin S; an agent on Forge
executes it and publishes the result to a private repo. No interaction between
"stop recording" and "read the output."

**Read `docs/SPEC.md` first.** It is the source of truth for architecture,
task breakdown, and open questions. This file is orientation only.

## Current state

**v1. Working end to end on real recordings** — 15 processed, 5 executed into
published HTML reports, each announced by a push notification carrying a link to
a private searchable site. Measured round trip ~30 minutes, of which ~13 is the
pipeline and the rest is device sync and poll intervals.

**Cloud → vault → agent output works end to end on real recordings.** Both
timers run on Forge. The pipeline has transcribed real audio, held the
wake-phrase gate on overheard speech, and published a 74 KB research report.

**The official Plaud CLI is paywalled** — we use a Plaud Web fetcher instead.
See [ADR-002](docs/decisions/ADR-002-plaud-web-fetcher.md). What recon
(T-06/T-07) settled, all now implemented in `ingest/plaud_web.py`:

- Auth is `Authorization: Bearer <workspace_token>`, harvested off a live
  request rather than reimplementing Plaud's three-step token dance. **The
  browser refreshes the token for us** — practical session life is the 30-day
  refresh window, not 24h.
- List: `GET /file/simple/web?skip&limit&is_trash=0&sort_by=start_time&is_desc=true`
- Audio: `GET /file/temp-url/{id}` → presigned S3 URL. Fetch it **without** an
  Authorization header. Prefer `temp_url` (MP3); `temp_url_opus` is often absent.
- **`ori_ready` is not a download gate** — it is `false` on files that fetch
  fine. An earlier revision refused to download on it, which was wrong.
- `_normalize()` is where Plaud's vocabulary stops. `duration` and `start_time`
  are **milliseconds** — the obvious trap.
- Application errors arrive as **HTTP 200** with non-zero `status`.
- Plaud seeds every account with three marketing files; `serial_number`
  starting `welcome_` is the reliable discriminator.

**The open problem is upstream of all of this:** getting audio off the pin
without a deliberate act. Sync requires the Plaud app foregrounded — see
`docs/transport-tests.md` for the matrix and the verdict. That work lives on
WarDog now (ADR-003).

Known open defects, from `docs/deploy/forge-2026-07-29.md`: the agent writes
deliverables straight into the vault instead of scratch (#2), and it runs
without web access (#3). Both need a maintainer decision, not a patch.

## Shape of the system

```
NotePin S ──BLE (app foregrounded)──► Plaud Cloud
                                           │  poll, every 15 min
                                           ▼
                                     FORGE  ingest ──► atticus-vault (git)
                                           │                    │
                                           │  git pull          │
                                           ▼                    │
                                     FORGE  processor ◄─────────┘
                                     transcribe → route → execute
                                           │
                                           ▼
                                  atticus-vault (git)
```

**Git is the queue, not just storage.** The two stages never talk directly —
only through commits — and that holds even now that they share a host.

| Role | Job | Timer |
|------|-----|-------|
| **ingest** | Plaud Cloud → `inbox/`. Owns `inbox/` + `.state/`. | 15 min |
| **processor** | `inbox/` → transcribe → route → execute → `processed/`. Owns `processed/` + `failures/`. | 5 min |

**Both roles run on Forge** as of 2026-07-29 ([ADR-003](docs/decisions/ADR-003-ingest-on-the-agent-host.md)).
WarDog keeps the unsolved device→cloud transport problem, which is a separate
project (`docs/transport-tests.md`). Roles are capabilities, not hostnames:
`./ops/install.sh {ingest|processor|all}`.

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
- **The agent cannot touch git**, and this is now enforced, not asserted. It
  runs under `bwrap` in a mount namespace with its own `HOME`: no `~/.ssh`, no
  `~/.config/ai/env`, no vault. Stripping `GIT_SSH_COMMAND` from its environment
  was never a control — the deploy key was readable and it has a shell.
  `ATTICUS_SANDBOX=off` disables this and is a real trade, not a formality.
- **`ios/` is a plain directory, not a submodule.** Split it out later if it
  ever ships independently.
- **Poll, don't wait for webhooks.** Twice over: ingest polls Plaud (no webhook
  documented for personal accounts) and the processor polls git (a GitHub
  webhook would mean exposing an inbound endpoint on the box that runs an
  autonomous agent).
- **Ingest polls every 15 minutes, and tightening that is not an improvement.**
  Audio only reaches Plaud Cloud while the Plaud app is *foregrounded*
  (`docs/transport-tests.md`), so arrivals are human-triggered bursts hours
  apart. Against that, 5 vs 15 minutes moves mean detection lag by 5 minutes
  and triples the daily headless-Chromium launches against an API we don't own.
  `PLAUD_POLL_DAYS` + the ledger make a missed window free. Revisit only if the
  transport becomes hands-off.
- **The Plaud credential lives on the ingest host, which is now also the agent
  host.** This reverses an earlier decision; the cost and the mitigation are
  spelled out in [ADR-003](docs/decisions/ADR-003-ingest-on-the-agent-host.md).
  The upshot: **the wake-phrase gate is load-bearing for credential safety now,
  not just for avoiding stray agent runs.** Do not disable it here.
- **Both roles push to one repo.** Every push is `pull --rebase` + bounded
  retry. They own disjoint paths — ingest `inbox/` + `.state/`, processor
  `processed/` + `failures/`.
- **A silent failure is the worst failure.** A dead Plaud session is
  indistinguishable from a quiet weekend: both are "0 new recordings" forever,
  while audio piles up in the cloud. Ingest alarms on it through
  `ATTICUS_NOTIFY_URL`, throttled to one message per condition per 6h. A failed
  push is the same shape and is logged loudly for the same reason.
- **The processor must never be able to read the Plaud session.**
  `InaccessiblePaths=%h/.local/share/claude-fetchers` on the processor unit.
  `ProtectHome=read-only` still permits reads, so without it the agent that
  executes ambient-audio-derived text can read the session cookie jar. Ingest
  keeps its own access; the processor never needed any.
- **Timers use `OnCalendar=`, never `OnUnitActiveSec=`.** The latter schedules
  from the service's last activation, a monotonic reference that is lost on a
  daemon-reload — after which systemd parks the timer at `next_elapse=infinity`
  and it never fires again while still reporting enabled *and* active.
  `atticus-vault-site.timer` did this for 76 minutes on 2026-07-29. A timer that
  never fires cannot alarm about not firing, which makes it the one failure that
  defeats every other safeguard here. `Persistent=true` is also a no-op on
  monotonic timers — it only works with `OnCalendar=`.
- **Sandbox options on systemd *user* units break ssh, and therefore break
  every push.** `ProtectSystem`/`ProtectHome`/`PrivateTmp` put the unit in a
  user namespace where root-owned files read as `nobody`, so ssh rejects
  `/etc/ssh/ssh_config.d/*.conf`. Both units carry
  `Environment="GIT_SSH_COMMAND=ssh -F %h/.ssh/config"`, which makes ssh skip
  the system config entirely. Don't remove it.

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
