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

### T7 — Direct BLE handshake and download attempt  *(executed 2026-07-31)*

**Destructive step, taken with explicit operator consent.** This is the first
time anything has been written to the pin. Only req-1 handshake frames were
sent — never `DepairReq`, record control, delete, or OTA.

**Result: ❌ the pin does not answer a req-1 handshake from an unbound client.
Direct BLE download is not achievable with what we currently know.**

Goal was concrete: pull three recordings off the device and commit them to the
vault without Plaud Cloud. Not achieved.

#### What happened

`ble_sync.py pull --go` reached the pin, connected, negotiated MTU 247, and sent
handshake frames for candidate `portVersion` 7, 9 and 2. Every one was accepted
at GATT level — no error, no rejection — and **none drew a reply**, so
`Pin.handshake()` raised and nothing downstream ran.

The obvious suspect was our listener. Today's T6 enumeration found `b004
[indicate]` inside service `0x1910`, which appears in no prior note, and
`ble_sync.py` subscribes to `0x2BB0` only. If the pin answered on `b004` we
would be deaf to it. So a probe subscribed to **every** notify/indicate
characteristic on the device — `2bb0`, `b004`, `2a19`, `2a05` and all five
Find My `4f8600xx` — and swept a wider `portVersion` range:

```
listening on: 2bb0, b004, 2a19, 4f860001..05, 2a05
b001 before: 01 02  u16le=513

→ pv= 1 (21B)  01 01 00 02 00 36 66 …   (silence)
→ pv= 2 (21B)                            (silence)
→ pv= 3 (22B)  01 01 00 02 00 00 36 …   (silence)
→ pv= 5 (22B)                            (silence)
→ pv= 7 (22B)                            (silence)
→ pv= 9 (38B)  01 01 00 02 00 00 … 65 62 (silence)
→ pv=12 (38B)                            (silence)
→ pv=20 (38B)                            (silence)

b001 after:  01 02  u16le=513
=== 0 frame(s) received total ===
```

**Zero frames on any channel.** The `b004` hypothesis is dead. `b001` is
byte-identical before and after, so the handshake attempts moved no visible
state.

#### What this establishes

- **The pin silently ignores commands from an unbound client.** It does not
  reject them — writes to `0x2BB1` succeed and produce nothing. This partially
  answers §9 item 9 of `ble-file-transfer.md`: a handshake is indeed a hard
  precondition for 26/28, and the pin will not perform one with us in this
  state.
- **`portVersion` is still unresolved, and `b001` is still only a candidate.**
  The authoritative read is `HandShakeRsp` bytes 4–5, and there was no
  `HandShakeRsp`. The `b001 = 513` versus "V1.2 per its own descriptor"
  ambiguity from T6 stands untouched. Do not act on 513.
- **The frame layout is unverified, and this test cannot distinguish two
  explanations.** Either our req-1 layout/token width is wrong, or the pin
  ignores req 1 until a pre-handshake (`0xFE20 PreHandShakeDataSyncReq` or
  `0xFE12 PreRSAHandShakeDataSyncReq`) has run. Neither is implemented, and
  neither layout is known. Silence is consistent with both, so this is not
  evidence for either.
- **The iPhone binding appears intact.** No handshake succeeded, `b001` did not
  change, and the writes behaved as no-ops. Confirm by foregrounding the Plaud
  app and watching a recording sync — which is also how you get the files off.

#### The one untried variable

The pin was **bound to the operator's iPhone** throughout, which is the most
likely reason it ignored us. Unbinding in the Plaud app and retrying is the
experiment that would actually settle it.

**Do not run that experiment with recordings still on the device.** If BLE still
fails after unbinding, those recordings are stranded until the pin is re-paired,
and the recovery path is untested. Drain the device through the working cloud
path first, then experiment on an empty pin.

### T8 — SDK decompile plus a passive advertisement read  *(executed 2026-07-31)*

**Non-destructive.** One passive BLE scan and static analysis of the vendor's
Apache-2.0 SDK. Nothing written to the pin.

**Result: ❌ direct BLE is blocked, and we now know exactly why.
`portVersion = 20`, so every frame is ChaCha20-Poly1305 encrypted and the RSA
pre-handshake is mandatory. Unbinding would not have helped.**

This was run as the "learn something first" alternative to unbinding the pin. It
paid off: it establishes that T7's silence had nothing to do with the iPhone
binding, so the irreversible experiment would have cost the official app and
taught us nothing.

#### Method

`jadx` 1.5.6 on `sdk/android/plaud-sdk.aar` from `Plaud-AI/plaud-sdk-public`
(Apache-2.0, 1.77 MB). jadx decompiled all 345 classes cleanly, including the
`enPkg()` bodies CFR previously failed on — so the earlier notes' gaps were a
tool limitation, not obfuscation.

#### 1. `portVersion` is in the advertisement, not the handshake

`PkgUtils.convertManufacturerSpecificData2BleDevice()` builds a `BleDevice`
carrying serial and `portVersion` from advertisement data alone, and
`BleAgentImpl.connectionBLE()` feeds that same value into `HandShakeReq`. The SDK
knows `portVersion` **before it ever writes to the pin.** So can we:

```
53:BC:59:82:FE:E9  -63dBm  Plaud NotePin S
company id 0x005d (93), 24 bytes
04 56 07 02 01 08 88 20 b5 02 72 38 37 61 44 14 00 04 6e f9 ea 37 01 01
```

Decoded against the SDK parser:

| Field | Offset | Value |
|---|---|---|
| structure width | 0 | `04` |
| letter | 1 | `V` |
| version, 24-bit LE | 2–4 | `07 02 01` |
| serial length | 5 | `08` |
| serial | 6–13 | `8820b50272383761` **as a hex string** |
| `bindInfoLen` | 14 | `0x44` |
| **`portVersion`** | **15** | **`0x14` = 20** |

Two independent cross-checks confirm the decode:

- The serial is read as a **hex string**, not ASCII, and its prefix `882` is the
  SDK's own discriminator for "Plaud NotePin S" (`880` NotePin, `881` NotePro,
  `888` Plaud_NOTE). It matches the advertised name.
- **The vendor's debug code validates our byte offsets.** `PkgUtils` logs
  `"❌ NotePro portVersion STILL WRONG: 5188 (0x1444) - parsing logic failed!"`
  and `"✅ NotePro portVersion CORRECT"` for 20. On this advertisement the
  *wrong* u16le parse of bytes 14–15 yields **exactly 5188**, and the *correct*
  single-byte parse of byte 15 yields **exactly 20**. Plaud shipped an assertion
  that confirms our reading.

#### 2. Therefore ChaCha20 applies, and the RSA pre-handshake is mandatory

`BleAgentImpl` branches on this value:

```java
if (bleDevice.getPortVersion() >= 20) {   // "NotePro device detected … starting pre-handshake"
} else {                                   // "Standard device … starting standard handshake"
```

At `portVersion >= 20`, `ble-file-transfer.md` §9 item 2 says every frame in both
directions is ChaCha20-Poly1305 with a u32le replay counter, and that key
exchange is undecoded. Both blockers are now live for **this** device.

#### 3. Which fully explains T7

Our req-1 frame was **correct**. `HandShakeReq.enPkg()` writes
`[01][01 00][02][arg1]`, then `[arg2]` only when `portVersion >= 3`, then the
token padded to 16 or 32 — byte-for-byte what `ble_sync.py` builds. `DEFAULT_MTU`
is 184 and `PkgUtils.filterByteArray()` merely trims trailing zeros, so it is a
no-op on our frame.

The pin ignored us because we sent a **plaintext, out-of-sequence** handshake to
a device that requires an RSA pre-handshake and ChaCha20-wrapped frames. Silence
is the correct response to that. **The binding was never the blocker.**

#### 4. Corrections to prior notes

- **`ble-hardware-findings.md` is refuted on its central claim.** It states the
  B2B partner key "gates only `portVersion >= 20` hardware (NotePro), not this
  pin." This pin reports 20. The partner key gates *this pin*.
- **`b001` is not `portVersion`.** T6 flagged `b001 = 01 02` (u16le 513) as a
  candidate and warned against acting on it. That caution was right: the real
  value is 20. `b001` is something else, consistent with its own descriptor,
  "V1 read characteristic".
- **The command table conflates two pre-handshakes.** `PRE_HANDSHAKE` is
  **65040** (`0xFE10`); **65056** (`0xFE20`) is `PRE_HANDSHAKE_AND_CLEAR`.
  Confirms are `PRE_HANDSHAKE_CNF` 65041 and `STICK_PREHANDSHAKE_CNF` 65042.
- **`ble_sync.py`'s `PV_GUESS = 7` is wrong for this hardware** and its candidate
  sweep cannot succeed, encryption aside.

#### 5. Newly decoded, recorded for whoever picks this up

Both pre-handshake requests share one layout and **skip `packHead()`**:

```
[uint16le requestType][uint8 arg2][uint8 arg1][payload…]
```

Payloads are chunked 100 bytes per frame (`BleAgentImpl` lines 1182, 1307).

`HandShakeRsp` carries more than the notes recorded — `status` at byte 3,
`portVersion` u16 at 4–5 (as documented), `timezone` 6, `timezoneMin` 7,
`audioChannel` 8, `supportWifi` 9, `noNsAgc` 10, and **`isOggAudio` at byte 11**.
That last one matters: the device can report that audio is *already* Ogg, which
would make our own muxing unnecessary. Untestable until a handshake succeeds.

The SDK's connect preamble, in order: enable battery notify → read battery level
(`0x2A19`) → subscribe TX (`0x2BB0`) → handshake.

#### What would unblock this

Only the ChaCha20 key exchange plus an RSA partner key, in that order.
`SecretUtil.encryptWithChaChaPoly1305Separate` and
`BleGattCallback.process_item_data(byte[])` are where the key material is
assigned from an untraced path; `PartnerApiManager.getPartnerRsaPrivateKey`
wants a key issued under a B2B agreement. Neither is a small piece of work, and
no amount of hardware access substitutes for either.

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
| 07-31 | **T7** | bound, untouched | n/a | no | ❌ **no handshake reply; download not possible** | — |
| 07-31 | **T8** | bound, untouched | n/a | no | ❌ **portVersion=20: ChaCha20 + RSA pre-handshake required** | — |

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
