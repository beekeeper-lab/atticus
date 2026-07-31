# ingest

Polls Plaud Cloud for new recordings and lands them in the vault.

**Runs on the agent host under a systemd timer, every 15 minutes**
([ADR-003](../docs/decisions/ADR-003-ingest-on-the-agent-host.md)). This is the
whole of the role: it never transcribes, never routes, never runs an agent — it
just gets audio into git. The processor takes it from there, and the two only
ever meet through commits.

```
list  →  filter vs .state/seen-<host>.jsonl  →  audio <id>
      →  download  →  sha256  →  write metadata (status: raw)  →  commit  →  push
```

The recording is durable in git before any processing is attempted on it — and
before the processor has any idea it exists.

Every push is `git pull --rebase --autostash` then push, bounded retry. Both
roles push to the same repo and own disjoint paths: ingest writes `inbox/` and
`.state/`, the processor writes `processed/` and `failures/`.

## Why 15 minutes

Because the bottleneck is not here. `docs/transport-tests.md` established that
audio only reaches Plaud Cloud while the Plaud iOS app is **foregrounded** — not
while charging, not merely with the phone unlocked. Recordings therefore arrive
in bursts whenever the operator happens to open the app, potentially hours apart.

Against that, polling every 5 minutes instead of 15 improves mean detection lag
by five minutes and triples the daily headless-Chromium launches against an API
we do not own. `PLAUD_POLL_DAYS` (default 2) plus the ledger makes a missed
window free.

Revisit if the transport ever becomes hands-off — then the latency is ours.

## Files

| File | Status |
|------|--------|
| `poller.py` | ✅ The timer's entry point. Transport-agnostic. |
| `plaud_web.py` | ✅ Complete — auth, `list`, `whoami`, `audio`, verified against real recordings. |
| `plaud_discover.py` | ✅ One-time recon. Watches a logged-in session and reports the API shape; redacts secrets. Re-run if `plaud_web.py` starts exiting 5. |
| `ble_scan.py` | Research only — the direct-BLE transport (T5), not wired into the pipeline. Scan + GATT enumeration, read-only. Start with `--watch`: a bound pin only advertises when woken. |
| `ble_read.py` | Research only. Dumps every readable characteristic and the true ATT MTU. Read-only. |
| `ble_sync.py` | Research only. ⚠️ **Written, never run against hardware.** Handshake → list → download → Ogg. Refuses to write without `--go`. |
| `ogg_opus.py` | Ogg Opus muxer for the BLE path. Round-trip tested against ffmpeg (`test_ogg_opus.py`). |

The official CLI is paywalled; we use a Plaud Web fetcher instead. See
[ADR-002](../docs/decisions/ADR-002-plaud-web-fetcher.md).

Audio download URLs are presigned and time-limited. Fetch immediately; never
persist the URL.

## Operating it

Everything here runs under the fetchers venv, not system python — `poller.py`
launches the transport with `sys.executable`, and the fetcher needs Playwright.

```bash
V=~/.local/share/claude-fetchers/venv/bin/python

$V ingest/poller.py --status     # ledger + inbox summary, changes nothing
$V ingest/poller.py --health     # exercise the session only
$V ingest/poller.py --dry-run    # list and diff, download nothing
$V ingest/poller.py              # one real pass
```

### The session

Auth is a seeded Playwright profile at
`~/.local/share/claude-fetchers/sessions/plaud`, created once with
`plaud_web.py login`. The browser refreshes Plaud's token for us, so the
practical lifetime is the **30-day refresh window**.

`login` needs a display. On a headless host, seed it on a machine that has one
and copy the profile directory across:

```bash
rsync -a --delete other-host:~/.local/share/claude-fetchers/sessions/plaud/ \
                             ~/.local/share/claude-fetchers/sessions/plaud/
```

Leave `PLAUD_SESSION_ROOT` blank unless the session lives somewhere unusual. A
hardcoded path pointing at the wrong home is an auth failure on every tick,
which is how that bug was first found.

### When it dies, you must hear about it

**Set `ATTICUS_NOTIFY_URL`.** An expired session reports "0 new recordings"
forever, which is exactly what a quiet weekend looks like, while audio piles up
in the cloud. `poller.py` alarms on exit 3 and on a failed push, throttled to
one message per condition per `ATTICUS_ALARM_THROTTLE_HOURS` (6). With the URL
unset it says so on every failure and alarms nowhere.

## Exit codes

`plaud_web.py` distinguishes these so `poller.py` can react correctly — in
particular, **3 means the session died**, which otherwise looks exactly like
"no new recordings."

`0` ok · `2` usage · `3` auth/session expired · `4` transient · `5` upstream changed

`poller.py`'s own: `0` clean · `1` partial failure (including committed-but-not-pushed) ·
`2` config error · `3` session dead.

## The second transport: direct BLE

Research only — none of this is wired into the pipeline. It is a parallel path
that would remove Plaud Cloud, the phone, and the official app from the design
entirely, which matters because the cloud path's real constraint is upstream of
this directory: audio only leaves the pin while the Plaud app is foregrounded.

**It does not work, and it cannot.** See
[ADR-005](../docs/decisions/ADR-005-direct-device-access-is-closed.md) and
`docs/transport-tests.md` T6–T8. The link layer is genuinely wide open — the pin
connects from Linux with no pairing prompt *while still bound to the phone*, and
the whole command channel enumerates. But it reports **`portVersion = 20`**, so
its firmware requires an RSA pre-handshake keyed by a credential Plaud issues
under a B2B agreement, and wraps every frame in ChaCha20-Poly1305 whose key
exchange is undecoded. A byte-correct plaintext handshake draws **no reply on any
characteristic**.

An earlier revision of this section claimed the opposite — that no partner key was
required, because it gates only `portVersion >= 20` hardware. The condition was
right; the assumption that this pin sat below it was wrong.

```bash
./ble_sync.py init-token        # local only: writes ~/.config/atticus/ble-token
./ble_sync.py handshake         # dry run — prints frames, writes nothing
./ble_sync.py pull --go         # runs, and gets silence. T7.
```

`--go` is still gated, and the gate is still worth respecting even though the
handshake cannot succeed: it writes to the device, and the failure mode was never
fully characterised. Note that T7 found the writes were *accepted and ignored*,
and the iPhone binding survived — so binding was never the obstacle we assumed.

`ble_sync.py` never sends `DepairReq`, record control, `ClearRecordFile`, or OTA,
and does not implement deleting recordings. Its `PV_GUESS = 7` is wrong for this
hardware and left as-is deliberately: `portVersion` is not something to guess, it
is readable from the advertisement (byte 15 of the manufacturer data).
