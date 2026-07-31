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

### T6 — Direct BLE GATT enumeration from WarDog  *(executed 2026-07-30)*

**Non-destructive.** Scan plus GATT enumeration plus characteristic reads. No
writes, no handshake, no pairing attempt. The pin stayed bound to the iPhone
throughout.

| Variable | Setting |
|---|---|
| Host | WarDog, Arch Linux, BlueZ `bluetoothd` active, controller `D8:B3:2F:BD:CD:62` |
| Library | `bleak` 3.0.2 (system, not a venv) |
| Pin | **bound to the operator's iPhone**, not charging, button **not** pressed |
| Tools | `ingest/ble_scan.py`, `ingest/ble_read.py` |

**Result: ✅ the full Plaud command channel is present and connectable from
Linux while the pin is still bound to the phone.**

Four things here were not expected.

**1. The pin advertises while bound, with no button press.** The briefing
predicted silence, and the earlier Forge scans that found ~25 devices and no
`0x1910` were read as evidence of that. A plain 20-second scan found it
immediately:

```
★ 55:13:FB:C4:E1:42   -66dBm  Plaud NotePin S
```

Note *how* it matched: on the **name**, not on `0x1910`. The pin advertises **no
service UUIDs at all**. Any scan filtering on `0x1910` in advertisement data
will miss it — which is a plausible explanation for the negative Forge scans,
independent of whether the pin was in range.

**2. The address is a rotating RPA.** It changed between two runs minutes apart
(`55:13:FB:C4:E1:42` → `47:21:DD:E0:E6:CF`). Never persist it; always rediscover
by name. `ble_read.py` already does this.

**3. GATT accepts an unbound client.** Connection succeeded with no pairing
prompt, no bonding, and no rejection — twice. Full enumeration:

```
service 1910  ★ Plaud command/data service
   char 2bb1  [write-without-response,write]  ★ RX — us→device, WRITE here
   char 2bb0  [notify]                        ★ TX — device→us, SUBSCRIBE here
      desc 2902
   char b004  [indicate]                      ← NOT in ble-protocol-notes.md
      desc 2902
   char b001  [read]                          ← NOT in ble-protocol-notes.md
      desc 2901  'V1 read characteristic'
service 180f  battery      2a19 [read,notify] = 0x5a (90%)
service 1800  GAP          2a00 = "Plaud NotePin S", 2a01 = 0, 2a04 = zeros
service 1801  GATT         2a05 [indicate]
service 1804  TX power     2a07 = 0
service fd44                        4f860001..05 [write,indicate]
service 87290102-3c51-43b1-a1a9-11b9dc38478b   6aa50001..0a [read]
```

`0x2BB0` / `0x2BB1` are confirmed present with exactly the properties
`ble-protocol-notes.md` predicted, including the direction inversion — `2bb1` is
writable, `2bb0` is notify-only. **`b001` and `b004` are new** and appear in no
prior note.

`fd44` + `87290102-…` are Apple's Find My Network and Accessory Information
services; the pin is a Find My accessory. Unrelated to audio, but it explains
two of the services and the 8-byte identifier at `6aa50001` (redacted — this is
a public repo and it is a stable hardware ID; it is in the session transcript
if needed).

Readable vendor characteristics:

```
6aa50002 = "PLAUD"            6aa50006 = 0b 00 00 00   (u32 11)
6aa50003 = "Plaud NotePin S"  6aa50007 = 0b 03 01 00   (looks like a version triple)
6aa50005 = 01 00 …            6aa50008 = 00 00 01 00
6aa50009 = 02                 6aa5000a = 00
6aa50001 = <8-byte device ID, redacted>
```

**4. The true ATT MTU is 247, not 184.** `bleak`'s `mtu_size` reports a
placeholder 23 until the BlueZ backend is poked; the negotiated value is 247,
so the ATT payload is 244 bytes. `ble-file-transfer.md` §9 item 8 derives a
171-byte device chunk from an assumed MTU of 184. That assumption is wrong on
this hardware. It does not change correctness — `payloadLen` remains
authoritative — but every throughput estimate built on 171 is low.

#### What this does NOT establish

Reaching GATT is necessary, not sufficient. Still open, and **not** answered by
this test:

- **Whether the pin accepts a handshake from an unbound client.** Nothing was
  written to `2bb1`. `ble-file-transfer.md` §9 item 9 stays open.
- **`portVersion`.** `b001` reads `01 02` (u16le **513**) and its descriptor
  says `V1 read characteristic`. `ble_read.py` labels it a *candidate*
  `portVersion` and that is as far as the evidence goes. **Do not act on it.**
  The authoritative source is `HandShakeRsp` bytes 4–5, which requires a
  handshake. The two readings diverge sharply: if `b001` really is
  `portVersion`, 513 ≥ 20 means **every frame is ChaCha20-Poly1305 encrypted**
  and the key exchange is undecoded — a hard blocker. If instead it encodes
  "V1.2" per its own descriptor, ChaCha20 does not apply. Guessing here
  silently corrupts audio, which is exactly what §9 warns about.
- **The bind signature.** The handshake requires a signature over the device
  serial that Plaud's server issues and the firmware validates. Verified against
  the vendor SDK 2026-07-30; it is not forgeable. This is unaffected by anything
  above.

#### Correction to a prior conclusion

The Forge scans finding "nothing advertising `0x1910`" were interpreted as the
pin staying silent while bound. Finding 1 shows `0x1910` is never advertised by
this device **even when it is plainly present and connectable**, so those scans
could not have detected it on that criterion regardless. Treat that earlier
negative as uninformative rather than as evidence of silence.

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
| 07-30 | **T6** | bound, untouched | n/a | no | ✅ **GATT reachable from Linux while bound** | — |

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
