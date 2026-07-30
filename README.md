# Atticus

**Speak a task into a wearable recorder. Walk away. Get a finished document.**

> *"Atticus, research the top five AI security vulnerabilities. Give me a few
> paragraphs on each — what it is, how it's done, how to defend against it.
> This is the start of research for a blog post."*

Thirty minutes later there is a 72 KB HTML report in a private git repo, indexed
on a private website, and a link to it on your phone. Nobody touched a keyboard.

---

## What this actually is

Not a voice assistant. There is no conversation, nothing answers you, and
nothing waits for you to reply.

Every voice assistant on the market is **synchronous and ephemeral** — you ask,
it answers now, nothing persists. Atticus is the opposite on both axes:
**asynchronous and durable.** You speak, walk away, and later a permanent,
searchable artifact exists.

The closest honest description: *an answering machine in reverse.* You leave the
message, and your agent does the work.

That framing is the whole design. It is **not trying to be fast** — the round
trip is roughly 30 minutes and that is fine, because nothing is waiting on it.
What it optimises for is *never needing you again* after you stop talking.

## How it works

```
┌──────────────┐
│  NotePin S   │  you speak, you stop. that is the entire interaction.
└──────┬───────┘
       │  BLE → phone → vendor cloud
       ▼
┌──────────────┐
│   ingest     │  poll every 15 min, download, commit
└──────┬───────┘
       │  git push
       ▼
┌──────────────────────────┐
│   your vault (private)   │  ← the queue, not just storage
│     inbox/               │
└──────┬───────────────────┘
       │  git pull, every 5 min
       ▼
┌──────────────┐
│  processor   │  transcribe → gate → route → execute → publish
└──────┬───────┘
       │  git push  +  push notification with a link
       ▼
   processed/2026/07/…/report.html   →   a private, searchable website
```

**Git is the boundary.** The two halves never talk directly — every handoff is a
commit. That buys durability (a recording is in version control before anything
processes it), a complete audit trail of every prompt an autonomous agent was
ever given, and independent failure: the processor can be down for a day and the
work simply waits.

## The five ideas worth stealing

**1. Git is the queue, not just storage.** Each recording's `status` field *is*
the pipeline state: `raw → transcribed → routed → executed → published`. Every
stage commits before the next begins, so a crash resumes mid-record instead of
redoing work or double-executing. The two halves need no message broker, no
shared database, and no direct connection — only a repo they both push to.

**2. Routing is the model's job.** `claude -p` runs with a skills directory
mounted and picks a matching skill from its description. **Adding a capability
means adding a directory.** There is no routing table, and there should not be
one.

**3. The transport is a pluggable executable.** Ingest shells out to a fetcher
implementing a four-command CLI (`whoami`, `list`, `audio`, plus an exit-code
contract). Direct BLE, an Android bridge, or a vendor web API all satisfy it, so
the transport can be replaced without touching the pipeline.

**4. A wearable overhears things, so assume the input is hostile.** Only
transcripts beginning with a wake word are executed; everything else is filed as
an unexecuted note. This is load-bearing: of the first fifteen real recordings,
most were correctly *not* executed. The agent also runs inside a mount namespace
with its own `HOME`, where the vault, the SSH keys and the shared credential
file do not exist — so "it cannot touch git" is enforced rather than asserted.
It still has network access; see [`docs/HARDENING.md`](docs/HARDENING.md) for
what is and is not guaranteed.

**5. Every capability ends in the same artifact.** A research request produces an
HTML report. A "file a ticket" skill would file the ticket *and* produce an HTML
record of what it did, with a link. Keeping the output shape constant is what
makes the website a universal index of everything the system has ever done,
regardless of what the underlying action was.

## Status — v1

**Working end to end on real recordings.** Fifteen processed; five executed into
published reports.

| Stage | State |
|---|---|
| Cloud → vault | ✅ 15-minute poll, per-host dedupe ledger, alarms on failure |
| Transcription | ✅ `gpt-4o-transcribe`, length-guarded |
| Wake-phrase gate | ✅ holding; most recordings correctly not executed |
| Agent execution | ✅ produces self-contained HTML reports |
| Publish + notify | ✅ private searchable site, push notification with a link |
| Device → cloud | ⚠️ **needs the vendor app foregrounded** — the one unsolved link |

Measured on a real run: **13 minutes** of pipeline time (ingest → transcribe →
9 minutes of agent → publish), inside a **~30 minute** door-to-door round trip.
The rest is device sync and poll intervals.

### Honest limitations

- **Sync is not hands-off.** Testing established that audio only reaches the
  vendor cloud while their phone app is *foregrounded* — not while charging, not
  merely with the phone unlocked. See [`docs/transport-tests.md`](docs/transport-tests.md)
  for the matrix and the verdict. This is the biggest open problem.
- **The agent has no web access** in the unattended run, so research output is
  knowledge-only with a caveat banner. Granting it is a deliberate decision, not
  an oversight — see the roadmap.
- **Recordings over ~23 minutes** can't be transcribed in one request. Atticus
  truncates to the first 180 seconds rather than failing, because a command is
  10–30 seconds and the wake word must come first. Chunking is not built.
- **A misheard wake word silently files a real command as a note.** Observed
  once ("Atticus" → "Advocates"). The full transcript is always kept, so nothing
  is lost, but the command doesn't run.
- **Single operator.** No multi-user support and none planned.

## Roadmap

### Skills — the main line of work

Adding a capability is adding a directory, so this is where the leverage is:

- **Signal** — *"Atticus, send <name> a message saying…"* via `signal-cli`
- **Outlook / email** — read, summarise, draft, send
- **Azure DevOps** — *"file a ticket on the DDI project that does X"*, then
  produce an HTML record of the ticket it filed
- **Calendar, notes, home automation** — anything with an API and a CLI

Each follows the same contract: do the thing, then write an HTML record of what
was done, with links. The pipeline never changes.

### Cut the vendor cloud out of the loop

The current round trip is ~30 minutes, most of it waiting on the phone to sync
to the vendor cloud and then on a 15-minute poll. **Talking to the device
directly over BLE from the phone — and pushing straight to the vault — should
roughly halve that to ~15 minutes.**

The BLE protocol has already been mapped from the vendor's own published Android
SDK; see [`docs/decisions/ble-file-transfer.md`](docs/decisions/ble-file-transfer.md)
and [`ble-protocol-notes.md`](docs/decisions/ble-protocol-notes.md). The pin binds
to one client at a time, which is the main obstacle.

This is **not** a bid for real-time. The workflow is asynchronous on purpose. But
15 minutes beats 30, and removing a vendor cloud from the path removes a
dependency, a privacy exposure, and a failure mode.

### Known work, roughly in order

1. **Scope the agent's prompt to the command.** Truncation bounds the *audio* at
   180s, but the whole transcript still reaches the agent — including ambient
   conversation. A sentence cap after the wake phrase would bound the *prompt*.
2. **Fix the output contract.** The agent is told to write to the vault path but
   the pipeline collects from a scratch directory, so a stray `response.md` is
   committed and the byte accounting is wrong.
3. **Decide on web access.** Granting `WebSearch`/`WebFetch` makes research
   actually research — and widens what a prompt-injected agent can reach. A real
   trade-off, deliberately unresolved.
4. **Chunk long recordings** with overlap, so a 40-minute meeting transcribes.
5. **Fuzzy wake-word matching**, or a phonetically stronger wake word.
6. **One transcription implementation** instead of two.

## Running it yourself

**The vault is yours, not ours.** Nothing in this repo names a particular vault.
Scaffold your own, make the repo private, give the host a deploy key with write
access, and point `ATTICUS_VAULT_PATH` at it:

```bash
./ops/init-vault.sh ~/my-vault    # creates inbox/ processed/ failures/ .state/
```

```bash
git clone https://github.com/beekeeper-lab/atticus.git ~/atticus
cd ~/atticus && cp ops/.env.example ops/.env && chmod 600 ops/.env
#   set ATTICUS_VAULT_PATH to YOUR vault checkout
#   set ATTICUS_NOTIFY_URL — a dead pipeline is otherwise silent

./ops/install.sh processor   # transcribe/route/execute
./ops/install.sh ingest      # cloud → vault  (needs a seeded fetcher session)
./ops/install.sh all         # both on one host
```

Roles are **capabilities, not hostnames** — run them on one machine or two. The
installer preflights python, `requests`, the `claude` CLI, `~/.config/ai/env`,
Playwright/Chromium, and a `git push --dry-run` against your vault before
touching systemd.

**Try it without hardware** — synthesises speech, so transcription and the agent
both really run:

```bash
python3 processor/mkfixture.py --clean --say "Atticus, research X and write it up as HTML"
ATTICUS_VAULT_PATH=$PWD/.scratch-vault python3 processor/pipeline.py
```

## Requirements

Python 3.11+, `requests`, git, `ffmpeg`, and the `claude` CLI. Playwright only
for the web fetcher. No GPU, and **no Mac** — transcription is an API call, and
the only Mac-dependent path was designed out.

Credentials come from `~/.config/ai/env`, a single machine-wide file. **Nothing
in this repo holds a key**, `ops/.env` is gitignored, and the vault — which holds
your audio and output — is a separate private repo.

## Licence

[Apache-2.0](LICENSE). Chosen over MIT deliberately: this repository makes
explicit security claims, and Apache requires a modified fork to say that it is
modified — so nobody can ship a weakened build carrying these assurances.

The licence covers this work only. It grants no rights in Plaud's service, whose
undocumented endpoints this software calls; see [`NOTICE`](NOTICE) and
[`SECURITY.md`](SECURITY.md).

## Where to read next

| Document | What |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | **Source of truth.** Architecture, task breakdown, open questions |
| [`docs/decisions/`](docs/decisions/) | ADRs, plus reverse-engineered notes on the device's BLE protocol |
| [`docs/transport-tests.md`](docs/transport-tests.md) | The sync-behaviour matrix and its unfavourable verdict |
| [`docs/history/forge-2026-07-29.md`](docs/history/forge-2026-07-29.md) | A real deployment, including every defect it exposed |
| [`CLAUDE.md`](CLAUDE.md) | Orientation and standing decisions for agents working on this repo |

## A note on the deployment report

[`docs/history/forge-2026-07-29.md`](docs/history/forge-2026-07-29.md) is kept
deliberately unvarnished. It records seven real defects found during deployment —
including one where every `git push` from a systemd unit failed **silently** for
an entire day while the journal reported success, and another where the
processor could read the ingest credential it was supposed to be isolated from.

If you take one thing from this project, take that: **in unattended systems, the
failures that matter are the quiet ones.** A cron job that breaks loudly is a
good day. Most of the engineering here is about refusing to let anything fail in
silence.
