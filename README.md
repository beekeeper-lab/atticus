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
       │  wi-fi (while charging) or BLE
       ▼
┌──────────────┐
│   WARDOG     │  ingest — pull new recordings, commit them
│  always-on   │
└──────┬───────┘
       │  git push
       ▼
┌──────────────────────────┐
│  atticus-vault (private) │  ← the queue, not just storage
│    inbox/                │
└──────┬───────────────────┘
       │  git pull, every 5 min
       ▼
┌──────────────┐
│    FORGE     │  transcribe → route → execute → publish
│  AI server   │
└──────┬───────┘
       │  git push
       ▼
   processed/2026/07/…/agentic-harness.html
```

**Git is the boundary.** The two halves never talk directly — every handoff is a
commit. That gives durability (a recording is in version control before anything
processes it), a complete audit trail, and independent failure: Forge can be
down for a day and the work simply waits.

## The four ideas worth knowing

**1. The recorder does not need a phone.** The NotePin S has its own 2.4 GHz
Wi-Fi and uploads while charging. An iPhone app was the original plan and turned
out to be unnecessary — see [ADR-001](docs/decisions/ADR-001-no-iphone-app-v1.md).

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
| `ingest/` | WarDog: poller + transport fetchers + BLE scanner |
| `processor/` | Forge: transcribe → route → execute → publish |
| `skills/` | Voice-command capabilities |
| `ops/` | systemd units, env template, installer |
| `ios/` | Empty by design — see ADR-001 |

Audio and generated output live in a **separate private repo**,
`beekeeper-lab/atticus-vault`. Code and bulk binary data do not belong in the
same history.

## Status

| | |
|---|---|
| **Forge half** | ✅ Built and tested end to end — speech in, HTML report out |
| **WarDog half** | Poller done and tested against a mock transport; **the real transport is undecided** |
| **Device** | Not yet in hand |

The open question is how audio gets from the pin to WarDog. Four candidates,
one interface — see [SPEC §2.2.1](docs/SPEC.md). The official Plaud CLI turned
out to be paywalled ([ADR-002](docs/decisions/ADR-002-plaud-web-fetcher.md)), so
the leading option is now talking to the device directly over Bluetooth, using a
protocol mapped from Plaud's own published Android SDK.

## Running it

**Forge** (processing):

```bash
git clone https://github.com/beekeeper-lab/atticus.git ~/atticus
cd ~/atticus && cp ops/.env.example ops/.env && chmod 600 ops/.env
#   set ATTICUS_VAULT_PATH to your atticus-vault checkout
./ops/install.sh forge
```

The installer preflights python, `requests`, the `claude` CLI,
`~/.config/ai/env`, and the vault before touching systemd.

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
