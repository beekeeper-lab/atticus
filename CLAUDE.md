# Atticus

Voice-to-agent pipeline. Speak a task into a Plaud NotePin S; an agent on Forge
executes it and publishes the result to a private repo. No interaction between
"stop recording" and "read the output."

**Current truth lives in this file, `README.md`, `docs/configuration.md` (every
setting, generated from the code) and `docs/decisions/` (ADRs).** Read those
first.

`docs/SPEC.md` is the **design record**, not the source of truth — it says so
itself and deliberately retains superseded reasoning. It used to be labelled
both ways, which is how several of its stale details came to be read as current.
Go there for *why* a decision was made, the W0–W9 task breakdown, and the
contingent iOS work; do not trust it for a schema, an interval or a file path.

## Current state

**v1. Working end to end on real recordings** — 15 processed, 5 executed into
published HTML reports, each announced by a push notification carrying a link to
a private searchable site. Measured round trip ~30 minutes, of which ~13 is the
pipeline and the rest is device sync and poll intervals.

**As of 2026-08-02 the system also acts and is controllable.** Eleven skills;
todo, reminders, GitHub (file/comment/close), Slack, and Outlook drafts and
events all proven from real or synthesised speech. Voice lifecycle control
(`atticus.status` / `cancel` / `retry`), named projects with versioned
artifacts, an approval queue for held actions, and severity-routed
notifications. 927 tests. Meeting mode is built but **inert**, waiting on
[ADR-008](docs/decisions/ADR-008-recording-other-people.md).

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
- **Harvest the bearer from a data request, never from the token dance.** A
  page load whose cached 24h workspace token has gone stale opens with
  `POST /user-app/auth/workspace/refresh/<ws_id>`, and that request carries the
  ~30-day *refresh* token. Take it and every call fails
  `status=-3901 'token type does not match parse mode'` — which is the server
  being precise, not upstream drifting. Taking the first bearer indiscriminately
  cost ten days of ingest (2026-08-06 → 08-16) while reporting itself as
  "upstream changed — re-run recon". `TOKEN_EXCHANGE_PATHS` in `plaud_web.py` is
  the exclusion list; `/user-app/profile/` is real data and must stay
  harvestable, so the prefix is `/user-app/auth/` and not `/user-app/`.
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

**The one manual step is upstream of all of this, and it is now settled as
permanent:** sync requires the Plaud app foregrounded (`docs/transport-tests.md`).
Direct BLE was investigated to a conclusion on 2026-07-31 and is **closed** — the
pin reports `portVersion = 20`, so its firmware demands an RSA pre-handshake keyed
by a B2B-issued credential *and* ChaCha20 framing whose key exchange is undecoded.
Reaching the device was never the problem; it connects from Linux fine. See
[ADR-005](docs/decisions/ADR-005-direct-device-access-is-closed.md) for the
verdict, the rejected alternatives (reflash, build our own, unbind) and the
trigger conditions. **Do not re-derive this** — T6–T8 in `docs/transport-tests.md`
record it in detail.

**Defects are tracked in GitHub issues** as of 2026-07-31. Before that they lived
in prose in `docs/history/`, which is how the two defects this paragraph used to
list as open — the agent writing deliverables into the vault instead of scratch,
and running without web access — stayed listed for a week after being fixed in
PRs #13 and #20. If you are looking for open work, `gh issue list` is the answer,
not this file.

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
The device→cloud transport was WarDog's remaining project; it is now **closed, not
unsolved** ([ADR-005](docs/decisions/ADR-005-direct-device-access-is-closed.md)).
Roles are capabilities, not hostnames: `./ops/install.sh {ingest|processor|all}`.

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
- **Transcription is OpenAI `gpt-4o-transcribe`, not Plaud and not local.**
  AutoFlow needs >200 words to fire, so short commands arrive with no Plaud
  transcript at all. We use the *same* endpoint and steering prompt as the
  machine's existing dictation (hyprwhspr) — one transcription stack, not two —
  but a **larger model than dictation's `gpt-4o-mini-transcribe`**, because a
  dictation typo is visible and instantly fixable while a misheard word here
  becomes an autonomous agent's instruction. Do not "unify" these without
  re-reading the cost/error table in SPEC §2.3. Key from `~/.config/ai/env`,
  never in this repo. This reverses an earlier "local faster-whisper" decision;
  see SPEC §2.3 for why, including the privacy consequence.
- **Routing is the model's job, not ours.** `claude -p` runs in a workspace
  whose `.claude/skills/` is a **copy** of `skills/` (symlinks dangle inside the
  mount namespace), and picks a matching skill from its description. Adding a
  capability = adding a skill directory. There is no routing table and there
  should not be one.
- **The frontmatter is the contract, and the description IS the routing.** Each
  skill declares `verbs`, `requires`, `risk`, `outputs`, `cost`. A skill whose
  `requires` names an empty setting is **not copied into the workspace**, so the
  agent never learns it exists — better than routing to it and refusing after
  the fact, which is the state of every credential on a fresh install. Tests
  assert declared verbs match registered handlers in both directions, and that a
  description does not *prohibit* a verb its own skill declares. That last one is
  not pedantry: `github.close` shipped implemented and documented while the
  description still said "do NOT use it to close anything", and the agent
  correctly refused. **A capability the description denies does not exist.**
- **The agent declares intent; the pipeline performs it.** Nothing credentialed
  runs inside the sandbox. Verbs carry a risk class (`internal` / `tracked` /
  `outward`), `confirm` puts an action in an approval queue, and **approval
  arrives by push, never through the vault browser** — that UI is on loopback,
  which the sandbox shares, so an approve button there could be pressed by a
  prompt-injected agent ([ADR-009](docs/decisions/ADR-009-approval-arrives-out-of-band.md)).
- **Referents are resolved pipeline-side, never by the agent.** Contacts,
  GitHub issues, past recordings, project names — the agent writes the words it
  heard and the pipeline looks them up afterwards, **refusing when they match
  none or several**. Four uses of the same pattern now
  ([ADR-006](docs/decisions/ADR-006-contact-resolution.md),
  [ADR-011](docs/decisions/ADR-011-projects-are-bounded-pre-fetch.md)). Guessing
  is the failure mode worth designing against, because nobody is present to
  disambiguate.
- **Radar is a lead source for the briefing, never a source of record.** A
  separate pipeline on this host (`~/workspace/radar`) collects practitioner
  signals from 14 sources twice a day; the briefing reads its versioned export
  contract (`uv run radar export`) and nothing else — never its store, never a
  collector, never a write. Signals arrive as pipeline-side pre-fetch, fenced as
  untrusted text, bounded, deduplicated against the covered ledger, and
  explicitly **leads not citations**: the briefing chases one to a primary source
  or drops it. Nothing else about the briefing changes, and every Radar failure
  degrades to "no leads today" rather than costing the morning's output.
  [ADR-012](docs/decisions/ADR-012-radar-is-a-lead-source.md).
- **Severity picks the notification channel.** ntfy cannot break through iOS
  Focus — that is their bug and not tunable — so `critical` also books a
  calendar alert, which can. Escalation waits for persistence and has its own
  longer throttle; quiet hours park routine messages for the 07:00 brief rather
  than dropping them. Two multi-day outages in one week were **delivery**
  failures, not detection failures
  ([ADR-010](docs/decisions/ADR-010-severity-decides-the-channel.md)).
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
  `PLAUD_POLL_DAYS` + the ledger make a missed window free. The transport will
  not become hands-off — direct BLE is closed (ADR-005) — so treat this as fixed
  unless one of that ADR's trigger conditions fires.
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
user, a scratch workspace the agent cannot escape, an env allowlist, and no
credentials in scope beyond the vault deploy key — which the agent cannot see.
Every executed prompt is committed, and the agent's stdout is collected beside
its output, so git history is the audit trail for both the instruction and what
was done with it.

**The tool list is NOT deny-by-default, and the docs used to claim it was.**
`ATTICUS_ALLOWED_TOOLS` ships with `Bash` and the permission mode is
`acceptEdits`, which is a deliberate trade — denying tools bought nothing while
the agent had a shell and a network. The transcript is fenced as untrusted data
in the prompt, but fencing is mitigation, not a control: treat arbitrary shell
inside the sandbox as reachable from anything spoken near the pin, and rely on
the namespace rather than the tool list.

**Two secrets remain inside the sandbox boundary** — the agent's auth secret
(with `ATTICUS_CLAUDE_TOKEN_FILE` set, a dedicated long-lived `setup-token`
passed via env, independently revocable; blank, the operator's own credential
file with its refresh token — prefer the former) and, because the network
namespace is shared by default, anything served on loopback. See `SECURITY.md`.
