# ops

Deployment for **two hosts** from one repo:

```
./ops/install.sh wardog     # ingest timer, Node/CLI checks
./ops/install.sh forge      # processor timer, whisper/claude checks
```

No containers in v1 — both are single trusted hosts, and a container would add
a layer without removing a problem.

Both hosts push to `atticus-vault`, so every push goes through the safe-push
wrapper: `pull --rebase --autostash`, then bounded retry (T-45).

| File | Purpose | Status |
|------|---------|--------|
| `.env.example` | Committed template. Never holds a real secret. | ✅ |
| `.env` | Local, gitignored, `chmod 600`. | ✅ |
| `atticus-ingest.timer` / `.service` | 2-minute poll | planned |
| `atticus-processor.service` | Pipeline stages | planned |
| `install.sh` | Idempotent setup | planned |
| `RUNBOOK.md` | Failure modes and recovery | planned |

## Credentials

There is **no Plaud username/password variable**, deliberately. The official
CLI uses browser OAuth and writes `~/.plaud/tokens.json`; nothing here reads a
password. Anything asking for one is a reverse-engineered package — see SPEC §7.

For v1 the real credential is `~/.plaud/tokens.json` itself, not anything in
`.env`. It lives on **WarDog only** — the Plaud credential never touches the AI
server. Mode 600, and treat it as the secret.

Each host gets its own vault deploy key. Two keys, both write-access,
independently revocable.

Plaud Embedded credentials (client ID/secret, API key) are parked in `.env`
under a commented W8 block. They are unused until the contingent iOS app
triggers — see [ADR-001](../docs/decisions/ADR-001-no-iphone-app-v1.md).

Claude Code auth and the GitHub deploy key live in their own stores
(`claude` config, `~/.ssh`). Do not duplicate them into `.env`.

## Known operational hazard

Plaud CLI tokens are obtained through a browser login and copied to Forge by
hand. If they cannot refresh headlessly, expiry is a silent multi-day outage —
recordings pile up in the cloud and nothing errors loudly. T-72 specifies a
watchdog that surfaces this before it bites.
