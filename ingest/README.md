# ingest

Polls Plaud Cloud for new recordings and lands them in `atticus-vault`.

**Runs on WarDog** under a systemd timer, every 2 minutes. WarDog must be
always-on; this is the only component with that requirement.

This is the whole of WarDog's job. It never transcribes, never routes, never
runs an agent — it just gets audio into git. Forge takes it from there.

```
plaud recent --json  →  filter vs .state/seen.jsonl  →  plaud audio <id>
  →  download  →  sha256  →  write metadata (status: raw)  →  commit  →  push
```

The recording is durable in git before any processing is attempted on it —
and before Forge has any idea it exists.

Every push is `git pull --rebase --autostash` then push, bounded retry.
Forge pushes to the same repo. WarDog owns `inbox/` and `.state/`; Forge owns
`processed/` and `failures/`.

## Files

| File | Status |
|------|--------|
| `plaud_discover.py` | ✅ Ready. One-time recon — watches a logged-in Plaud Web session and reports the API shape. Redacts secrets. |
| `plaud_web.py` | ⚠️ Contract implemented, **endpoints stubbed**. Fill in `PlaudAPI`'s four TODO(T-06) methods from the discovery report. |
| poller | Not started — blocked on `plaud_web.py`. |

The official CLI is paywalled; we use a Plaud Web fetcher instead. See
[ADR-002](../docs/decisions/ADR-002-plaud-web-fetcher.md).

**Blocked until the device arrives.** Plaud will not accept an audio import
without a bound device, so the web API cannot be observed and the fetcher
cannot be finished. Arrival-day order: T-30/T-32 (do short recordings exist?),
then T-06 (recon), then T-07 (fill in `PlaudAPI`).

Audio download URLs are time-limited. Fetch immediately; never persist the URL.

## Exit codes

`plaud_web.py` distinguishes these so ingest can react correctly — in
particular, **3 means the session died**, which otherwise looks exactly like
"no new recordings."

`0` ok · `2` usage · `3` auth/session expired · `4` transient · `5` upstream changed
