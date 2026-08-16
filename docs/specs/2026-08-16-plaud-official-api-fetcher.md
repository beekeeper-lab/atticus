# The official Plaud API as a second fetcher — spec

**Date:** 2026-08-16
**Status:** Proposed. Phase 0 gates everything else.
**Source:** the 2026-08-06 → 08-16 ingest outage (PR #108); [ADR-002](../decisions/ADR-002-plaud-web-fetcher.md)
**Covers:** adding a second, officially-supported ingest transport alongside
`ingest/plaud_web.py` — not replacing it.

## Why this is worth revisiting

ADR-002 chose the Playwright fetcher because the official CLI demanded a paid
subscription, and it was right on the evidence available on 2026-07-28. Two
things have changed since.

**Plaud launched a Developer Platform.** It did not exist when ADR-002 was
written. It splits into two products, only one of which is relevant here, and
the docs today are silent on the plan gate that ADR-002 hit.

**The web fetcher just cost ten days of ingest, and the cause was structural.**
Not a Plaud change — our own. `_bearer()` harvested whichever `Authorization`
header appeared first, and on a stale-token page load that is the refresh token
rather than the workspace token. This is not a bug that got fixed so much as a
bug that *the design permits*: we infer a private contract from observed request
order, so any change in that order is a silent, mis-diagnosed failure. It
reported itself as "upstream changed — re-run recon" for ten days while nothing
upstream had changed.

That is the honest argument for a supported API. Not that the web fetcher is
broken — it works, it is fixed, and it is free — but that its failure modes are
*unbounded and mislabelled*, and it is the single point of failure in front of
everything else Atticus does.

## What Plaud actually ships

| | **Plaud Embedded** | **Plaud MCP & CLI** |
|---|---|---|
| For | Partners embedding Plaud in their own product | An individual's own Plaud data |
| Auth | `client_id` + `secret_key` → partner token → per-user token | Browser OAuth, tokens at `~/.plaud/tokens.json` |
| Data | Audio **you upload** through their SDK | **Your existing recordings** |
| Billing | $15/device/mo + $0.28/transcription-hour; 50 devices + 300 hrs free | Not addressed by the billing doc, which scopes charges to Embedded |
| Verdict | **Wrong product** | **The candidate** |

**Embedded is rejected on read, not on price.** Its file API generates presigned
*upload* URLs; there is no endpoint to list an account's existing recordings or
download their original audio. It is a pipeline for audio you captured yourself
through their SDK — which is to say, it solves a problem we do not have, and
does not solve the one we do.

The MCP/CLI surface maps onto our needs almost exactly:

| Our contract | Plaud CLI | Plaud MCP |
|---|---|---|
| `whoami` | `plaud login` / implicit | `get_current_user` |
| `list --days N` | `plaud recent --days N`, `plaud files --page` | `list_files` |
| `audio <id> -o` | `plaud audio <id>` → 24h URL | `get_file` → `presigned_url` |
| — (unused) | `plaud transcript`, `plaud summary` | `get_transcript`, `get_note` |

## Phase 0 — the question that gates everything

**Is the CLI still behind a paid plan for a Starter account?**

ADR-002's paywall finding is empirical and three weeks old, and it predates the
platform launch. The current CLI docs state no plan requirement, and the billing
page explicitly scopes charges to Embedded. That is suggestive and not evidence:
docs omitting a gate is not the same as a gate being removed.

Nobody should write a line of `plaud_cli.py` before this is answered, and it is
a five-minute answer. It needs an interactive browser, so it is the operator's
to run, on the ingest host:

```bash
npm install -g @plaud-ai/cli     # Node 20+; currently v0.3.8
plaud login                      # browser OAuth — the step that failed in ADR-002
plaud recent --days 2            # does a Starter account get its own file list?
plaud audio <id-from-above>      # and its own original audio?
```

Three outcomes, three different specs:

- **Free.** Proceed to T1 below. ADR-002 gets amended, not superseded — its
  reasoning was sound on its evidence.
- **Paid, ~$216–360/yr.** Stop. ADR-002's arithmetic is unchanged and still
  says no: we use none of Plaud's transcription, and the free tier already
  exposes the original audio. Record the re-test in ADR-002 so the next person
  does not re-derive it a third time, and keep the web fetcher.
- **Free but crippled** (no original audio on Starter, only transcripts) — the
  outcome worth watching for, because it looks like success. Atticus needs the
  MP3; Plaud's transcripts do not exist for our short commands anyway, since
  AutoFlow needs >200 words. Treat as "paid".

## The seam already exists

This is the cheap part, and it is cheap by prior design. `ATTICUS_FETCHER`
already names a *pluggable executable*:

```python
# processor/config.py
self.fetcher = g("ATTICUS_FETCHER", "ingest/plaud_web.py")
```

`poller.Fetcher` shells out to it and requires only a four-command CLI
(`whoami | list | audio`, each `--json` where applicable) and the documented
exit codes `0/2/3/4/5` → `F_OK / F_USAGE / F_AUTH / F_TRANSIENT / F_CHANGED`.
It already validates the returned shapes and maps garbage onto that contract.

**Nothing downstream of `Fetcher` changes.** A second transport is a second
executable and a failover policy. That is the whole integration.

---

## T1 — `ingest/plaud_cli.py`

A thin adapter over `@plaud-ai/cli`, implementing the same four-command contract.

**Owns:** `ingest/plaud_cli.py`, `tests/unit/test_plaud_cli.py`.
**Must not touch:** `ingest/plaud_web.py`, `ingest/poller.py`.

Design notes that are not optional:

- **Normalise at the boundary, as `plaud_web._normalize()` does.** Plaud's
  vocabulary must stop inside this file. Re-check the millisecond trap
  independently — `duration` and `start_time` are ms in the web API, and the
  CLI's units are unverified. Do not assume they match.
- **Filter the seeded marketing files.** `serial_number` starting `welcome_` is
  the discriminator; three ship with every account.
- **`audio` must write the file, not print a URL.** The CLI returns a 24h link;
  the adapter fetches it and writes to `-o`, because `Fetcher.audio()` asserts a
  non-empty file exists afterwards. Fetch the presigned URL **without** auth
  headers, as with the S3 URLs in the web path.
- **Never let a URL into an error string.** `poller._run` redacts, but that
  guard exists because a presigned URL with session cookies reached the journal
  on 2026-07-30 — a short-lived credential made durable. Redact at the source
  too.
- **Distinguish "not signed in" from "plan required".** Both are auth-shaped;
  only one is fixable by re-running `plaud login`. A plan gate re-appearing
  after we adopt this is a *policy* change and must not be reported as an
  expired session — that is precisely the mislabelling that made the last
  outage ten days long instead of one poll interval.

**Acceptance:** `ATTICUS_FETCHER=ingest/plaud_cli.py` ingests a real recording
end to end, and the same recording ingested via either fetcher produces
byte-identical audio and an equivalent metadata JSON.

## T2 — failover, and never silently

**Owns:** `ingest/poller.py` fetcher selection, `processor/config.py`.

Add `ATTICUS_FETCHER_FALLBACK` (default blank). On `F_AUTH` or `F_CHANGED` from
the primary — not on `F_TRANSIENT`, which is what retries are for — try the
fallback once within the same tick.

Failover is safe here for a reason specific to this pipeline: the ledger makes a
double fetch free, because no ID is ever processed twice. That is what makes
this a few lines rather than a distributed-systems problem.

**Running on the fallback must raise an alarm.** A pipeline that quietly
self-heals into a degraded mode is the failure this repo has been bitten by
repeatedly: the alarm net works, and the thing that defeats it is a condition
that never announces itself. Degradation is `ALERT`, throttled per condition —
not `CRITICAL`, since ingest is still working.

**Acceptance:** with the primary forced to fail auth, a recording still lands,
and exactly one alarm says which transport is carrying it.

## T3 — the credential, and the unit that must not see it

**Owns:** `ops/atticus-processor.service`, `ops/install.sh`, `SECURITY.md`.

`plaud login` writes `~/.plaud/tokens.json`. That is a **second Plaud credential
on the agent host**, and the processor unit currently isolates only the first:

```ini
InaccessiblePaths=-%h/.local/share/claude-fetchers
```

`ProtectHome=read-only` permits reads, so without an explicit entry the agent
that executes ambient-audio-derived text can read the new token file. Add:

```ini
InaccessiblePaths=-%h/.plaud
```

This is the easiest thing in the whole spec to forget and the most expensive to
forget. **T3 lands before T1 is ever pointed at a real account**, not after.

Also name in `SECURITY.md`: adopting this puts a global npm package and Node 20+
on the ingest host, which is new supply-chain surface that the Playwright path
does not carry. Pin the version; do not install with `-g @latest` from a timer.

**Acceptance:** as the processor user, reading `~/.plaud/tokens.json` fails.
A test asserts the unit file contains the path, in the style of the existing
unit assertions.

## T4 — write down what we learned

**Owns:** `docs/decisions/ADR-002-plaud-web-fetcher.md`, `CLAUDE.md`.

Amend ADR-002 with the Phase 0 result whatever it is. A re-test that says "still
paid" is as valuable as one that says "now free" — it is the difference between
this being reconsidered annually and being reconsidered every time someone
notices the fetcher is unofficial.

If T1–T3 land, ADR-002 is **amended, not superseded**: the web fetcher stays as
the fallback, and $0/yr with two transports beats $0/yr with one.

---

## Not in this spec

- **Plaud Embedded.** Wrong product; see above. Revisit only if Atticus ever
  captures audio it did not get from a Plaud device.
- **Plaud's transcription.** ADR-002 and SPEC §2.3 both settle this. We use
  `gpt-4o-transcribe` deliberately, and short commands produce no Plaud
  transcript at all.
- **The MCP server as the ingest path.** The CLI and MCP expose the same data,
  but MCP means a live credentialed server; the CLI is a subprocess with an exit
  code, which is what `Fetcher` already speaks. More to the point, an MCP server
  reachable from the agent's network namespace is a Plaud credential inside the
  sandbox boundary — the exact thing `InaccessiblePaths` exists to prevent.
  **Do not wire Plaud MCP into the processor.**
- **Tightening the poll interval.** Unchanged and still not an improvement:
  arrivals are human-triggered bursts hours apart because sync needs the Plaud
  app foregrounded. A cheaper API call does not change the arrival pattern.
- **Retiring `plaud_web.py`.** It is the fallback. Keeping two transports is the
  point.

## What would make us abandon this

- Phase 0 says paid, or says free-without-original-audio.
- The CLI's list omits fields `Fetcher.list()` requires (`id`, `created_at`) and
  they cannot be derived.
- Plaud re-gates after adoption. Mitigated by T2: the fallback is still there
  and the alarm says so, which is a materially better failure than the one we
  just had.
