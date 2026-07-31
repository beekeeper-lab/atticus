# ADR-005 — Direct device access is closed; stay on the vendor cloud

**Status:** Accepted
**Date:** 2026-07-31

## Context

The one manual step left in Atticus is that audio only leaves the NotePin S while
the Plaud phone app is *foregrounded* (`docs/transport-tests.md`, verdict after
T1–T3). Everything downstream of that is hands-off. So "get audio off the pin
without a deliberate act" has been the standing open problem, and talking to the
device directly over BLE was the obvious answer — it would remove the chore, halve
the round trip, and take a third party out of the audio path.

Over 2026-07-30/31 that was investigated to a conclusion. Full detail is in
`docs/transport-tests.md` **T6, T7 and T8**; protocol-level detail in
[`ble-protocol-notes.md`](ble-protocol-notes.md) and
[`ble-file-transfer.md`](ble-file-transfer.md). The short version:

- **T6 — the device is reachable.** Linux connects to it over BLE with no pairing
  prompt, while it is still bound to the operator's iPhone, and enumerates the
  full command channel (`0x1910` with `0x2BB0`/`0x2BB1`). Battery, serial and
  model all read fine. Reaching the device was never the problem.
- **T7 — it will not talk.** A `req 1` handshake drew **no reply on any
  characteristic**, across eight `portVersion` candidates. Writes were accepted
  and ignored — not rejected.
- **T8 — and now we know why.** The pin reports **`portVersion = 20`**, read
  passively from its advertisement. At `>= 20` the vendor's own SDK requires an
  **RSA pre-handshake** and wraps **every frame in ChaCha20-Poly1305**. Our
  handshake frame was byte-for-byte correct; it was simply plaintext and
  out-of-sequence, and silence is the right response to that.

Two things about T8 are worth preserving because they are what makes this a
verdict rather than a guess.

**`portVersion` is in the advertisement, not the handshake.** Every prior note
said to read it from `HandShakeRsp` at runtime. In fact
`PkgUtils.convertManufacturerSpecificData2BleDevice()` parses it out of
advertisement data and `BleAgentImpl.connectionBLE()` feeds that value *into* the
handshake — so the SDK knows it before writing anything, and so can we, with zero
writes.

**The vendor's own debug code confirms our decode.** `PkgUtils` ships an error
branch for `portVersion == 5188` ("parsing logic failed") and a success branch for
`== 20`. On this pin's advertisement, the *wrong* u16 parse of bytes 14–15 yields
exactly 5188 and the *correct* single-byte parse of byte 15 yields exactly 20.
Plaud shipped an assertion that validates our reading.

### Why the RSA key cannot be worked around

The gate is not obscurity. `PartnerApiManager.getPartnerRsaPrivateKey` expects a
key **issued under a B2B agreement**, the signature is over the device serial, and
the **firmware** validates it. The published SDK deliberately contains the grammar
of the conversation and none of the secrets. Stacked on top, the ChaCha20 key
exchange is undecoded — the key/nonce material is assigned from a path nobody has
traced (`SecretUtil.encryptWithChaChaPoly1305Separate`,
`BleGattCallback.process_item_data(byte[])`).

This is a licensing gate, not an engineering gap. More development time does not
close it.

## Decision

**Stay on the vendor cloud. Close the direct-BLE track.** Accept foregrounding
the Plaud app as the trigger that moves audio.

Keep the research code and notes in-tree as reference rather than deleting them —
`ingest/ble_scan.py`, `ble_read.py`, `ble_sync.py`, `ogg_opus.py` — marked
research-only and wired into nothing.

## Options considered

**1. Direct BLE from Linux, as the SDK does it.** Closed, per above. Blocked by a
B2B RSA key plus undecoded frame encryption.

**2. Unbind the pin from the Plaud app and retry.** Rejected — and T8 is the
reason it was never attempted. Client binding was the intuitive suspect, and had
we tested it first we would have spent the pin's binding (losing the official
app's sync, firmware updates and Wi-Fi setup UI, with an untested recovery path)
to learn nothing, because the blocker is `portVersion = 20`, not the binding. This
is the clearest argument in the whole exercise for answering cheap questions
before expensive ones.

**3. Reflash this device with our own firmware.** The lock is firmware policy —
`if` statements on a microcontroller we own — so replacing the firmware genuinely
dissolves *this* blocker. `libtnt_ble_utils.so` and the `0x1910` service indicate
a **Telink** BLE SoC, which has a healthy custom-firmware community and an open
single-wire flashing path, so this is not fantasy. It is still the wrong trade:

- Getting code on means opening a sealed, very small device and finding debug
  pads. The vendor OTA path (requests 50/51) is behind the same encrypted channel
  we cannot enter.
- **Probably no way back.** If flash readout is protected you may be able to
  erase-and-write without ever being able to *dump* the original, so restoring it
  is impossible. You also lose the Plaud app, the cloud, and the device's Apple
  Find My functionality (services `fd44` + `87290102-…`, discovered in T6).
- **It is rewriting a product, not a protocol.** The firmware drives a
  microphone, runs an **Opus encoder on-chip** (which is why audio arrives as
  bare Opus packets), and manages a flash filesystem, power, charging, button,
  LED and a haptic motor — none documented, pin mappings untraced. Decoding the
  protocol took days; this is months.

Two cheaper variants, recorded for whoever revisits: **patch rather than
replace** (dump firmware, flip the crypto gate, reflash — needs readable flash and
no secure boot, and means reverse-engineering assembly rather than tidy Java), and
**read the storage directly** (if recordings live in an external SPI flash, clip
onto it and read them with no firmware work at all — likely dead here, because on
a device this small the flash is probably internal to the SoC, but it costs
nothing to check if one is ever opened).

**4. Build our own recorder.** Technically the cleanest — the firmware problem is
already solved by people who wanted it solved (ESP32-based recorders, open-hardware
wearable recorder projects). **Rejected primarily on physical size.** The NotePin
S's value here *is* that it is small enough to wear without thinking about it.
Anything we build will be materially larger, and a larger device that gets left on
a desk records nothing. The property that makes this device worth using is exactly
the property that makes it closed and unhackable: there is no room inside it for
our convenience.

That tension is the honest summary of this whole ADR.

## Trigger conditions

Revisit only if one fires:

1. **A second, inspectable device is acquired.** The most likely trigger. Buying a
   *separate* pin to open, dump and reflash removes the "no way back" objection
   entirely, because the working device stays working. Option 3 becomes reasonable
   the moment it is not a rescue mission for the only unit.
2. **Plaud offers a partner or developer key self-serve.** That collapses option 1
   from months to days. Worth re-checking annually; it is a business decision on
   their side and could change without notice.
3. **The privacy exposure becomes unacceptable.** If audio that must not transit a
   third party starts being recorded, the cloud path is disqualified on grounds
   that have nothing to do with convenience, and the calculus above resets. This
   mirrors ADR-001's trigger 1.
4. **A small enough open device appears.** Option 4's only real objection is size.
   If an open-hardware recorder reaches NotePin-S dimensions, it wins outright.

## Consequences

- The vendor cloud stays a hard dependency and a single point of failure, and
  recorded audio continues to transit a third party. Accepted for personal use;
  reopened by trigger 3.
- Sync stays human-triggered. `PLAUD_POLL_DAYS` plus the ledger make a missed poll
  window free, so the cost is latency, not loss.
- The ~30-minute round trip stands. Roughly half of that is device sync and poll
  intervals, and it is not reducible without one of the triggers above.
- `docs/transport-tests.md` T6–T8 and the two protocol notes are the record of
  what we knew. They are deliberately detailed enough to resume from cold.

## For a non-technical audience

> We can reach the device just fine — our Linux box finds the recorder over
> Bluetooth, connects to it, and can read its battery level and serial number.
> What we can't do is *talk* to it. Plaud's firmware requires two things before it
> will hand over a recording: a credential cryptographically signed by Plaud's own
> servers, and a fully encrypted command channel. We decompiled Plaud's publicly
> published SDK, which gave us the entire grammar of the conversation — the exact
> commands, byte layouts and sequence — and we confirmed our requests were
> correctly formed. The device ignores them, because the SDK deliberately ships the
> instructions and none of the secrets: it fetches the real keys from Plaud's
> infrastructure, and the signing key is only issued under a commercial partner
> agreement. So this isn't an engineering gap we can close with more effort; it's a
> licensing gate. We have the dictionary and the grammar, but the door needs a key
> Plaud issues, not one the manual lets you derive. Integrating directly would
> require a partnership conversation with Plaud, not more development time.
