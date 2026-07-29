# Transport test protocol

How does audio actually get from the pin to Plaud Cloud? Everything downstream
is built and working; this is the last unknown.

**Run the tests in order.** Each one drains the pin's backlog if it succeeds,
which destroys the ability to run the ones below it on the same recordings.
Ordering is least-destructive first, not most-interesting first.

---

## What we are actually optimising for

Revised 2026-07-29 after the operator described real usage:

- Recordings are **captured away from the computer**, phone always present
- The workflow is **asynchronous** — capture a thought, read the output later
- **Batching is acceptable.** Several recordings syncing an hour later is fine
- Deliberately opening an app is friction; *incidental* phone use is not

So the target is not low latency. It is **"syncs without a deliberate act."**
An hour is fine. A step you have to remember is not, because the failure is
silent — recordings pile up on the pin with no indication.

This softens the original spec, which treated minutes as the goal.

---

## Established

| # | Condition | Result |
|---|-----------|--------|
| E1 | Phone locked, app backgrounded, pin nearby, not charging | ❌ **no sync** — 2 recordings, 20+ min |
| E2 | Phone locked, app backgrounded, pin in another room | ❌ **no sync** |
| E3 | Phone present, app state unrecorded (probably foreground) | ✅ near-instant |

E3 is the confound that misled us: an early result suggested BLE sync was
instant and automatic. It was instant, but almost certainly with the app in
the foreground.

---

## The matrix

Variables: phone location · lock state · Plaud app state · pin charging ·
pin Wi-Fi configured.

### T1 — Charging, phone untouched  *(least destructive)*

| Variable | Setting |
|---|---|
| Phone | in the room, **screen off**, app backgrounded |
| Pin | **on the charger**, idle |
| Action | none — leave it 45 min |

- ✅ syncs → **Wi-Fi-while-charging works.** Best outcome: phone becomes
  optional, nightly charging is the sync trigger, design intact.
- ❌ nothing → **ambiguous.** Either the feature is off or Wi-Fi was never
  configured on the pin. Does not disprove anything on its own.

### T2 — Phone unlocked, Plaud NOT opened  *(the one that matters)*

| Variable | Setting |
|---|---|
| Phone | unlocked, **use any other app** — mail, browser, anything |
| Plaud | still backgrounded, **do not open it** |
| Pin | nearby, not charging |
| Action | use the phone normally for 3 min |

- ✅ syncs → **this is the answer we want.** Unlocking is something you do
  dozens of times a day without thinking. Effectively hands-off in practice.
- ❌ nothing → the app must be foregrounded. Real friction, but survivable
  given the async workflow. Raises the value of T1 and direct BLE.

### T3 — Plaud app foregrounded  *(most destructive; expected to work)*

| Variable | Setting |
|---|---|
| Phone | unlocked, **Plaud app opened**, do not tap anything |
| Pin | nearby |

- ✅ syncs → confirms foreground is the trigger. Note the latency.
- ❌ nothing → something else is broken; stop and investigate.

### T4 — Wi-Fi provisioning  *(only if T1 failed)*

Not a test — a setup step that was never done, and a prerequisite for T1
meaning anything.

1. Update firmware if offered
2. Join the pin to a **2.4 GHz** network (it cannot do 5 GHz)
3. Enable **"Sync to cloud while charging"** — the toggle only appears once
   Wi-Fi is configured
4. Re-run T1 with a fresh recording

Requires Bluetooth and the app, so it drains the backlog as a side effect.
Run it after T2/T3, never before.

### T5 — Direct BLE from WarDog  *(no phone at all)*

Gated on the RSA handshake question, which the free Plaud Developer Portal
registration answers without hardware.

```
ingest/ble_scan.py                 # is the pin visible?
ingest/ble_scan.py --connect ADDR  # does it accept an unbound client?
```

Note the pin binds to one client at a time, so a real attempt means unbinding
from the Plaud app.

---

## Recording results

Append below. Note the app state every time — that is the variable that
misled us once already.

| Date | Test | Phone | App | Charging | Result | Lag |
|------|------|-------|-----|----------|--------|-----|
| 07-29 | E1 | locked, 1 ft | background | no | no sync | — |
| 07-29 | E2 | locked, other room | background | no | no sync | — |
| 07-29 | E3 | present | *unrecorded* | no | synced | seconds |
| 07-29 | **T1** | in room, locked | background | **yes** | ❌ **no sync** | — |
| 07-29 | **T2** | unlocked, using Facebook | background | no | ❌ **no sync** | — |
| 07-29 | **T3** | unlocked, **Plaud opened** | **foreground** | no | ✅ **synced** | seconds |

### Verdict

**Sync requires the Plaud app in the foreground.** Neither charging nor an
unlocked phone is sufficient. T1 and T2 both failed; T3 succeeded immediately.

This is the answer to the question the project has been assuming since day one,
and it is the unfavourable one.

> **Instrument failure worth recording.** A watcher script reported
> "CHARGER/WIFI SYNC WORKS" during T1. It was wrong — the label was hard-coded
> into the detector, so it asserted a cause for *any* new recording. The
> operator had briefly foregrounded Plaud while waking the phone, which is what
> actually triggered the sync. **A test instrument must report what it
> observed, not what it assumed.** The operator's direct observation overruled
> it.

---

## How to check without disturbing anything

Reading the API does not touch the phone or the pin:

```bash
cd ~/Nextcloud/workspace/atticus
/home/gregg/.local/share/claude-fetchers/venv/bin/python \
  ingest/plaud_web.py list --days 1 --json
```

The ingest timer on WarDog pulls anything new into the vault within 5 minutes
regardless, so nothing observed here is lost.
