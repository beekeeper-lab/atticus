# NotePin S BLE protocol — reverse-engineering notes

**Date:** 2026-07-28 (pre-hardware)
**Source:** `Plaud-AI/plaud-sdk-public` → `sdk/android/plaud-sdk.aar`, Apache 2.0,
published by Plaud. Decompiled with CFR 0.152.
**Purpose:** decide whether WarDog can talk to the pin directly over BLE,
removing Plaud Cloud from the design entirely (SPEC option 1).

**Verdict: feasible.** The protocol is legible and reimplementable in Python
with `bleak`. One unknown remains — whether the handshake requires a
server-issued RSA key.

---

## GATT

```
Service        0x1910
Characteristic 0x2BB0     TX  — device transmits → WE SUBSCRIBE (notify)
Characteristic 0x2BB1     RX  — device receives  → WE WRITE
Descriptor     0x2902     CCCD, standard
Service        0x180F     Battery
Characteristic 0x2A19     Battery level
```

> **Corrected 2026-07-29.** An earlier revision of this file had these
> backwards. The SDK names them `TX`/`RX` **from the device's point of view**
> (`BleProtocol.BleUUID`), which inverts from the client's. Subscribe to
> `0x2BB0`; write to `0x2BB1`.

`0x1910` and the `libtnt_ble_utils.so` native lib point at a Telink BLE stack.

## Framing

From `BaseReqPkgBean`:

```
packHead(), protocolType == 1:
    [uint8  protocolType][uint16le requestType]        3 bytes

enPkg() (per-request; PreRSAHandShakeDataSyncReq example):
    [uint16le requestType][uint8 b][uint8 a][payload…]
```

> **Corrected 2026-07-29.** An earlier revision claimed an endianness
> inconsistency here. There is none — **everything is little-endian.**
> `packInt*` is a JNI native in `libtnt_ble_utils.so`; disassembly shows
> `packInt16` and `packInt16Little` emit identical bytes, differing only in
> in-place-with-cursor vs. allocate-and-return. The apparent big-endian case
> came from `PreRSAHandShakeDataSyncReq`, the one class that overrides
> `enPkg()` and skips `packHead()` entirely. See
> [`ble-file-transfer.md`](ble-file-transfer.md).

## Command table

All observed requests are `protocolType = 1`.

| Req | Class | Purpose |
|-----|-------|---------|
| 1 | `HandShakeReq` | session handshake |
| 3 | `GetStateReq` | device state |
| 4 | `TimeSyncReq` | clock sync |
| 5 | `DepairReq` | unbind |
| 6 | `GetStorageReq` | storage stats |
| 8 | `CommonSettingsReq` | settings |
| 9 | `BattStatusReq` | battery |
| 10 | `OpenWifiReq` | enable Wi-Fi |
| 20–23 | `RecordStart/Pause/Resume/Stop` | **remote recording control** |
| 26 | `GetRecSessionsReq` | list sessions |
| 28–30 | `SyncRecFileStart/Stop/Del` | recording file sync |
| 50–51 | `AppFotaPush/PackFinish` | firmware OTA |
| 61 | `SyncStatFileReq` | stats file |
| 103 | `PrivacySetReq` | privacy setting |
| 104 | `ClearRecordFileReq` | wipe recordings |
| ~~112~~ | `FileInfoSyncReq` | ⚠️ **NOT file listing** — host *uploads* a blob to the device |
| ~~114~~ | `FileDataSyncReq` | ⚠️ **NOT download** — host→device upload data |
| ~~116~~ | `FileDataCheckReq` | ⚠️ host→device upload verification |
| 120–125 | `GetWifiInfo/SetWifi/GetWifiList/TestWifiInfo/TestWifiResult/DeleteWifi` | **Wi-Fi provisioning over BLE** |
| 139 | `CommonStringSetReq` | string setting |
| 142–143 | `GetWifiRssi/Result` | signal strength |
| 65042 (0xFE12) | `PreRSAHandShakeDataSyncReq` | pre-handshake, RSA |
| 65056 (0xFE20) | `PreHandShakeDataSyncReq` | pre-handshake |

> **Corrected 2026-07-29 — this was the most consequential error.** An earlier
> revision named 112/114/116 as the download path. They are the **opposite
> direction**: the host pushing a blob *to* the device. `BleConfirm`'s
> `STICK_GET_FILE_DATA_REQ = 113` is the device asking the host for the next
> slice, and the SDK's only caller is `sendHttpTokenToDevice`.
>
> **The real download path is `26 → 28 → notifications → 29 / 30`:**
>
> | Req | Confirm | Purpose |
> |-----|---------|---------|
> | 26 `GetRecSessionsReq` | `STICK_GET_REC_SESSIONS_CNF = 26` | list recordings |
> | 28 `SyncRecFileStartReq` | `STICK_SYNC_FILE_HEAD_IND = 28` | begin transfer at an offset |
> | — | protocolType-2 frames | the audio itself |
> | 29 `SyncRecFileStopReq` | `STICK_SYNC_FILE_TAIL_IND = 29` | end / CRC |
> | 30 `SyncRecFileDelReq` | `STICK_SYNC_REC_FILE_STOP_RSP = 30` | delete |
>
> Full byte layouts, the chunking scheme, and a Python sketch are in
> **[`ble-file-transfer.md`](ble-file-transfer.md)**. Read that before
> implementing anything.

Higher-level entry points in `BluetoothManager`: `syncHistoricalData()`,
`startFileSyncFromOffset()`, `startRealTimeFileSync()`.

## RESOLVED 2026-07-31 — the RSA handshake blocks this device

**The open question below is answered, and unfavourably.** The NotePin S reports
**`portVersion = 20`**, read passively from its advertisement (`docs/transport-tests.md`
T8). At `>= 20` the SDK requires the **RSA pre-handshake** and wraps **every
frame in ChaCha20-Poly1305**:

```java
if (bleDevice.getPortVersion() >= 20) {   // → pre-handshake
} else {                                   // → standard req-1 handshake
```

So option 1 is blocked for this pin, not merely uncertain. The key is issued
under a B2B agreement (`PartnerApiManager.getPartnerRsaPrivateKey`) and the
ChaCha20 key exchange is undecoded on top of that.

Two consequences for anything below: `portVersion` is **not** a mystery to be
read from `HandShakeRsp` at runtime — it is in the advertisement, available
before any write. And the request numbers here conflate two pre-handshakes:
**`PRE_HANDSHAKE` is 65040 (`0xFE10`)**; 65056 (`0xFE20`) is
`PRE_HANDSHAKE_AND_CLEAR`. Confirms are 65041 and 65042.

Both pre-handshake requests share one layout and skip `packHead()`:

```
[uint16le requestType][uint8 arg2][uint8 arg1][payload…]   payload chunked 100B
```

## The open question — RSA handshake  *(superseded; kept for reasoning)*

`PreRSAHandShakeDataSyncReq` carries an opaque `byte[]`, and
`PartnerApiManager` exposes `getPartnerRsaPrivateKey` /
`setPartnerRsaPrivateKey`. So the device likely authenticates the client
against a key tied to a partner account.

**If the key is issued self-serve** by the (free) Plaud Developer Portal, a
Linux client is legitimate and straightforward. **If it is issued only under a
B2B agreement**, option 1 is blocked and the Android bridge (option 2) becomes
the way to use the SDK without a Mac.

Unresolved. Registering a portal account answers it without hardware.

## Audio format

`libopus.so`, `libjni_ogg.so`, `JXOggPlayer`, `JXOpusDecoder`, `Mp3Convert`.

**Corrected 2026-07-29:** audio arrives as **bare Opus packets with no
container** — the SDK muxes Ogg itself (`OggUtils.init(path, 16000, ch, 320)`).
Packets are a fixed 80 bytes × channels per 20 ms, so the stream is
self-delimiting and byte offsets are exactly seekable.

A direct-BLE pipeline therefore needs to mux Ogg itself (or hand raw Opus to
ffmpeg), rather than expecting a playable file off the wire.

## Notes on obfuscation

Class and method names are clear. **Field names are mangled** into
plausible-looking identifiers (`process_item_data`, `retrieve_config_value`,
`update_internal_state`) — they carry no meaning; read `enPkg()` for the real
wire order.

Some `enPkg()` bodies fail to decompile under CFR (back-jumps in try blocks).
`jadx` or `procyon` would likely do better on those specific methods.

## Legitimacy

The SDK is published publicly by the vendor under Apache 2.0, and the target is
hardware the operator owns. This is interoperability work, not circumvention.
Note that pairing from a custom client requires **unbinding from the official
Plaud app** — devices bind to one client at a time — which forfeits the
official app's firmware updates and Wi-Fi setup UI.

## Companion document

**[`ble-file-transfer.md`](ble-file-transfer.md)** — wire-level byte layouts,
the transfer sequence, chunking and loss recovery, CRC, and the open questions
(notably `portVersion`, which selects four different layouts and whether
ChaCha20-Poly1305 wraps every frame).

## Next step

Arrival day, **before binding the pin to the Plaud app**: scan with `bleak`,
confirm `0x1910` / `0x2BB0` / `0x2BB1` are advertised, and see how far an
unbound device will talk. `ingest/ble_scan.py` is ready to run.
