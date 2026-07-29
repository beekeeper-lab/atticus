# Atticus

Speak a task into a wearable recorder. An agent executes it and publishes the
result. **No human interaction between "stop recording" and "read the output."**

> *"Atticus, research the definition of an agentic harness. Do a complete
> write-up and generate an HTML guide with the results."*

Twenty minutes later there is a finished HTML report in a private git repo.
Nobody touched a keyboard.

---

## How it works

```
┌──────────────┐
│  NotePin S   │  you speak, you stop. that is the entire interaction.
└──────┬───────┘
       │  BLE → Plaud app → Plaud Cloud
       ▼
┌──────────────┐
│   ingest     │  poll Plaud every 15 min, download, commit
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
│  processor   │  transcribe → route → execute → publish
└──────┬───────┘
       │  git push
       ▼
   processed/2026/07/…/agentic-harness.html
```

**Git is the boundary.** The two stages never talk directly — every handoff is a
commit. That gives durability (a recording is in version control before anything
processes it), a complete audit trail, and independent failure: the processor can
be down for a day and the work simply waits. They are separately timed and
separately resumable whether they run on one host or two.

## The four ideas worth knowing

**1. Everything downstream of the cloud is solved; the pin is the hard part.**
The design assumed the NotePin S would upload over its own Wi-Fi while charging,
making the phone optional. **Testing disproved that** — sync only happens with
the Plaud app *foregrounded*, not while charging and not merely with the phone
unlocked. The matrix and verdict are in
[`docs/transport-tests.md`](docs/transport-tests.md). Everything from Plaud Cloud
onward is deterministic and works; getting audio off the pin without a deliberate
act is the open problem.

**2. Routing is the model's job.** `claude -p` runs with `skills/` mounted and
picks a matching skill from its description. **Adding a capability means adding
a directory.** There is no routing table, and there should not be one.

**3. The transport is pluggable.** Ingest shells out to a fetcher executable
implementing a four-command CLI. Direct BLE, an Android bridge, or the Plaud web
API all satisfy it, so the transport can change without touching the pipeline.

**4. A wearable overhears things.** Only transcripts beginning with **"Atticus"**
are executed. Everything else is filed as a note. Beyond that, the agent never
holds a git credential — it writes to a scratch directory and the pipeline
commits on its behalf.

## Layout

| Path | What |
|------|------|
| [`docs/SPEC.md`](docs/SPEC.md) | **Source of truth.** Architecture, full task breakdown, open questions. |
| `docs/decisions/` | ADRs, plus reverse-engineering notes on the device's BLE protocol |
| `ingest/` | Poller + transport fetchers + BLE scanner |
| `processor/` | transcribe → route → execute → publish |
| `skills/` | Voice-command capabilities |
| `ops/` | systemd units, env template, installer |
| `ios/` | Empty by design — see ADR-001 |

Audio and generated output live in a **separate private repo**,
`beekeeper-lab/atticus-vault`. Code and bulk binary data do not belong in the
same history.

## Status

| | |
|---|---|
| **Cloud → vault** | ✅ Working on real recordings. Fetcher, poller, ledger, 15-min timer. |
| **Vault → output** | ✅ Working end to end — speech in, HTML report out |
| **Pin → cloud** | ⚠️ **Needs the Plaud app foregrounded.** The one unsolved link. |

The official Plaud CLI turned out to be paywalled
([ADR-002](docs/decisions/ADR-002-plaud-web-fetcher.md)), so ingest authenticates
with a Playwright browser session and calls Plaud's JSON API directly.

The remaining work is upstream of all of it: making the pin sync without a
deliberate act. Candidates are Wi-Fi provisioning, direct BLE (a protocol mapped
from Plaud's own published Android SDK), or the contingent iOS app of
[ADR-001](docs/decisions/ADR-001-no-iphone-app-v1.md). Until one lands, the
workflow is genuinely asynchronous rather than hands-off — which the design
tolerates, because nothing waits on a human.

## Running it

**The vault is yours, not ours.** Nothing in this repo names a particular vault.
Scaffold your own, make the repo private, give the host a deploy key with write
access, and point `ATTICUS_VAULT_PATH` at your checkout:

```bash
./ops/init-vault.sh ~/my-vault    # creates inbox/ processed/ failures/ .state/
```

```bash
git clone https://github.com/beekeeper-lab/atticus.git ~/atticus
cd ~/atticus && cp ops/.env.example ops/.env && chmod 600 ops/.env
#   set ATTICUS_VAULT_PATH to YOUR vault checkout
#   set ATTICUS_NOTIFY_URL — a dead pipeline is otherwise silent

./ops/install.sh processor   # transcribe/route/execute
./ops/install.sh ingest      # Plaud → vault  (needs a seeded Plaud session)
./ops/install.sh all         # both on one host
```

The installer preflights python, `requests`, the `claude` CLI,
`~/.config/ai/env`, Playwright/Chromium, and — importantly — a
`git push --dry-run` against your vault, before touching systemd.

Ingest additionally needs a one-time interactive `plaud_web.py login`, which
wants a display; on a headless host, seed it elsewhere and copy the profile
directory across. See [`ingest/README.md`](ingest/README.md).

**Try it without hardware** — synthesises speech, so the real transcription and
agent paths both run:

```bash
python3 processor/mkfixture.py --clean --say "Atticus, research X and write it up as HTML"
ATTICUS_VAULT_PATH=$PWD/.scratch-vault python3 processor/pipeline.py
```

## Requirements

Python 3.11+, `requests`, git, and the `claude` CLI. No GPU, and **no Mac** —
transcription is an API call, and the only Mac-dependent path (a native iOS app)
was designed out.

Credentials come from `~/.config/ai/env`, the machine-wide credential file.
Nothing in this repo holds a key, and `ops/.env` is gitignored.
