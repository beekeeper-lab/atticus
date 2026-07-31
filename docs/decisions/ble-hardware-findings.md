# NotePin S BLE — first hardware contact

**Date:** 2026-07-29
**Host:** WarDog (Framework, BlueZ, `bleak` 3.0.2, fetchers venv)
**Device state during all tests:** **still bound to the official Plaud iPhone app.**
Nothing here required unbinding.
**Companions:** [`ble-protocol-notes.md`](ble-protocol-notes.md),
[`ble-file-transfer.md`](ble-file-transfer.md) — both written pre-hardware.

Everything below is **measured**, not inferred. This is the first entry in the
project written against a real device.

> ## ⚠️ SUPERSEDED 2026-07-31 — the verdict below is WRONG
>
> **This document's central conclusion is refuted.** It concludes that no partner
> key is required, on the reasoning that the RSA pre-handshake is gated on
> `portVersion >= 20` and that this pin is below that threshold. The conditional
> was right; the assumption was wrong. **This pin reports `portVersion = 20`.**
>
> It was measured passively from the advertisement, and the vendor's own debug
> assertions confirm the decode. So the RSA pre-handshake *and* ChaCha20-Poly1305
> framing both apply here, and direct BLE is **closed**.
>
> Read [ADR-005](ADR-005-direct-device-access-is-closed.md) for the verdict and
> `docs/transport-tests.md` **T6–T8** for the evidence. Attempting the §8
> handshake against real hardware produced **no reply on any characteristic**
> (T7), exactly as `portVersion = 20` predicts.
>
> **Everything else here still stands and is why the file is kept:** the
> measurements are sound — MTU 247, the GATT map, the frame layouts, the SDK
> call-graph. Only the feasibility conclusion and anything downstream of "no
> partner key" is void.

**Original verdict (RETRACTED): direct BLE ingest is feasible. No partner key is
required.**

The link layer is indeed wide open on real hardware, and the plain `req 1`
handshake is **not itself cryptographic** — it takes a client-chosen identifier
string, and Plaud's own reference app falls back to a random UUID for it. §8 has
the exact frame, and that frame was later confirmed byte-for-byte correct against
`HandShakeReq.enPkg()`.

**What this missed:** on `portVersion >= 20` hardware the plain handshake is never
reached. The SDK branches to the RSA pre-handshake before it, and every frame is
encrypted. A correct frame sent into that regime is simply ignored.

MTU turned out to be a non-issue — **247**, not the 23 `bleak` reports (§6).
Nothing technical is now known to block a download. What remains is one
**operator decision**: sending the handshake may bind the pin to our client
(§9).

---

## What was tested, and what was deliberately not

Read-only recon, in escalating order. The line was drawn at writing a Plaud
protocol command, because that is the first action that could plausibly
disturb a working iPhone binding the operator depends on.

| Step | Action | Result |
|---|---|---|
| 1 | Passive advertisement scan | ✅ pin visible |
| 2 | GATT connect + service discovery | ✅ no pairing required |
| 3 | Read every readable characteristic | ✅ all succeeded unauthenticated |
| 4 | Subscribe (CCCD write) to TX + `b004` | ✅ both succeeded |
| 5 | Write protocol commands to RX (§7) | ✅ accepted, **zero replies** |
| 6 | **Write a *correct* handshake** | ⛔ **not attempted — see §9** |

Steps 1–4 were each run multiple times; §5 records the one flaky pass. Step 5
sent only non-mutating queries: req 1, 3, 9, 26. `DepairReq` (5), record control
(20–23), delete (30), clear-all (104), OTA (50/51) and the host→device upload
path (112/114/116) were never sent.

Step 4's CCCD write is the standard notification-enable, not a Plaud request.

---

## 1. The pin is reachable from Linux while bound to the iPhone

```
★ 52:DA:CB:DA:72:B4  -65dBm  Plaud NotePin S
```

- **It advertises no service UUIDs.** `0x1910` is not in the advertisement;
  it only appears after connecting. `ble_scan.py`'s name match is what finds
  it, and its hint that the pin "may not advertise 0x1910 until woken" is
  wrong in a harmless way — it never advertises it at all.
- **The MAC is a resolvable private address and rotates.** Observed
  `52:DA:CB:DA:72:B4`, then `5D:94:3F:0A:A7:8B`, then `5A:FC:8D:3A:B5:75`
  across ~15 minutes. Bits 7:6 of the first octet are `01` = RPA.

> **Load-bearing consequence: never persist the pin's MAC.** Any sync client
> must rediscover by advertised name (`"Plaud NotePin S"`) on every run, or
> resolve the RPA via the bond's IRK. A hardcoded address works until it
> silently doesn't, roughly a quarter-hour later.

### The advertisement carries device metadata

`PkgUtils.convertManufacturerSpecificData2BleDevice` builds a whole `BleDevice`
— **including `portVersion` and SN** — from manufacturer-specific data alone,
with no connection. Its log strings give away the fields:
`"🔧 NotePro portVersion:"`, `"🔍 Branch len<10: SN="`. The parser walks a
length-prefixed chain and special-cases model codes **901** and **705**.

Captured from the pin (company ID `0x005d`, 24 bytes):

```
04 56 07 02 01 | 08 88 20 b5 02 72 38 37 61 | 44 14 00 04 6e f9 ea 37 01 01
└ len=4        └ len=8                      └ tail — does not continue the TLV
   'V' 07 02 01
```

Field 0 is `'V'` (0x56) followed by version bytes — and the SDK holds a literal
`"V"` constant it compares against, so the marker is real. **If the `07` is
`portVersion`, it is 7: below both the RSA threshold and the ChaCha20 threshold
of 20.**

Two honest caveats. The `0x44` third length does not fit the remaining bytes, so
the tail is either not TLV or the chain terminates early — **the walk is
incomplete and the field mapping is a hypothesis, not a decode.** And the
`portVersion` log string sits in the *NotePro* branch (models 901/705), which
this device is probably not.

Worth finishing: a complete decode would yield `portVersion` **passively, from
an advertisement, with no connection and no handshake** — which would retire
Blocker B in §9 outright.

## 2. The command channel is present and matches the corrected notes

Full GATT enumeration succeeded **without bonding or pairing**:

```
service 1910                 ★ Plaud command/data
   2bb0  [notify]            ★ TX — device→host, subscribe here
   2bb1  [write-without-response, write]   ★ RX — host→device, write here
   b001  [read]   desc 2901 = "V1 read characteristic"
   b004  [indicate]
service 180f  battery        2a19 [read,notify] = 0x5a → 90%
service 1800/1801/1804       generic access / attribute / tx power
service fd44                 5× 4f8600xx [write,indicate]   ← unidentified
service 87290102-…-11b9dc38478b   9× 6aa500xx [read]        ← device info
```

**`ble-file-transfer.md`'s correction is confirmed on hardware.** `2bb0` is
notify-only and `2bb1` is write-only, exactly as its correction table states
and opposite to the original `ble-protocol-notes.md` claim. The device-centric
TX/RX naming is real. Anyone implementing this should trust
`ble-file-transfer.md` where the two documents disagree.

Two characteristics **neither document predicted**: `b001` (read) and `b004`
(indicate), both inside `0x1910`.

## 3. Unauthenticated reads work — and they answer real questions

Every readable characteristic returned data with no bonding:

| Char | Bytes | Reading |
|---|---|---|
| `1910/b001` | `01 02` | desc *"V1 read characteristic"* — **candidate `portVersion`** |
| `6aa50002` | `PLAUD\0` | manufacturer |
| `6aa50003` | `Plaud NotePin S\0` | model |
| `6aa50001` | 8 bytes, redacted | device-unique id / serial |
| `6aa50005` | `01 00 …` (8B) | value 1 — **candidate audio-channel count** |
| `6aa50006` | `0b 00 00 00` | 11 |
| `6aa50007` | `0b 03 01 00` | plausibly firmware 11.3.1 |
| `6aa50008` | `00 00 01 00` | plausibly version 1.0.0 |
| `6aa50009` / `6aa5000a` | `02` / `00` | unknown |
| `180f/2a19` | `5a` | battery 90% |

### Why `b001` matters more than it looks

`ble-file-transfer.md` §9 item 1 names `portVersion` the single most
consequential unknown: it selects the voice-frame layout (5-byte vs 1-byte
prefix), the session-list entry stride (10/9/8), the delete-response layout,
and — at `>= 20` — **whether ChaCha20-Poly1305 wraps every frame**, whose key
exchange was never decoded and is the one genuinely hard blocker in the design.

`b001` reads `01 02` behind a descriptor literally labelled *"V1 read
characteristic"*. That is **not proof** — the documents are explicit that
`portVersion` comes from `HandShakeRsp` bytes 4–5 — but nothing here is
anywhere near 20.

**Every readable version-shaped field on this device is a small number.** If
`portVersion` is likewise small, the undecoded ChaCha20 layer does not apply
and the hardest unresolved item in the design is moot. Treat as encouraging,
verify at runtime, and **do not hardcode** — §9 item 1's runtime cross-check
still stands as the way to settle it.

## 4. Subscribing to the command channel does not require bonding

```
✓ subscribed to 2bb0/TX
✓ subscribed to b004
0 spontaneous notifications in 15s
```

This was the most likely place to hit a wall — peripherals commonly gate
notifications behind link encryption. **It did not.** The silence afterwards is
expected: the device answers requests, it does not chatter.

So an unbound Linux client can **connect, discover, read, and subscribe**. No
step in the transport path below the Plaud protocol itself demands a bond.

## 5. The link is usable but not perfectly stable

Across four full read passes, **three completed cleanly and one dropped
mid-pass** — a `GATT Protocol Error: Unlikely Error` on `6aa50009` followed by
`Service Discovery has not been performed yet` on everything after it, which is
`bleak` reporting that the peripheral went away.

Not reproducible: the two runs immediately after it had zero read failures.

The likely cause is **contention with the iPhone**, which is still bound and
periodically reclaims the link. If so, unbinding should make this better rather
than worse. Alternatives not ruled out: an idle timeout for unbound clients, or
that characteristic genuinely erroring under some device state.

> **This is designed for, not a blocker.** `ble-file-transfer.md` §5 specifies
> loss recovery as *stop, then re-issue req 28 with `startOffset = recvOffset`* —
> and because `recvOffset` is never rewound and restart is idempotent, the same
> mechanism recovers a mid-transfer **disconnect** for free. A real client must
> implement reconnect-and-resume regardless; it does not need a stable link, it
> needs a resumable one. Do not treat a dropped connection as a failed download.

## 6. MTU is 247 — and the "23" was a lie

**RESOLVED.** The negotiated ATT MTU is **247**, giving a **244-byte ATT
payload**. Throughput is a non-issue and the handshake fits comfortably.

An earlier revision of this document treated MTU as a possible hard blocker on
the strength of `bleak` reporting `mtu_size == 23`. **That number was never
real.** From `bleak/backends/bluezdbus/client.py`:

```python
@property
def mtu_size(self) -> int:
    if self._mtu_size is None:
        warnings.warn("Using default MTU value. Call _acquire_mtu() …")
        return 23        # <-- hardcoded placeholder, not a measurement
    return self._mtu_size
```

It returns a **literal 23** until something populates `_mtu_size`. The warning
was printed on every run and was the actual finding; it got filtered out as
noise.

### Getting the true value

`_acquire_mtu()` exists — on the **backend**, not on `BleakClient`, which is why
calling `client._acquire_mtu()` raised `AttributeError` and was misread as
"bleak 3.0.2 removed it". It calls D-Bus `AcquireWrite` on the first
`write-without-response` characteristic (`0x2BB1`) and reads the MTU from the
reply:

```python
await client._backend._acquire_mtu()    # then client.mtu_size is real
print(client.mtu_size)                  # 247
```

`ingest/ble_read.py` now does this via a guarded `real_mtu()` helper — private
API, so it degrades to a clear "unknown" rather than a wrong number if bleak
moves it again.

### Consequences

| | Value |
|---|---|
| ATT payload | **244 B** |
| Handshake frame (§8), 38 B | **fits in one write** |
| Audio per voice notification (244 − 10 header) | **~234 B** ≈ 2.9 Opus packets |
| Audio wire rate (80 B = 20 ms mono) | 4 000 B per second of audio |

So one notification carries ~58 ms of audio. Even a pessimistic 10 KB/s moves a
minute of recording in ~24 s.

**Nothing here was a device limitation.** BlueZ 5.86 defaults `[GATT]
ExchangeMTU` to 517 and negotiated 247 without configuration. No `main.conf`
change was made or needed.

> **The lesson, since it cost real time:** a library's fallback constant is not a
> measurement. `mtu_size` had no way to signal "I don't know" through its return
> type, so it returned a plausible-looking number and put the truth in a warning.
> Read the accessor before trusting the value — and treat a suppressed warning as
> suspicious, not as tidy output.

---

## 7. The pin accepts commands and answers nothing

Fresh connection per command, TX subscribed first, 12 s of listening, then a
battery read as a liveness check:

| Command | Write | Link after 12 s | Replies |
|---|---|---|---|
| *control — no write* | — | ✓ alive | 0 |
| req 9 `BattStatusReq` | accepted | ✓ alive | 0 |
| req 3 `GetStateReq` | accepted | ✓ alive | 0 |
| req 1 `HandShakeReq`, bare 3-byte head | accepted | ✓ alive | 0 |
| req 26 `GetRecSessionsReq` | accepted | ✓ alive | 0 |

> **A wrong turn, recorded because it nearly became a conclusion.** An earlier
> single-shot run wrote req 1, then saw every following write fail with
> *"Service Discovery has not been performed yet"*. Read alone that looks like
> **rejection by disconnect** — the device hanging up on an unauthenticated
> client. It was not. It was the §5 flakiness landing on that pass. The
> controlled table above, one command per fresh connection, shows the link
> surviving req 1 and req 26 both. **Never diagnose a rejection from a single
> connection on a link you have already measured as flaky.**

So the device is not refusing us. It is **ignoring malformed frames silently**,
which §8 explains: a bare 3-byte head is not a valid `HandShakeReq`.

## 8. The handshake is not cryptographic — this is the finding that matters

Decompiled from the same public Apache-2.0 AAR as the earlier notes, with
`javap -c -p` (no CFR needed; Java 26's `javap` reads `enPkg()` fine).

### `HandShakeReq` (req 1) actually looks like this

```
0    u8    protocolType = 1
1    u16le requestType  = 1
3    u8    2                  — constant, hardcoded in enPkg()
4    u8    arg1
5    u8    arg2               — ONLY IF arg4 >= 3
6..  ASCII string, right-padded with '0' (0x30), or truncated:
         32 chars if arg4 >= 9, else 16 chars
```

The bare `01 01 00` sent in §7 is missing all of it. Hence the silence.

### And the call site supplies nothing secret

`BleAgentImpl` builds exactly one:

```java
new HandShakeReq(0, 0, this.handle_async_event, bleDevice.getPortVersion())
```

`arg1 = 0`, `arg2 = 0`, `arg4 = portVersion`. So the only real input is the
string — and it is assigned in exactly one place, from a **caller-supplied
parameter**:

```java
public void connectionBLE(BleDevice d, String s1, String s2, String s3, long, long) {
    this.handle_async_event = s1;      // the handshake string
```

The iOS surface names it, and one overload omits it entirely:

```objc
- (void)connectBleDeviceWithBleDevice:(BleDevice *)d deviceToken:(NSString *)deviceToken;
- (void)connectBleDeviceWithBleDevice:(BleDevice *)d;                    // no token at all
- (void)connectBleDeviceWithBleDevice:(BleDevice *)d :(NSString * _Nullable)devToken … ;
```

And the vendor's own template app resolves that token like this:

```swift
// plaud-template-app/ios/…/UI/Onboarding/WelcomeViewController.swift
private static func extractUserId() -> String {
    let token = DeviceManager.shared.partnerToken
    …
    guard …, let sub = json["sub"] as? String
    else { return UUID().uuidString }        // ← fallback: a RANDOM UUID
    return sub
}
```

> **`deviceToken` is an opaque client identifier, not a credential.** It is the
> `sub` claim of the partner JWT when one exists, and **a freshly generated
> random UUID when one does not** — Plaud's own reference app is willing to hand
> the device a value the device has never seen and cannot verify. A device that
> validated this cryptographically could not accept that fallback.
>
> It only needs to be **stable across reconnects**, so the pin recognises the
> same client. Any fixed 32-char string we choose will do. (A UUID string is 36
> chars, so `HandShakeReq` truncates it to 32 — which is why 32 is the
> `portVersion >= 9` width.)

### The RSA path is for NotePro, not for us

`BleAgentImpl`'s log strings draw the line explicitly:

```
"Standard device detected (portVersion < 20), starting standard handshake"
"NotePro device detected (portVersion >= 20), starting pre-handshake"
"❌ userRSAPublicKey 为空，Partner API 可能未正确初始化"
```

**The RSA pre-handshake is gated on `portVersion >= 20`** — the same threshold
that gates ChaCha20 in `ble-file-transfer.md` §2. Below it, the SDK goes
*straight to the standard handshake*: no `PreRSAHandShakeDataSyncReq`, no
partner RSA key, no `userRSAPublicKey`.

This **resolves `ble-protocol-notes.md` §"The open question"**, which framed a
B2B-only partner key as a possible hard block on the whole design. It is a hard
block only for `portVersion >= 20` hardware. Everything in §3 points at the
NotePin S being far below 20.

Also decoded, and **new** — the pre-handshake requests skip `packHead()` (as
`ble-file-transfer.md` §2 says of the RSA one) and share one layout:

```
0    u16le requestType    — 65042 (0xFE12) RSA
                          — 65056 (0xFE20) or 65040 (0xFE10) non-RSA,
                            selected by a boolean ctor arg
2    u8    arg2
3    u8    arg1
4..  opaque byte[]
```

`0xFE10` appears in neither companion document. Note there is a **non-RSA**
pre-handshake, so even `>= 20` hardware may have a route that avoids the
partner key — untested and not needed if portVersion is low.

## What this changes

### The SDK is closed, but we never needed it

The vendor SDK (Android `.aar`, and iOS `PlaudBleSDK` / `PlaudDeviceBasicSDK` /
`PlaudWiFiSDK`) is **B2B-gated**: `PLAUD_CLIENT_ID` + `PLAUD_API_KEY` from the
Developer Portal, *plus* a per-user JWT only a partner backend can mint via
`POST /open/partner/users/access-token`. Confirmed from the repo's own
`PartnerConfig.xcconfig` and README. Not self-serve.

`ble-protocol-notes.md` treated this as possibly fatal. It is not, because it
conflated two things that §8 now separates cleanly:

| | Status |
|---|---|
| Partner key gates the **SDK and Plaud's cloud APIs** | ✅ closed to us |
| Partner key gates the **device's BLE handshake** | ❌ **only at portVersion >= 20** |

Our own `bleak` client reimplements the protocol and imports none of that. The
one string the handshake wants is a client-chosen identifier we generate
ourselves. **The B2B gate costs us nothing on the BLE path.**

### Where a client can run — and where it cannot

The protocol is transport-agnostic, but the host platforms are not equal:

| Target | BLE API | Viable? |
|---|---|---|
| **WarDog / Linux** | `bleak` on BlueZ | ✅ proven above; the obvious proving ground |
| **iPhone, native** | Swift + CoreBluetooth | ✅ possible — reimplement, do **not** use the B2B SDK |
| **Browser, desktop** | Web Bluetooth (Chrome) | ⚠️ works, but a detour |
| **Browser, iOS** | — | ❌ **impossible** |

> **Web Bluetooth does not exist on iOS.** WebKit has never shipped it, and
> every iOS browser is required to use WebKit — so Chrome and Firefox on iPhone
> cannot do BLE either. A "prove it in a web app, then wrap it for mobile" plan
> **cannot reach the iPhone**; the web prototype would be throwaway.
>
> If the phone is a destination, it means a **native Swift app**: Apple Developer
> membership, a Mac or a Mac-free CI path, and CoreBluetooth's background rules
> (`bluetooth-central` mode plus state preservation/restoration — real, but
> fiddly, and iOS will not let it run continuously).

Prove the protocol on WarDog first. Not because the web is impossible, but
because the Python path is already scaffolded, debuggable, and is the same
`pin → WarDog → git` shape the pipeline already assumes — and unlike a browser
prototype, none of that work is wasted if an iOS client follows.

### No prior art exists

Every published Plaud tool — `plaud-toolkit`, `plaud-sync-for-obsidian`,
`riffado`/`openplaud` — is a **Plaud Cloud API client**. None touches BLE. There
is no direct-BLE implementation for any Plaud device to borrow from, which makes
`ble-file-transfer.md` the state of the art and means the remaining risk is
unshared.

### ADR-001's factual premise is dead

ADR-001 rests on *"The NotePin S does not need a phone… when charging and idle
it connects to a configured network and uploads recordings directly to Plaud
Cloud."* `transport-tests.md` **T1 disproved this** (charging + idle + phone
untouched → no sync), and T2 disproved the unlocked-phone fallback. Its
rationale that *"two independent transports already cover each other"* is
therefore false: one of the two does not exist, and the other needs the app
foregrounded.

**ADR-001 needs superseding, not amending.** Its trigger 2 has effectively
fired. That is a separate decision and is not made in this document.

---

## 9. Next steps, and why the handshake was still not sent

**One blocker remains, and it is not cryptographic.** MTU is resolved (§6) — it
was never a real constraint. What is left is a single decision plus one runtime
unknown that the handshake itself answers.

### ~~Blocker A — MTU~~ — CLEARED

Real MTU is **247**, ATT payload **244 B**. The 38-byte handshake fits in one
write and audio throughput is comfortable. See §6.

### Blocker B — confirm portVersion

It selects the handshake string width (16 vs 32), the voice-frame prefix
(1 vs 5 bytes), the session-list stride (10/9/8), and — at `>= 20` — RSA and
ChaCha20. §3 and the §"advertisement" decode both point low, and `b001` reads
`01 02`, but **nothing here is a confirmed read.** `HandShakeRsp` bytes 4–5 is
authoritative, which means the handshake has to succeed before we can be sure —
so implement the runtime cross-check in `ble-file-transfer.md` §9 item 1 rather
than trusting either candidate.

### Why the handshake still was not sent

The operator authorised writing req 1 while still bound, on the understanding
that it would most likely be **ignored or refused**. §8 inverts that: a correct
handshake will most likely **succeed**, and a successful handshake plausibly
**binds the pin to our client**, which is exactly the step the operator reserved
for themselves. The iOS API's `isForceClear:` parameter implies binding is
something that gets cleared and taken over.

**Authorisation obtained for one risk profile does not carry to a materially
different one.** With MTU cleared, this decision is now the *only* thing between
here and a real file listing.

A note for whoever asks for it: prefer a **stable, self-chosen 32-char token**
over a fresh `UUID()` per run. The pin is expected to remember which client it
is bound to, so a rotating token risks either re-binding on every connection or
being rejected. Generate one, commit it to the host's config (not this repo),
and reuse it.

### Then — already built, waiting on that decision

`ingest/ble_sync.py` implements the whole sequence and **refuses to write
anything without `--go`**:

```bash
./ble_sync.py init-token      # stable client token -> ~/.config/atticus/ble-token
./ble_sync.py handshake       # dry run: prints the exact frames, sends nothing
./ble_sync.py handshake --go  # the decision point
./ble_sync.py list --go
./ble_sync.py pull  --go --out ./pulled
```

It does handshake (1) → `portVersion`/`channels` from `HandShakeRsp` → list (26)
→ download (28 → protocolType-2 frames → 29) → Ogg mux, with
stop-and-restart-at-`recvOffset` recovery (§5), the `PLAUD.AI` E2EE guard
(`ble-file-transfer.md` §9 item 7), and a hard stop if the device reports
`portVersion >= 20`. It never sends `DepairReq`, record control,
`ClearRecordFile` or OTA, and deleting recordings is deliberately not
implemented.

Because the handshake frame's width depends on `portVersion` but `portVersion`
is only authoritative *from the handshake reply*, it guesses 7, retries widths
for 9 and 2, then trusts whatever the device reports over the guess that got in.

The dry run is worth eyeballing before `--go`:

```
handshake     01 01 00 02 00 00 33 38 63 33 31 64 33 66 61 38 61 66 34 33 38 66
list (req 26) 01 1a 00 01 00 5a 5a 00 00 00 00 00
```

### Audio muxing — done and tested

`ingest/ogg_opus.py` muxes the bare packet stream into a playable `.opus`.
`ingest/test_ogg_opus.py` round-trips it against `ffmpeg`: 3 s of real 16 kHz
mono CBR-32k Opus → demuxed to a bare stream → re-muxed → **packets
byte-identical, duration exact, decodes to PCM, no CRC warnings.**

> **One correction to `ble-file-transfer.md` §6.** It says granulepos advances
> **320 per packet**. That is the SDK's *encoder frame size* at 16 kHz, not an
> Ogg granule position. RFC 7845 §4 fixes the granulepos clock at **48 kHz
> regardless of input rate**, so a 20 ms packet advances **960**. Using 320
> yields a file that plays correctly but reports **one third** of its true
> duration — silently wrong timestamps everywhere downstream, and the kind of
> error nothing catches until someone questions a transcript's timing. The test
> asserts on duration precisely to pin this down.

## Reproducing this

`bleak` is now installed in the fetchers venv:

```bash
cd ~/Nextcloud/workspace/PLAUD/atticus
V=~/.local/share/claude-fetchers/venv/bin/python
$V ingest/ble_scan.py --seconds 20          # scan; pin shows as "Plaud NotePin S"
$V ingest/ble_scan.py --connect <ADDR>      # enumerate GATT — ADDR rotates, rescan first
$V ingest/ble_read.py                       # §3 reads; finds the pin by name
```

`ingest/ble_read.py` was added by this recon and is what produced §3. Both
tools are read-only and safe to run against a bound pin. Neither sends a Plaud
protocol command.

§8's decompilation, reproducible with nothing but a JDK — no CFR, no jadx:

```bash
git clone --depth 1 https://github.com/Plaud-AI/plaud-sdk-public.git
cd plaud-sdk-public && mkdir -p /tmp/aar && cd /tmp/aar
unzip -q ~/…/plaud-sdk-public/sdk/android/plaud-sdk.aar && unzip -q classes.jar
javap -c -p -constants sdk/penblesdk/entity/bean/ble/request/HandShakeReq.class
javap -c -p -constants sdk/penblesdk/impl/ble/BleAgentImpl.class | grep -oE '// String .*'
```

The `deviceToken` fallback is plain readable Swift in the same repo at
`plaud-template-app/ios/PlaudTemplateApp/UI/Onboarding/WelcomeViewController.swift`.

Field names in the AAR are mangled (`process_item_data`,
`handle_async_event`, …) and carry no meaning — read `enPkg()` and the call
sites, exactly as `ble-protocol-notes.md` warns.
