# Atticus — Voice-to-Agent Pipeline Specification

**Status:** Draft v1
**Last updated:** 2026-07-28
**Owner:** Gregg Reed

---

## 1. Intent

Speak a task into a wearable recorder. Have an autonomous agent execute it and
publish the result. No human interaction between "stop recording" and "read the
output."

Example utterance:

> "Please research the definition of an agentic harness. Do a complete writeup
> and generate an HTML guide with the results."

Expected outcome: an HTML guide committed to a private repo, without touching a
phone, laptop, or terminal.

### 1.1 Non-goals for v1

- Plaud's AI summarization/transcription features. The NotePin S is being used
  as a wearable microphone, not as a note-taking product.
- Real-time / sub-minute latency. Minutes are fine.
- Multi-user support. Single operator.
- Voice output or conversational back-and-forth. One-shot commands only.

### 1.2 Success criteria

| # | Criterion |
|---|-----------|
| S1 | Recording stops → audio lands in the vault repo with zero human action |
| S2 | Short (<30s) command recordings survive the pipeline intact |
| S3 | Transcript is accurate enough to route without correction |
| S4 | Agent output is committed back to the vault repo |
| S5 | Failures are visible, and no recording is silently dropped |
| S6 | Same recording is never processed twice |

---

## 2. Architecture

The controlling insight: **the NotePin S has its own 2.4 GHz Wi-Fi radio and can
upload directly to Plaud Cloud with no phone involved.** Combined with a Plaud
Web fetcher (ADR-002), the iPhone drops out of the design entirely.

```
┌──────────────┐
│  NotePin S   │  record → stop
└──────┬───────┘
       │
       ├─── (A) Wi-Fi, while charging ──────────┐
       │                                        ▼
       └─── (B) BLE ──► Plaud iOS app ──►  Plaud Cloud
                        (auto, no taps)         │
                                                │ poll (web fetcher)
                                                ▼
                                       ┌─────────────────┐
                                       │ WARDOG          │  always-on, light
                                       │                 │
                                       │  ingest         │  detect new recording
                                       │                 │  download original audio
                                       │                 │  commit + push
                                       └────────┬────────┘
                                                ▼
                                    ┌───────────────────────┐
                                    │  atticus-vault (git)  │  ← the queue
                                    │   inbox/              │
                                    └───────────┬───────────┘
                                                │ poll (git pull)
                                                ▼
                                       ┌─────────────────┐
                                       │ FORGE           │  AI server
                                       │                 │
                                       │  transcribe     │  gpt-4o-mini-transcribe
                                       │     ↓           │
                                       │  route          │  transcript → task
                                       │     ↓           │
                                       │  execute        │  claude -p (headless)
                                       │     ↓           │
                                       │  publish        │  commit + push
                                       └────────┬────────┘
                                                ▼
                                    ┌───────────────────────┐
                                    │  atticus-vault (git)  │
                                    │   processed/          │
                                    └───────────────────────┘
```

### 2.0 Host topology

**Git is the queue, not just storage.** The two halves of the pipeline never
talk to each other directly — they communicate only through commits to
`atticus-vault`.

| Host | Role | Needs | Always-on? |
|------|------|-------|------------|
| **WarDog** | Ingest only. Plaud Cloud → vault. | Python 3.11+, Playwright, git, seeded Plaud session | **Yes** — polling requires it |
| **GitHub** | The queue and the durable record | private repo | — |
| **Forge** | Everything downstream of a committed recording | Python 3.11+, `requests`, `claude`, git | 24x7 Fedora, but tolerates downtime |

Consequences worth stating:

- **The Plaud credential lives on WarDog, not Forge.** The seeded browser
  session never touches the AI server.
- **Forge can be offline** without losing recordings. They accumulate in
  `inbox/` and get processed when it returns. This is the main thing the split
  buys.
- **Both hosts push to the same repo.** They write disjoint paths
  (`inbox/` + `.state/` vs `processed/`), so conflicts should be rare, but every
  push must be `pull --rebase` then retry. See §4.3.
- **Two deploy keys**, one per host, both with write access. Revocable
  independently.

> **Assumption A3:** WarDog is always-on and has outbound internet. Ingest
> polling depends on it. If WarDog sleeps, latency becomes "whenever WarDog is
> awake" and the design needs revisiting — see T-24.

### 2.1 Transport options

Both land audio in the same place. The Forge poller is identical either way —
transport is a runtime detail, not a code branch.

| | Trigger | Latency | Phone? | App open? |
|---|---|---|---|---|
| **A. Wi-Fi → Cloud** | Pin placed on charger, idle | Next charge cycle | No | No |
| **B. BLE → App → Cloud** | Proximity to paired phone | Minutes | Yes | No |

**Trap to avoid:** Plaud's "Fast Transfer" (Wi-Fi *to phone*) requires the app
stay in the foreground. Do not enable it. Plain BLE transfer is the hands-off
path.

**Decision:** enable A as primary, leave B enabled as opportunistic. They are
not mutually exclusive — whichever moves the file first wins, and dedupe by
recording ID handles the overlap.

### 2.2 Why no iPhone app in v1

See `docs/decisions/ADR-001-no-iphone-app-v1.md`. Summary: an iOS app is
*possible* via Plaud's official Embedded Device SDK, but it buys nothing v1
needs, costs a Mac (or a painful Mac-less CI path), an Apple Developer
membership, and ongoing maintenance. It is specified here as **W8, contingent**
— triggered only by a proven latency or data-residency requirement.

### 2.2.1 Ingest transport — four paths, one interface

ADR-002 forced a rethink of *how* audio reaches WarDog. Four options, all
feeding the same `plaud_web.py`-shaped contract, so ingest and everything
downstream is unaffected by the choice.

| # | Path | Mac? | Plaud $ | Cloud? | Status |
|---|------|------|---------|--------|--------|
| 1 | **Direct BLE, WarDog → pin** | No | $0 | **No** | Feasible; RSA handshake unresolved |
| 2 | **Android bridge app** | **No** | $0 | No | Official SDK; needs an Android phone |
| 3 | **Plaud Web API** (current) | No | $0 | Yes | Working; auth + list implemented |
| 4 | iOS app | **Yes** | $0 | No | Now the worst option |

**Option 1 is the destination if the handshake opens.** WarDog has a Bluetooth
controller and `bleak` installed. The protocol is fully mapped from Plaud's own
publicly-published Android SDK — see
[`ble-protocol-notes.md`](decisions/ble-protocol-notes.md). It removes Plaud
Cloud, the subscription question, and T-14 in one move.

**Option 2 is the fallback that still avoids a Mac.** The Android `.aar` builds
with Gradle on Linux; no Xcode anywhere. A cheap Android phone becomes a
dedicated bridge. Less work than option 1 because the SDK does the protocol.

**Option 3 stays as the working fallback** regardless — it is the only path
proven to return audio today, and it costs nothing to keep.

**Option 4 is superseded.** It was the only device-direct path when we thought
iOS was the only SDK target. It isn't.

> The `plaud_web.py` contract (§4.1.1) was designed to be transport-agnostic.
> That was luck as much as foresight, but it holds: a BLE implementation
> satisfies the same four commands.

### 2.3 Transcription — OpenAI, not Plaud, not local

**Not Plaud's.** AutoFlow only fires above 200 words. A 10-second command is
~25, so short recordings arrive with *no Plaud transcript at all*. We cannot
depend on it even if we wanted to.

**Not local either** — this reverses an earlier decision. The original plan was
`faster-whisper` on Forge. The machine already runs dictation many times a day
through **hyprwhspr → OpenAI `gpt-4o-mini-transcribe`**, and that transcription
is known-good. Atticus uses the same endpoint and the same capitalization
steering prompt — but a **larger model, `gpt-4o-transcribe`**.

Reasons, in order of weight:

1. **One transcription stack, not two.** A second one would drift, and the
   known-good one is already tuned.
2. **Quality.** These models beat a locally-runnable Whisper on short,
   context-poor clips — exactly our case.
3. **Cost is noise.** Fractions of a cent per recording at these lengths.
4. **No GPU dependency on Forge.**

**Why a bigger model here than for dictation.** Same vendor, same endpoint,
different job:

| | Dictation (hyprwhspr) | Atticus |
|---|---|---|
| Model | `gpt-4o-mini-transcribe` | `gpt-4o-transcribe` |
| Cost/min | $0.003 | $0.006 |
| Latency | **User is watching a cursor** | Nobody is waiting — 5-min timer |
| Error cost | You see the typo and fix it mid-sentence | Silently becomes an agent's instruction |

At ~20 recordings/day that is $11/yr against $22/yr. The asymmetry in error
cost, not the price, is the argument: a dictation mistake is visible and
instantly correctable; an Atticus mistake surfaces twenty minutes later as a
finished document about the wrong topic.

Do **not** "unify" these onto one model later without re-reading this table.

> **The privacy consequence, stated plainly.** Audio goes to OpenAI. If T-14
> concludes nothing may leave the network, this decision reverses *and* so does
> the existing dictation workflow — the exposure is not new, but it is real, and
> it now spans two vendors alongside Plaud. The fallback is `faster-whisper` on
> Forge; `~/tools/whisper.cpp` is already built on WarDog but holds only test
> models.

The API key is read from `~/.config/ai/env` at runtime — the machine's single
credential source — and never written into this repo. See the
`hyprwhspr-doctor` skill for that convention.

**The gate this creates.** `gpt-4o-mini-transcribe` returns plain text with no
confidence signal; `verbose_json` with `no_speech_prob` is `whisper-1` only. So
the "is this transcript trustworthy" check is heuristic — word count and an
optional wake phrase — rather than model confidence. See §4.2.

## 3. Repository layout

Two repos. Code and bulk binary data do not belong in the same history.

### 3.1 `beekeeper-lab/atticus` — this repo

```
atticus/
├── CLAUDE.md                  project context for agent sessions
├── README.md
├── docs/
│   ├── SPEC.md                ← this file, source of truth
│   ├── spec.html              readable rendering of the spec
│   └── decisions/             ADRs
├── ingest/                    Plaud → vault poller
├── processor/                 transcribe → route → execute
├── ops/                       systemd units, env templates, install scripts
└── ios/                       contingent W8 app (empty until triggered)
```

**On the iOS sub-repo question:** keep `ios/` as a plain directory in this repo,
not a submodule. A submodule buys independent versioning we do not need and
costs a detached-HEAD footgun on every clone. If the app ever ships
independently, split it then — that is a cheap operation and an expensive
premature one.

### 3.2 `beekeeper-lab/atticus-vault` — data repo (**must be private**)

```
atticus-vault/
├── inbox/2026/07/
│   ├── 2026-07-28T142211Z_<plaud_id>.mp3
│   └── 2026-07-28T142211Z_<plaud_id>.json     metadata + status
├── processed/2026/07/
│   ├── 2026-07-28T142211Z_<plaud_id>.transcript.txt
│   ├── 2026-07-28T142211Z_<plaud_id>.task.md
│   └── 2026-07-28T142211Z_<plaud_id>/         agent output dir
│       └── agentic-harness-guide.html
├── failures/2026/07/
│   └── 2026-07-28T142211Z_<plaud_id>.error.json
└── .state/
    └── seen.jsonl              append-only ledger of processed Plaud IDs
```

Audio stays in the vault permanently. Git is the durability story; no separate
backup tier for v1.

> **The vault must be private, and this is the one repo setting that is not a
> preference.** It accumulates every recording the pin makes — ambient audio
> from a wearable, including whatever was said near it that was never meant for
> Atticus. Every other repo under `beekeeper-lab` is currently public, so the
> default is wrong here. Verify with `gh repo view beekeeper-lab/atticus-vault
> --json visibility` after creating it.
>
> The code repo can be public; there are no credentials in it, and `ops/.env`
> plus `docs/recon/` are gitignored.

> **Assumption A2:** Volume is low enough that plain git (no LFS) is fine.
> A 1-minute MP3 at 64 kbps is ~500 KB. At 10/day that is ~1.8 GB/year. Acceptable.
> Revisit if usage grows or long-form recordings become common.

### 3.3 Metadata schema (`inbox/**/*.json`)

```json
{
  "plaud_id": "abc123",
  "source": "plaud-notepin-s",
  "recorded_at": "2026-07-28T14:22:11Z",
  "ingested_at": "2026-07-28T14:26:03Z",
  "transport": "wifi-cloud",
  "duration_seconds": 47,
  "audio_path": "inbox/2026/07/2026-07-28T142211Z_abc123.mp3",
  "audio_sha256": "…",
  "status": "raw",
  "attempts": 0
}
```

`status` transitions: `raw` → `transcribed` → `routed` → `executed` → `published`,
or → `failed` at any point. Status lives in the metadata file so the pipeline is
resumable and idempotent after a crash.

---

## 4. Components

### 4.1 `ingest/` — Plaud → vault

**Runs on WarDog** on a systemd timer (every 2 minutes).

1. `plaud_web.py list --days 2 --json` → candidate list
2. Filter against `.state/seen.jsonl`
3. For each new ID: `plaud_web.py audio <id>` → time-limited download URL
4. Download to `inbox/YYYY/MM/`, verify size, compute SHA-256
5. Write metadata JSON with `status: raw`
6. Append ID to `seen.jsonl`, commit, push

Design notes:

- **Poll, don't push.** No webhook is documented for personal Plaud accounts.
  Webhooks appear only in the enterprise workflow product. 2-minute polling is
  cheap and the latency floor is set by device sync anyway.
- **Ledger before commit, commit before processing.** The recording is durable
  in git before any work is attempted on it.
- Download URLs are time-limited — fetch immediately, never persist the URL.
- Language: **Python 3.11+** throughout. The fetcher is a module in this repo,
  not an external binary. No Node dependency at all now that the CLI is gone.

### 4.1.1 `plaud_web.py` — the fetcher

> ✅ **Mostly resolved.** T-06 recon ran on 2026-07-28 against the unbound free
> account — the web app seeds three demo files, which was enough to observe
> auth, the list endpoint, and the record schema. **The audio download path is
> the one thing still unknown** (T-06b), because there was no exportable
> recording to trigger it.

#### Committed — the contract ingest depends on

This is stable regardless of what recon finds, because it is our design, not
Plaud's. Deliberately CLI-shaped, so the earlier `plaud`-based ingest sketch
still reads correctly.

```
plaud_web.py login                    seed the session (interactive, once)
plaud_web.py whoami [--json]          session health check
plaud_web.py list [--days N] [--json] recordings, newest first
plaud_web.py audio <id> -o <path>     download original audio
```

`list --json` emits exactly this, whatever the upstream field names turn out
to be — normalizing here means no other component learns Plaud's vocabulary:

```json
{
  "recordings": [
    {
      "id": "…",
      "name": "…",
      "created_at": "2026-07-28T14:22:11Z",
      "duration_seconds": 47
    }
  ]
}
```

**Exit codes** — the part that makes silent failure tractable:

| Code | Meaning | Ingest's response |
|------|---------|-------------------|
| 0 | Success | proceed |
| 2 | Usage error | bug; alarm |
| 3 | **Session expired / auth failed** | alarm loudly — needs re-seeding |
| 4 | Network or transient upstream error | retry next tick, quiet |
| 5 | Unexpected — likely upstream changed | alarm; re-run T-06 recon |

Code 3 exists because it is the difference between "no new recordings" and
"login broken," and those look identical from the outside. Without it, R1's
failure mode is a pipeline that goes quiet and nobody notices for a week.

#### Observed — the endpoint layer

| # | Question | Answer from recon |
|---|----------|-------------------|
| E1 | Cookie or bearer? | **Bearer.** `Authorization: Bearer <workspace_token>` |
| E2 | `requests` or browser? | **Playwright `ctx.request`** — `requests` is not in the fetchers venv, and the browser context shares cookies and TLS fingerprint anyway |
| E3 | Pagination? | **`skip` / `limit`**, with `data_file_total` for the bound |
| E4 | Download shape? | ❓ **Unobserved** — see T-06b |
| E5 | Server-side prep needed? | ⚠️ **Probably yes.** Every record carries `ori_ready`, and it was `false` on all three demo files |
| E6 | Are short recordings listed normally? | ❓ Needs a real recording — this is Q5 |

**Auth flow.** The web app does a three-step dance: `POST /auth/access-token` →
`GET /team-app/workspaces/list` → `POST /user-app/auth/workspace/token/<ws_id>`.
Rather than reimplement it, the fetcher **harvests the `Authorization` header
off a live request** after loading the app.

That has a useful consequence: the workspace token lives 24h but its refresh
token lives 30 days, and the *browser* performs the refresh. So the practical
session lifetime is **~30 days, not 24 hours** — the fetcher never touches
token renewal. This is most of Q2's answer, ahead of schedule.

**Endpoints in use:**

```
GET  /user/me                 → data_user{email,…}, data_state{is_bind,…}
GET  /file/simple/web         ?skip&limit&is_trash=0&sort_by=start_time&is_desc=true
                              → {status, data_file_total, data_file_list[]}
```

**Record schema** (Plaud's names stop at `_normalize`; nothing downstream sees
them):

| Plaud | Ours | Note |
|-------|------|------|
| `id` | `id` | hex string |
| `filename` | `name` | may be empty |
| `start_time` | `created_at` | **ms** epoch → ISO-8601 UTC, second precision |
| `duration` | `duration_seconds` | **ms**, not seconds |
| `ori_ready` | `ori_ready` | original audio prepared? — carried through because E5 depends on it |
| `filetype` | `filetype` | e.g. `audio/mp3` |
| `file_md5` | `md5` | integrity check on download |

Note the two millisecond traps: `duration` and `start_time` are both ms, and
naively treating either as seconds produces plausible-looking garbage.

**Application errors arrive as HTTP 200** with a non-zero `status` field, so the
transport layer checks the body, not just the status line.

#### T-06b — the remaining unknown

The download path was never triggered because the account has no exportable
recording. `ori_ready: false` on every demo file is the concrete form of E5:
the original audio is not necessarily sitting there ready to fetch.

On arrival day, re-run recon against a real recording and **export its original
audio**, watching for: the call that flips `ori_ready`, any polling the app
does, and whether the final URL is a presigned CDN link or a stream from
`api.plaud.ai`.

`audio_url()` raises rather than guessing. A fetcher that downloads a
not-ready object yields truncated files that look successful to the ledger —
the worst available failure mode, since the recording is then marked seen and
never retried.

#### Deployment shape

Session seeding is interactive; WarDog is headless. Same pattern the CLI's
`tokens.json` would have used:

```
desktop:  plaud_web.py login          → ~/.local/share/claude-fetchers/sessions/plaud/
          scp -r … wardog:…
wardog:   plaud_web.py whoami         → confirms headless reuse (T-22)
```

Playwright on WarDog needs `chromium` plus its system deps — heavier than the
Node CLI would have been, and worth confirming WarDog can carry it (T-26).

> **Risk R1:** the fetcher depends on Plaud's web app, which can change
> without notice. Mitigations: authenticate via Playwright but call the JSON
> API rather than scraping the DOM (a narrower, more stable target); keep the
> T-06 discovery report so a break can be diagnosed by re-running recon; and
> treat T-72's session health check as load-bearing. **The failure is loud at
> the fetcher and silent everywhere else** — nothing downstream knows the
> difference between "no new recordings" and "login broken."

### 4.2 `processor/` — transcribe → route → execute

**Runs on Forge** on a systemd timer (every 2 minutes).

Forge learns of new work by **polling git**, not by webhook:

```
git pull --rebase  →  scan inbox/ for status: raw  →  work  →  commit  →  push
```

Alternatives considered and rejected for v1: a GitHub webhook to a Forge
endpoint (requires exposing Forge inbound — a real attack surface for a box
that runs an autonomous agent), and `repository_dispatch` via Actions (same
exposure, plus a moving part). Polling costs a `git pull` every two minutes and
needs no inbound firewall rule. Revisit only if latency becomes the complaint.

1. Scan `inbox/` for `status: raw`
2. OpenAI `gpt-4o-mini-transcribe` → `processed/**/*.transcript.txt`, status `transcribed`
3. Route: transcript → task file, status `routed`
4. Execute: `claude -p` headless in the vault working dir, status `executed`
5. Commit outputs, status `published`

**Routing (v1 is deliberately dumb):** the entire transcript becomes the prompt.
No wake words, no grammar, no intent classification. A preamble is prepended
giving the agent its output contract (write results into the recording's output
directory, prefer HTML per the house standard).

Structured routing — keyword prefixes like "research…", "draft…", "remind me…"
mapping to different handlers — is a v2 concern. Build it when a real utterance
demands it, not before.

**Safety:** the agent executes text derived from audio. Anything within earshot
of the pin can, in principle, become an instruction. Mitigations for v1:

- Run the processor as an unprivileged user, scoped to the vault directory
- No credentials in the processor's environment beyond the vault deploy key
- Deny-by-default tool permissions; no destructive Bash
- Every executed prompt is committed to the vault, so the audit trail is the
  git history

This is a real surface, not a theoretical one. Keep the blast radius small.

### 4.3 `ops/` — deployment

Two deploy targets from one repo. `install.sh` takes a role:

```
./ops/install.sh wardog     # timer + ingest + Node/CLI checks
./ops/install.sh forge      # timer + processor + whisper/claude checks
```

systemd units: `atticus-ingest.timer` (WarDog), `atticus-processor.timer`
(Forge). No containers for v1 — both are single trusted hosts, and a container
adds a layer without removing a problem.

**Concurrent-push handling.** Both hosts commit to `atticus-vault`. Every push
is wrapped:

```
git pull --rebase --autostash  →  push  →  on failure, retry (bounded)
```

They write disjoint paths — WarDog owns `inbox/` and `.state/`, Forge owns
`processed/` and `failures/` — so a rebase should apply cleanly. The one file
both touch is a recording's metadata JSON, when Forge advances `status` on a
record WarDog created. Forge only ever edits records already committed, so the
rebase is a fast-forward in practice. Retry bounded at 3, then quarantine and
notify rather than loop.

### 4.4 `ios/` — contingent

Empty until W8 triggers. If built: Swift, Plaud Embedded iOS SDK,
`PlaudDeviceAgent.connectBleDevice / getFileList / exportAudio`, uploading to a
Forge endpoint rather than Plaud Cloud.

---

## 5. Task breakdown

**Owner key:** **C** = Claude (me, in this repo) · **G** = Gregg (physical
device, accounts, credentials, judgment calls) · **CG** = paired

**Mac column:** ✓ means the task cannot be done on Linux.

**Host column:** where the work lands. `—` means it is not host-specific.

### W0 — Foundation

| ID | Task | Owner | Host | Mac | Depends on |
|----|------|-------|------|-----|------------|
| T-01 | Project structure, CLAUDE.md, README, this spec | C | — | — | — |
| T-02 | ADR-001 (no iPhone app in v1) | C | — | — | T-01 |
| T-03 | Create `beekeeper-lab/atticus` (public ok) + `beekeeper-lab/atticus-vault` (**private, non-negotiable**) | G | — | — | — |
| T-04 | Push repo; add **two** deploy keys to vault (WarDog, Forge) | G | — | — | T-01, T-03 |

### W1 — Device enablement *(blocked on hardware arrival)*

| ID | Task | Owner | Host | Mac | Depends on |
|----|------|-------|------|-----|------------|
| T-10 | Unbox, pair NotePin S to Plaud iOS app, update firmware | G | phone | — | — |
| T-11 | Configure 2.4 GHz Wi-Fi on the pin | G | phone | — | T-10 |
| T-12 | Enable "Sync to cloud while charging" | G | phone | — | T-11 |
| T-13 | Confirm "Fast Transfer" (Wi-Fi to phone) is **off** | G | phone | — | T-10 |
| T-14 | Decide: is Plaud Cloud acceptable for the audio you'll speak? | G | — | — | — |

T-14 is a judgment call, not a technical step, and it gates everything
downstream. Both zero-touch transports route audio through Plaud's servers.
If that is unacceptable for the content you intend to record, v1 as specified
is the wrong design and W8 becomes mandatory rather than contingent.

### W2 — Plaud access layer

| ID | Task | Owner | Host | Mac | Depends on |
|----|------|-------|------|-----|------------|
| T-21 | Confirm Forge specs; pick whisper model size | CG | Forge | — | — |
| T-24 | Confirm WarDog is always-on with outbound internet (A3) | G | WarDog | — | — |
| T-26 | Confirm WarDog can run Playwright + chromium headless | G | WarDog | — | — |
| T-08 | Fetcher contract + skeleton (`plaud_web.py`), endpoints stubbed | C | WarDog | — | — |
| T-27 | Map the BLE protocol from Plaud's public Android SDK | C | — | — | — |
| T-28 | **GATE (arrival, do FIRST):** `ble_scan.py` — will an *unbound* pin talk to WarDog? | G | WarDog | — | T-hw |
| T-29 | Register Plaud Developer Portal; does a self-serve account get an RSA key? | G | — | — | — |
| T-25 | ~~Import a test MP3 to validate before hardware~~ **Blocked** — Plaud requires a bound device before audio import | G | desktop | — | T-10 |
| T-06 | ~~**GATE:** run `plaud_discover.py`~~ **Done 2026-07-28** — auth, list endpoint and record schema captured off the demo files | G | desktop | — | — |
| T-06b | **GATE:** re-run recon against a real recording; export original audio to capture the download path | G | desktop | — | T-10 |
| T-07 | ~~Fill in `PlaudAPI`~~ **Partly done** — auth, list, whoami implemented and unit-tested | C | WarDog | — | T-06 |
| T-07b | Implement `audio_url()` / download from the T-06b report | C | WarDog | — | T-06b |
| T-22 | **GATE:** session portability — seed on desktop, run headless on WarDog | G | WarDog | — | T-07b |
| T-23 | Document the fetcher's API contract + session lifetime | C | — | — | T-07b, T-22 |

**T-25 was the plan to de-risk before hardware, and it failed.** Plaud will not
accept an audio import until a device is bound to the account, so the free tier
cannot be exercised at all until the pin arrives. The consequence: **the entire
fetcher premise is unvalidated until arrival day**, and Q1/Q2 — which the dev
account was supposed to have pulled forward — go back on the critical path.

What survives: the fetcher's *contract* is committed and testable today (T-08),
so arrival day is filling in four methods rather than designing from scratch.

**T-06 detail.** `ingest/plaud_discover.py` watches a logged-in Plaud Web
session and reports which endpoints back the recording list and the audio
download. Secrets are redacted before anything is written. Its output is the
input to T-07 — and keeping it is how we repair the fetcher if Plaud changes
their web app.

**T-22 detail.** Session seeding requires a browser; WarDog is headless. Plan:
seed on a desktop, copy `~/.local/share/claude-fetchers/sessions/plaud/` to
**WarDog** (not Forge — the Plaud credential never touches the AI server), then
confirm `plaud_web.py whoami --headless` works there. Same operational shape as
the `tokens.json` plan this replaces. **Session expiry is a silent multi-day
outage**, which is what T-72 exists to catch.

### W3 — Arrival tests *(the measurements that settle open questions)*

| ID | Task | Owner | Host | Mac | Depends on |
|----|------|-------|------|-----|------------|
| T-30 | Record 15s command; confirm it appears in `plaud files` at all | G | WarDog | — | T-12, T-20 |
| T-31 | Confirm original audio downloads and is playable (MP3/WAV, bitrate) | G | WarDog | — | T-30 |
| T-32 | Confirm short recordings download **without** a Plaud transcript | G | WarDog | — | T-30 |
| T-33 | Time Wi-Fi-while-charging sync: stop → visible in cloud | G | — | — | T-12 |
| T-34 | Time BLE sync with app **backgrounded**, then **force-quit** | G | phone | — | T-10 |
| T-35 | Record results into `docs/arrival-tests.md` | CG | — | — | T-30…T-34 |

T-34 is the one genuine unknown in the whole design. Plaud documents that BLE
transfer starts automatically once connected, but whether iOS keeps it alive
when the app is suspended is untested and undocumented. If it works, transport B
becomes the low-latency default. If it doesn't, Wi-Fi-while-charging is the only
zero-touch path and latency is bounded by your charging habits.

### W4 — Ingest *(WarDog)*

| ID | Task | Owner | Host | Mac | Depends on |
|----|------|-------|------|-----|------------|
| T-40 | `ingest/` poller: list → dedupe → download → commit | C | WarDog | — | T-07b, T-23 |
| T-41 | Ledger + idempotency + resumable status transitions | C | WarDog | — | T-40 |
| T-42 | Structured logging + failure quarantine to `failures/` | C | WarDog | — | T-40 |
| T-43 | systemd timer unit (2 min) | C | WarDog | — | T-40 |
| T-45 | ~~Safe-push wrapper~~ **Done** — `vault.Git.commit_push` | C | both | — | — |
| T-44 | Deploy to WarDog; verify a recording reaches GitHub | CG | WarDog | — | T-43, T-04, T-31 |

### W5 — Transcription *(Forge)*

| ID | Task | Owner | Host | Mac | Depends on |
|----|------|-------|------|-----|------------|
| T-50 | ~~Install `faster-whisper`~~ **Moot** — using the OpenAI API (§2.3) | — | — | — | — |
| T-53 | ~~Git-poll loop~~ **Done** — `processor/pipeline.py` | C | Forge | — | — |
| T-51 | ~~Transcription stage~~ **Done** — `processor/transcribe.py` | C | Forge | — | — |
| T-52 | Accuracy check against T-30 recordings; tune model size | CG | Forge | — | T-51, T-35 |

### W6 — Routing and execution *(Forge)*

| ID | Task | Owner | Host | Mac | Depends on |
|----|------|-------|------|-----|------------|
| T-60 | ~~Prompt preamble + output contract~~ **Done** — `processor/execute.py` | C | Forge | — | — |
| T-61 | ~~`claude -p` headless invocation~~ **Done** — scratch workspace, no git in env | C | Forge | — | — |
| T-62 | Sandboxing: unprivileged user, restricted tools | C | Forge | — | T-61 |
| T-63 | ~~Commit outputs back to vault~~ **Done** — `processor/vault.py` | C | Forge | — | — |
| T-64 | **Milestone: full loop.** Speak the agentic-harness prompt, get HTML | CG | both | — | T-44, T-52, T-63 |

### W7 — Operations

| ID | Task | Owner | Host | Mac | Depends on |
|----|------|-------|------|-----|------------|
| T-70 | Failure notification (ntfy/email/Pushover — your call) | C | both | — | T-42 |
| T-71 | `ops/install.sh <role>` **done**; `.env.example` done; runbook pending | C | both | — | — |
| T-72 | Token-expiry watchdog (surfaces T-22's failure mode early) | C | WarDog | — | T-22 |
| T-74 | Backlog alarm: `inbox/` items stuck in `raw` past a threshold | C | Forge | — | T-53 |
| T-73 | Retention/rotation policy decision for vault audio | G | — | — | T-64 |

**T-74 rationale.** The split's benefit — Forge can be offline and work waits —
is also its failure mode: a dead Forge looks exactly like an idle one. Nothing
errors, recordings just quietly pile up. An alarm on inbox age is what makes the
queue safe to rely on.

### W9 — Skills

**The organising rule: project skills are *intents*, global skills are
*capabilities*.** If it maps to something spoken aloud, it lives in
`atticus/skills/` and ships with this repo. If it is a house standard or a
shared integration, it lives in Forge's `~/.claude/skills/` — otherwise every
intent skill re-specifies it and they drift apart.

#### Project skills — voice-command intents

| ID | Skill | Trigger | Owner | Depends on |
|----|-------|---------|-------|------------|
| T-90 | ~~`deep-research`~~ **Done** | "research X", "compare Y and Z" | C | — |
| T-91 | `price-scout` — purchase research, Amazon-weighted, HTML comparison | "find me a…", "best price on…" | C | T-95 |
| T-92 | `idea-to-spec` — paragraph → requirements, design, implementation plan; contrast approaches when asked | "app idea:", "I want to build…" | C | T-95 |
| T-93 | `capture-task` — todo / calendar / ticket capture | "remind me", "add a ticket", "put on my calendar" | C | T-97, T-98 |
| T-94 | `meeting-note` — clean up and summarise a long ambient recording | see the wake-phrase caveat below | C | T-99 |

#### Global skills on Forge — capabilities

| ID | Skill | Action | Owner | Note |
|----|-------|--------|-------|------|
| T-95 | `html-artifact-output` | **Sync from WarDog** | G | Every intent skill depends on it. Nothing else in W9 works properly until this lands. |
| T-96 | `dataviz` | **Sync from WarDog** | G | Comparison tables and charts in reports. Optional but cheap. |
| T-97 | `ado-integration` | **New** | CG | Azure DevOps work-item creation. Needs a PAT on Forge — first credential the agent will hold. |
| T-98 | Microsoft 365 / Outlook | **Decide: MCP or skill** | CG | An M365 MCP connector already exists in the account. Probably wire that up rather than write a skill. |

#### Do NOT sync to Forge

| Skill | Why not |
|-------|---------|
| `image-asset-generation` | Enforces a hard "wait for explicit user greenlight before spending" gate. **In an unattended pipeline there is nobody to approve**, so the run either stalls until timeout or the agent violates the skill's own rule. |
| `audio-asset-generation` | Same approval gate, same problem. |

That is a real trap rather than a theoretical one: both skills are written to
block on human confirmation, which is correct interactively and wrong for a
5-minute timer. If image generation is ever wanted in a report, it needs a
variant with a pre-authorised budget instead.

#### Open design question — T-99

The wake phrase must appear at the **start** of the transcript. That works for
"Atticus, research X" but not for the natural way to handle a long recording:
let it run, then say "Atticus, summarise that" at the *end*. The current gate
rejects it, so **`meeting-note` (T-94) is blocked until this is resolved.**

Options: also check the tail of the transcript; support a distinct
"end-marker" phrase; or route long unmarked recordings to `meeting-note`
automatically. Not urgent — decide once there is a real recording to test
against.

#### Deliberately not building yet

`decision-brief` ("should I use X or Y") overlaps `deep-research` heavily —
research already takes a position. Adding it risks the model picking randomly
between two similar descriptions. Revisit only if real usage shows research
answering choice-shaped questions badly.

### W8 — iOS app *(CONTINGENT — do not start unopposed)*

Trigger conditions — start only if **T-14 rules out Plaud Cloud**, or **T-33 and
T-34 both show unacceptable latency.**

| ID | Task | Owner | Host | Mac | Depends on |
|----|------|-------|------|-----|------------|
| T-80 | Register Plaud Developer Portal app; get client creds + API key | G | — | — | trigger |
| T-81 | Apple Developer Program membership ($99/yr) | G | — | — | trigger |
| T-82 | Swift app skeleton + Plaud Embedded SDK integration | C | — | — | T-80 |
| T-83 | BLE pair, `getFileList`, `exportAudio`, background transfer | C | — | — | T-82 |
| T-84 | Upload endpoint on **WarDog** + app-side client | C | WarDog | — | T-83 |
| T-85 | **Unbind pin from official Plaud app** (one app at a time) | G | phone | — | T-83 |
| T-86 | Build + sign | C | CI | ✓* | T-82, T-81 |
| T-87 | Install to device via TestFlight | G | — | ✓* | T-86 |
| T-88 | On-device BLE debugging | G | — | ✓ | T-87 |

Note T-84: under W8 the app uploads to **WarDog**, which already owns ingest
and already holds a vault deploy key. Forge stays out of the ingest path in
every scenario.

---

## 6. The Mac question

**v1 needs no Mac.** Everything through T-73 runs on Linux. Node, Python,
`requests`, git, systemd — Forge handles all of it. The Mac only enters
via W8, which is contingent and probably never triggers.

If W8 *does* trigger, a Mac is still avoidable, with friction:

| Need | Mac-free path | Cost |
|------|---------------|------|
| Build + sign | GitHub Actions `macos-latest` runner | Free tier, or ~$0.08/min |
| Certs + profiles | `fastlane match` + App Store Connect API key | Setup complexity |
| Install to iPhone | TestFlight | $99/yr Apple Developer |
| Iterate on BLE bugs | — | **This is where it breaks down** |

Build-and-ship is genuinely Mac-free. **Debugging is not.** BLE work is
empirical — you will be watching connection state, retry behavior, and
background-suspension edge cases. Doing that through 10-minute CI round-trips
with no debugger, no Xcode console, and no Instruments is miserable enough that
it will dominate the schedule.

**Recommendation:** don't buy a Mac now. If W8 triggers, borrow or rent one
(MacStadium, ~$100/mo) for the BLE development window, then drop it and keep
shipping via Actions. Marked ✓* in the table above for exactly this reason:
technically avoidable, practically not worth avoiding.

---

## 7. Which Plaud product surface

**Answer: none of them. We use a Plaud Web fetcher.** See
[ADR-002](decisions/ADR-002-plaud-web-fetcher.md).

Plaud ships three developer surfaces. All three are wrong for this project, for
different reasons.

| Surface | What it reads | Why not |
|---------|---------------|---------|
| **CLI** `@plaud-ai/cli` | Your personal account | **Paywalled.** Requires a paid plan. |
| **MCP** `@plaud-ai/mcp` | Same account, same data | Paywalled the same way, and a cron job has no use for a conversational client. |
| **Embedded** | Nothing of yours: audio *you* upload, plus BLE pairing | Free, but it is B2B infrastructure for building your own Plaud-powered app. No "list my recordings" endpoint. W8 only. |

### What we use instead

`ingest/plaud_web.py` — Playwright with a persistent session, credentials from
1Password, calling Plaud's JSON API with the session the browser established.
Follows the house pattern in `~/.claude/site-fetchers/`.

```
plaud_web.py login              seed the session (interactive, once)
plaud_web.py list --json        recordings since N days
plaud_web.py audio <id> -o …    download original MP3/WAV
plaud_web.py whoami             session health check
```

Same interface the CLI would have offered, so nothing downstream of ingest
changes. Cost: $0/yr.

### Why the free tier is sufficient

| | Starter (free) | Paid |
|---|---|---|
| Original audio export (MP3/WAV) | ✅ | ✅ |
| Wi-Fi sync to cloud | ✅ | ✅ |
| Web/app access to recordings | ✅ | ✅ |
| Transcription minutes | 300/mo | more |
| CLI / MCP | ❌ | ✅ |

Two reasons the transcription allowance is irrelevant: we transcribe locally
via the OpenAI API, and AutoFlow only fires above 200 words — so a
15-second command consumes **zero** minutes. The free tier is not a compromise
for this workload.

### The tradeoff we accepted

The fetcher breaks if Plaud reworks their web app. That is the same bet the
other five fetchers in the registry already make. It is why T-72's session
health check is load-bearing rather than nice-to-have, and why the discovery
report (T-06) is kept — re-running it is how we repair a break.

### Why Embedded is not it

The name suggests it is the "real" API. It is not — it is B2B infrastructure
for building your own Plaud-powered product, and it gives you two things:

- **Device SDK** — your app pairs with NotePin S hardware over BLE
- **Transcription API** — you upload audio, Plaud transcribes it

Neither touches your personal account. There is no "list my recordings"
endpoint, because Embedded assumes the recordings belong to *your users*,
captured through *your app*. Wrong shape for "Forge reads Gregg's pin."

It enters only under W8 — where we would use the Device SDK for BLE and still
skip the Transcription API, since Forge already transcribes via OpenAI. Note
the absurdity being avoided in v1: using Embedded's transcription would mean
uploading audio to Plaud that we just downloaded from Plaud.

### Community packages

**Avoid** `sergivalverde/plaud-toolkit` and the rest. Reverse-engineered from
the web app, alpha, and they want your email and password in plaintext.

Our fetcher targets the same underlying API, so the distinction is worth being
precise about: we authenticate through a real browser session held in a
1Password-backed Playwright profile, and **never store the account password**.
Same access, none of the credential handling that made the toolkits a bad idea.

### Free fallback worth remembering

The web API exposes Plaud's transcript when one exists. Short
commands will not have one (AutoFlow's 200-word floor), but meeting-length
recordings arrive already transcribed at no cost. A "use Plaud's if present,
whisper otherwise" fallback is cheap. Not v1 — noted so it is not rediscovered.

---

## 8. Open questions

| # | Question | Resolved by | Blocks |
|---|----------|-------------|--------|
| Q1 | ~~Does `--json` exist on the CLI?~~ **Moot** — CLI is paywalled (ADR-002) | — | — |
| Q2 | Does the browser session survive copy to WarDog and work headless? | T-22 | W4 |
| Q12 | Does `ori_ready` gate the download, and what flips it? (E5) | T-06b | **fetcher correctness** |
| Q9 | ~~What is the web API's actual shape?~~ **Answered** except the download path | T-06 | — |
| Q10 | Does audio export return an existing object, or start a server-side job? (E5) | T-06b | fetcher design |
| Q3 | Does BLE sync complete with the app suspended? | T-34 | transport choice |
| Q4 | Wi-Fi-while-charging latency? | T-33 | expectations |
| Q5 | Do sub-200-word recordings sync at all, and is raw audio retrievable with no transcript? | T-30, T-32 | **entire design** |
| Q6 | Forge CPU/GPU — which whisper model? | T-21 | W5 |
| Q7 | Is Plaud Cloud acceptable for this audio? | T-14 | W8 trigger |
| Q8 | Is WarDog always-on with outbound internet? (A3) | T-24 | ingest latency |
| Q11 | Can WarDog run Playwright + headless chromium? | T-26 | W4 |
| Q13 | Will an unbound pin accept a BLE connection from Linux? | T-28 | **transport choice** |
| Q14 | Is the handshake RSA key issued self-serve? | T-29 | **transport choice** |

**Q5 is the existential one.** The whole design assumes a 15-second utterance is
a first-class recording that syncs and exposes its original audio. Plaud's
product is built around meeting-length content, and AutoFlow's 200-word floor
proves they think in those terms. If short clips are discarded, deprioritized in
sync, or have their audio withheld pending transcription, v1 needs rethinking.
**Test this first, on day one, before anything else is built.**

---

## 9. Sequencing

```
NOW (genuinely hardware-free):
  T-01 ─ T-02 ─┬─ T-03 ─ T-04
               └─ spec review
  T-08 ─ T-06 ─ T-07  ◄── DONE: contract, recon, auth + list implemented
  T-24 ─ T-26  ◄── A3. WarDog always-on? Can it run Playwright?
  T-27         ◄── DONE: BLE protocol mapped from the public SDK
  T-29         ◄── Q14. Portal signup — does it hand out an RSA key?
  T-21         ◄── Q6. Forge specs.
  T-14         ◄── Q7. Decides whether W8 is mandatory.

ARRIVAL DAY (tightly ordered — each gate can stop the next):
  T-28                         ◄── Q13. BLE scan BEFORE binding to the app.
  T-10 ─ T-11 ─ T-12 ─ T-13    pair, wi-fi, cloud sync on, fast-transfer off
  T-30 ─ T-32                  ◄── Q5. Do short recordings exist and export?
  T-06b                        ◄── recon #2: EXPORT AUDIO. Only unknown left.
  T-07b ─ T-22                 ◄── implement download; prove headless on WarDog
  T-31 ─ T-33 ─ T-34 ─ T-35

THEN (I build, largely unattended):
  W4 (WarDog) ──► GitHub ──► W5 ─ W6 (Forge) ──► T-64 full loop
  W7 hardening

LATER / PROBABLY NEVER:
  W8
```

**Most of the head start was recovered.** The paywall looked like it would push
every Plaud unknown to arrival day. It didn't: the web app seeds three demo
files into a new account, and that was enough to observe the auth flow, the list
endpoint, and the record schema without a device. Auth, listing and `whoami` are
implemented and unit-tested against real captured payloads.

**One unknown remains: the audio download path** (T-06b, Q10, Q12). It could not
be observed because the account has nothing exportable — and `ori_ready: false`
on every demo file suggests it is not a simple GET. That is the first thing to
settle on arrival day, immediately after Q5.

W4 and W5/W6 remain independently deployable because git separates them. Ingest
can run on WarDog for days, accumulating recordings, before Forge exists.

---

## 10. References

- [Plaud Developer Platform](https://docs.plaud.ai/overview)
- [Plaud CLI](https://docs.plaud.ai/plaud-mcp-cli/cli.md)
- [Plaud MCP](https://docs.plaud.ai/plaud-mcp-cli/mcp.md)
- [Plaud Embedded — iOS SDK](https://docs.plaud.ai/plaud-embedded/ios-sdk.md)
- [Plaud Embedded — supported devices](https://docs.plaud.ai/plaud-embedded/devices.md)
- [Plaud Embedded — quickstart](https://docs.plaud.ai/plaud-embedded/quickstart.md)
- [Sync to cloud while charging](https://support.plaud.ai/hc/en-us/articles/11681029415439-How-to-enable-Sync-to-cloud-while-charging-transfer)
- [Private Cloud Sync](https://support.plaud.ai/hc/en-us/articles/51820671018265-Private-Cloud-Sync)
- [AutoFlow (200-word threshold)](https://support.plaud.ai/hc/en-us/articles/51885855749785-AutoFlow)
- [Transfer Files](https://support.plaud.ai/hc/en-us/articles/53640104184985-Transfer-Files)
