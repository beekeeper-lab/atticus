# ADR-003 — Ingest runs on the agent host

**Status:** Accepted
**Date:** 2026-07-29
**Supersedes:** the "Plaud credential never touches Forge" clause of SPEC §2.0

## Context

The original topology split the pipeline across two machines for one reason:
keep the Plaud credential off the box that runs an autonomous agent.

| Host | Role |
|------|------|
| WarDog | ingest — Plaud Cloud → vault |
| Forge | processor — vault → transcribe → route → execute → vault |

That split was never about load. It was about blast radius. Forge executes text
derived from ambient audio; anything spoken near the pin can become an
instruction. Putting a live Plaud session on that host means a
sufficiently-unlucky prompt injection has an authenticated path to the
operator's entire recording history.

Two things changed.

**1. The device→cloud transport is unsolved and is its own project.**
`docs/transport-tests.md` established that audio only reaches Plaud Cloud while
the Plaud iOS app is *foregrounded* — not while charging (T1), not merely with
the phone unlocked (T2). Making that hands-off is open-ended work: Wi-Fi
provisioning, direct BLE, possibly the contingent iOS app of ADR-001. It wants
a machine to experiment on, and it has nothing to do with polling an API.

**2. The cloud→vault half is finished and deterministic.** The fetcher, the
poller, the per-host ledger, and the vault's disjoint-path concurrency model all
work and were exercised on real recordings. It is a cron job with a browser
session.

The operator's decision (2026-07-29) is to split along that seam instead:
WarDog keeps the transport experiments, and the agent host owns the settled
cloud→vault pipeline end to end.

## Decision

**Ingest and processing both run on the agent host.** The Plaud session lives
there.

Roles become capabilities rather than machine names:

```
./ops/install.sh ingest      Plaud Cloud → vault
./ops/install.sh processor   vault → transcribe/route/execute → vault
./ops/install.sh all         both
```

`forge` and `wardog` remain as aliases, but nothing in the repo assumes a
particular host. The per-host ledger (`.state/seen-<host>.jsonl`) already
supports any number of ingest hosts, so this is a deployment change, not a code
change — WarDog can resume ingesting at any time without coordination.

## Consequences

### What we accept

**The credential isolation is weakened, and it was real.**
`~/.local/share/claude-fetchers/sessions/plaud` is a live, authenticated Plaud
session sitting on the same box as an agent that executes text derived from
ambient audio.

Two mitigations, in order of how much they actually buy:

1. **The processor cannot see the session directory.** The unit carries
   `InaccessiblePaths=%h/.local/share/claude-fetchers`. This was **not** in the
   original revision of this ADR, and its absence was a live hole:
   `ProtectHome=read-only` still permits reads, so the processor sandbox could
   read the session cookie jar — verified, 28 KB, fully readable. Adding the
   line blinds the processor while leaving ingest's own access untouched, both
   confirmed against the real units. This recovers most of what moving ingest
   here gave away, at zero functional cost, because the processor never had any
   use for that directory.
2. **The wake-phrase gate** (`ATTICUS_WAKE_PHRASE`), which already demonstrated
   its value: of the first real recordings, all but one were correctly filed as
   unexecuted notes. It remains load-bearing for credential safety, not just for
   avoiding spurious agent runs. **Do not disable it on a host that also
   ingests.**

Neither is a sandbox. The agent still has network access and still writes the
vault. The point is that the specific, obvious path from "overheard sentence" to
"exfiltrated Plaud session" is closed.

Worth revisiting if the agent ever gets broader tool permissions — granting
`WebFetch`/`WebSearch` (open defect #3 in `docs/history/forge-2026-07-29.md`)
moves in exactly the wrong direction and should be weighed against this.

### What we gain

- **One host to reason about.** No cross-machine handoff to debug when a
  recording does not appear.
- **The always-on requirement lands on the machine that already is.** SPEC's
  assumption A3 (WarDog always-on) is retired; the agent host is 24×7 anyway.
- **WarDog is freed** to be an experiment rig for the transport problem, where
  rebooting, unbinding the pin, and reflashing firmware cost nothing.

### What stays true

- Git is still the queue. The two stages still communicate only through
  commits, still own disjoint paths (`inbox/` + `.state/` vs `processed/` +
  `failures/`), and are still separately timed and separately resumable. Nothing
  about running them on one host merges them.
- The vault is still configured, never assumed. `ATTICUS_VAULT_PATH` points at
  whatever private repo you own; this repo names none.
