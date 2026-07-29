# ops

Deployment. One repo, two **roles**, any number of hosts:

```
./ops/install.sh ingest      # Plaud Cloud → vault                   (every 15 min)
./ops/install.sh processor   # vault → transcribe/execute → vault    (every 5 min)
./ops/install.sh all         # both, on one host
```

Roles are capabilities, not machine names — `forge` and `wardog` survive as
aliases, but nothing here assumes a particular box. Both currently run on Forge
([ADR-003](../docs/decisions/ADR-003-ingest-on-the-agent-host.md)).

User-level systemd units only. No sudo, no system services, no containers —
these are trusted single hosts and a container would add a layer without
removing a problem.

`install.sh` is idempotent and preflights everything it can, including a
`git push --dry-run` against your vault, because a push that cannot
authenticate is this system's worst failure mode: work commits, the journal
reads clean, and nothing reaches the other stage.

| File | Purpose |
|------|---------|
| `.env.example` | Committed template. Never holds a real secret. |
| `.env` | Local, gitignored, `chmod 600`. |
| `atticus-ingest.service` / `.timer` | 15-minute Plaud poll |
| `atticus-processor.service` / `.timer` | 5-minute pipeline pass |
| `install.sh` | Idempotent setup, either role or both |
| `init-vault.sh` | Scaffold a fresh vault checkout (run once, on your own repo) |
| `pr.sh` | Land changes to *this* repo through a PR. Refuses to run in the vault. |

## Two things in the units that look like hygiene and are not

**`Environment="GIT_SSH_COMMAND=ssh -F %h/.ssh/config"`.** Every sandbox option
needing a mount namespace puts a systemd *user* unit in a user namespace where
root-owned files read as `nobody:nobody`. ssh then refuses
`/etc/ssh/ssh_config.d/*.conf` with "Bad owner or permissions" and **every push
to the vault fails.** `ssh -F <file>` suppresses the system config and its
includes outright. Confirmed on Fedora 43 — and note the value must be quoted,
or systemd splits it on the space and silently drops the option.

**`Environment=PATH=%h/.local/bin:…`.** User units run with
`PATH=/usr/local/bin:/usr/bin`, which excludes `~/.local/bin` where `claude`
lives. Without it the execute stage dies with "claude binary not found" while an
interactive shell finds it fine — so the preflight passes green and the unit
still cannot launch the agent.

Both were found the hard way. See `docs/deploy/forge-2026-07-29.md`.

## Credentials

**There is no Plaud username or password variable, deliberately.** Auth is a
seeded Playwright browser profile at
`~/.local/share/claude-fetchers/sessions/plaud`, created once with
`plaud_web.py login`. The browser refreshes Plaud's token for us, so the
practical lifetime is the **30-day refresh window**. Nothing in this repo reads a
password; anything that asks for one is a reverse-engineered package — see
SPEC §7.

That profile *is* the secret. Treat it accordingly. Since ADR-003 it sits on the
same host as the agent, which is a real trade — the wake-phrase gate is what
keeps it acceptable, and it must stay enabled on a host that also ingests.

Each host gets its own vault deploy key, write access, independently revocable.

Plaud Embedded credentials (client ID/secret, API key) are parked in `.env`
under a commented W8 block, unused until the contingent iOS app triggers — see
[ADR-001](../docs/decisions/ADR-001-no-iphone-app-v1.md).

Claude Code auth and the GitHub deploy key live in their own stores (`claude`
config, `~/.ssh`). Do not duplicate them into `.env`.

## Known operational hazard

**A dead session is silent.** When the Plaud profile expires, ingest reports "0
new recordings" indefinitely — indistinguishable from a quiet weekend — while
audio accumulates in the cloud.

Mitigation: **set `ATTICUS_NOTIFY_URL`.** `poller.py` alarms on exit 3 and on a
failed push, throttled to one message per condition per
`ATTICUS_ALARM_THROTTLE_HOURS` (default 6) so the alarm stays worth reading.
With the URL unset, every failure prints a line saying it alarmed nowhere.

To check by hand, without touching the phone or the pin:

```bash
~/.local/share/claude-fetchers/venv/bin/python ingest/poller.py --health
```
