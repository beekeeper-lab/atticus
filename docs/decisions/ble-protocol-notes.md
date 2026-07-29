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
Characteristic 0x2BB0     (write)
Characteristic 0x2BB1     (notify)
Descriptor     0x2902     (CCCD, standard)
Service        0x180F     Battery
Characteristic 0x2A19     Battery level
```

`0x1910` and the `libtnt_ble_utils.so` native lib point at a Telink BLE stack.

## Framing

From `BaseReqPkgBean`:

```
packHead(), protocolType == 1:
    [uint8  protocolType][uint16be requestType]        3 bytes

enPkg() (per-request; PreRSAHandShakeDataSyncReq example):
    [uint16le requestType][uint8 b][uint8 a][payload…]
```

Note the endianness inconsistency — `packHead` uses big-endian `packInt16`,
`enPkg` uses `packInt16Little`. Both appear in `TntBleCommUtils`. Do not assume
one convention.

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
| **112** | **`FileInfoSyncReq`** | **list files** |
| **114** | **`FileDataSyncReq`** | **transfer file data** |
| **116** | **`FileDataCheckReq`** | **verify transfer** |
| 120–125 | `GetWifiInfo/SetWifi/GetWifiList/TestWifiInfo/TestWifiResult/DeleteWifi` | **Wi-Fi provisioning over BLE** |
| 139 | `CommonStringSetReq` | string setting |
| 142–143 | `GetWifiRssi/Result` | signal strength |
| 65042 (0xFE12) | `PreRSAHandShakeDataSyncReq` | pre-handshake, RSA |
| 65056 (0xFE20) | `PreHandShakeDataSyncReq` | pre-handshake |

The three that matter for ingest are **112 / 114 / 116**. Higher-level entry
points in `BluetoothManager`: `syncHistoricalData()`,
`startFileSyncFromOffset()`, `startRealTimeFileSync()`.

## The open question — RSA handshake

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

`libopus.so`, `libjni_ogg.so`, `JXOggPlayer`, `JXOpusDecoder`, `Mp3Convert` —
audio comes off the device as **Opus in an Ogg container**; the SDK transcodes
to MP3 for convenience. `faster-whisper` reads Opus natively via ffmpeg, so a
direct-BLE pipeline can skip the transcode entirely.

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

## Next step

Arrival day, **before binding the pin to the Plaud app**: scan with `bleak`,
confirm `0x1910` / `0x2BB0` / `0x2BB1` are advertised, and see how far an
unbound device will talk. `ingest/ble_scan.py` is ready to run.
