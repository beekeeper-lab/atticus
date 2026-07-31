# NotePin S BLE — file-transfer wire format

**Date:** 2026-07-29 (pre-hardware)
**Source:** `Plaud-AI/plaud-sdk-public` → `sdk/android/plaud-sdk.aar`, Apache 2.0.
Java decompiled with CFR 0.152, checked against `javap -c -p` where CFR failed
or reordered. `jni/arm64-v8a/libtnt_ble_utils.so` disassembled with
`llvm-objdump --triple=aarch64`.
**Companion:** `ble-protocol-notes.md` (GATT, command table, handshake).
**Scope:** the byte layout of listing and downloading recordings. Handshake and
pairing are out of scope and unchanged from the companion note.

Everything below is **read from bytecode or machine code** unless explicitly
tagged **(inferred)**. Read the "Unresolved" section before implementing.

---

## Corrections to `ble-protocol-notes.md`

Three claims in the companion note are wrong. They are load-bearing.

| Claim in companion note | Actual |
|---|---|
| `0x2BB0` write, `0x2BB1` notify | **Reversed.** `0x2BB0` = `TX` = notify (host subscribes). `0x2BB1` = `RX` = write (host writes). Names are device-centric. |
| `packHead()` uses big-endian `packInt16`; endianness is inconsistent | **All integers on this protocol are little-endian.** `packInt16` is native and compiles to `strh` on a little-endian ELF. `packInt16` and `packInt16Little` emit identical bytes. There is no inconsistency. |
| 112 / 114 / 116 are the recording-download path | **They are the opposite direction** — host *uploads* a blob to the device (the SDK uses it to push an HTTP token). Recording download is **26** (list) + **28/29/30** (session) + **protocolType 2** data frames. |

Evidence for the endianness claim, `Java_..._packInt` @ `0xb2c`:

```
ba4: strb  w22, [x2, w21, sxtw]      ; width 8
bb0: strh  w22, [x2, w21, sxtw]      ; width 16   <- LE store
bc8: strb  w22,[x9] / strb w10,[x9,#1] / strb w11,[x9,#2]   ; width 24, explicit LE
b98: str   w22, [x2, w21, sxtw]      ; width 32 (also the default case)
bdc: str   x22, [x2, w21, sxtw]      ; width 64
```

Jump table at file offset `0x7e0` is `[3,6,9,0,0,0,0,17]`, indexed by
`ror32(width-8, 3)`, confirming the 8/16/24/32/…/64 mapping. `readInt` @ `0xc14`
mirrors it with `ldrb` / `ldrh` / manual-LE-24 / `ldr w` / `ldr x`, table
`[2,4,6,0,0,0,0,14]` at `0x7e8`.

---

## 1. `TntBleCommUtils` primitives

`process_item_data` is the singleton; `getInstant()` returns it. Five natives
live in `libtnt_ble_utils.so`; everything else is pure Java.

### Native

| Java signature | Native behaviour | Returns |
|---|---|---|
| `packInt(int width, byte[] b, int off, long v)` | writes `width/8` bytes of `v` at `b[off]`, **little-endian**. `width` other than 8/16/24/32/64 falls through to the 32-bit case. No bounds check (uses `GetPrimitiveArrayCritical`). | `off + width/8` — the *next* offset |
| `readInt(int width, byte[] b, int off)` | reads `width/8` bytes at `b[off]`, **little-endian**, **zero-extended** into a `jlong`. Unknown widths read 32 bits. | value |
| `readFloat(byte[] b, int off)` | `ldr s0` — 4-byte LE IEEE-754 | float |
| `tntGetCrc(byte[] b, int len, int initCrc)` | CRC-16/CCITT-FALSE over `b[0..len)`. poly `0x1021`, MSB-first, no reflection, no final XOR, seed = `initCrc`. | `short` (low 16 bits) |
| `tntGetFileCrc(String path, int initCrc)` | same, over the whole file | `int`, masked `& 0xFFFF` |

CRC verified numerically against the ARM64 instruction sequence:
`"123456789"` with seed `0xFFFF` → `0x29B1`, which is the CRC-16/CCITT-FALSE
check value. The SDK's only call site (`NiceBuildSdk.sendHttpTokenToDevice`)
passes seed `65535`.

Python equivalent:

```python
def tnt_crc16(data: bytes, seed: int = 0xFFFF) -> int:
    crc = seed
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc
```

### Java wrappers

| Method | Bytes | Endian | Offset semantics |
|---|---|---|---|
| `packInt08(b, off, v)` | 1 | — | in-place; returns `off+1` |
| `packInt16(b, off, v)` | 2 | LE | in-place; returns `off+2` |
| `packInt24(b, off, v)` | 3 | LE | in-place; returns `off+3` |
| `packInt32(b, off, v)` | 4 | LE | in-place; returns `off+4` |
| `packInt64(b, off, v)` | 8 | LE | in-place; returns `off+8` |
| `readInt08(b, off)` | 1 | — | → `int`, unsigned |
| `readInt16(b, off)` | 2 | LE | → `int`, unsigned (0..65535) |
| `readInt24(b, off)` | 3 | LE | → `long`, unsigned |
| `readInt32(b, off)` | 4 | LE | → `long`, unsigned (0..4294967295) |
| `readInt64(b, off)` | 8 | LE | → `long` |
| `packInt8Little(v)` | 1 | LE | **allocates** a new array; `packInt32Little(v)[0:1]` |
| `packInt16Little(v)` | 2 | LE | **allocates**; `packInt32Little(v)[0:2]` |
| `packInt24Little(v)` | 3 | LE | **allocates**; `packInt32Little(v)[0:3]` |
| `packInt32Little(long v)` | 4 | LE | **allocates**; pure Java, explicit LE |
| `byteMergeAll(byte[]...)` | — | — | concatenation |

The only difference between the `packIntNN` and `packIntNNLittle` families is
in-place-with-cursor vs. allocate-and-return. **Not** endianness.

In Python all of it collapses to `struct` with `<`:
`<B`, `<H`, `<I`, `<Q`.

---

## 2. Framing

`BaseReqPkgBean.packHead()`:

```
protocolType == 1                     3 bytes
  0    u8    protocolType = 1
  1    u16le requestType

protocolType == 2 or 3                5 bytes
  0    u8    protocolType
  1    u32le 0xFFFFFFFF
```

`enPkg()` = `packHead()` ++ per-request payload. Requests with no payload
(`SyncRecFileStopReq`) are the bare 3-byte head.

`BaseRspBleBean` / `BaseRspPkgBean` validate every inbound frame with
`readInt16(b, 1) == getBleConfirmType()`, so responses share the layout:

```
  0    u8    protocolType
  1    u16le confirmType
  3..  payload
```

**One exception.** `PreRSAHandShakeDataSyncReq` (65042) overrides `enPkg()` and
does *not* call `packHead()`. Its frame has no protocolType byte:
`[u16le 0xFE12][u8][u8][payload]`. This is the frame the companion note
mistook for the general form.

`protocolType` values (`BleProtocol.BleProtocolType`):

| Value | Name | Use |
|---|---|---|
| 1 | `TYPE_COMMAND` | all request/response |
| 2 | `TYPE_VOICE_PKG` | **recording data frames** |
| 3 | `TYPE_OTA_PKG` | firmware |
| 4 | `TYPE_TEST_PKG` | BLE rate test (req 101) |

### GATT and MTU

```
Service         00001910-0000-1000-8000-00805f9b34fb
  TX  0x2BB0    notify   — device → host   (subscribe here)
  RX  0x2BB1    write    — host → device   (write here)
  CCCD 0x2902
```

`BleProtocol.DEFAULT_MTU = 184`. `BluetoothLeOperation` calls
`requestMtu(184)` immediately after service discovery. ATT payload is therefore
181 bytes.

### ChaCha20 layer

`BluetoothLeOperation.sendData` wraps the frame in
ChaCha20-Poly1305 **only when `BleDevice.getPortVersion() >= 20** and the
key/nonce/AD triple has been negotiated ("NotePro device" in the log strings).
Inbound frames on TX are decrypted the same way, and after decryption the first
4 bytes are a **u32le replay counter** that is stripped before parsing
(`_internalCounterValue` must strictly increase). Below portVersion 20 the
frames are plaintext. See Unresolved.

---

## 3. Enumerating recordings — request 26

This is what "list the files" actually is. `BluetoothManager` does not expose
it; `IBleAgent.getRecSessions(long, …)` does.

### `GetRecSessionsReq` → req 26

```
  0    u8    protocolType = 1
  1    u16le 26
  3    u32le uid          — client-chosen tag, echoed back
  7    u32le sessionId    — SDK passes the caller's argument
  11   u8    flag         — SDK always passes 0 (false)
```

The SDK sets `uid = System.currentTimeMillis() / 1000` (`SessionsHelper.init`)
and uses it purely to correlate the multi-frame reply. The `sessionId`
argument's semantics are not pinned down anywhere in the SDK — see Unresolved.

### `GetRecSessionsRsp` ← confirm 26 (multi-frame)

The reply is **several notifications**, each a complete frame. The response
handler is not removed from the pending list on confirm 26, so it keeps
receiving until the host decides it has them all.

```
  0    u8    protocolType = 1
  1    u16le 26
  3    u32le uid            — must equal the uid you sent, else drop the frame
  7    u16le totalFiles     — same in every frame
  9    u16le startIndex     — index of the first entry in THIS frame
  11.. entries
```

`SessionsHelper` drops a frame whose `startIndex != len(collected_so_far)`,
i.e. frames must be consumed strictly in order. Enumeration is complete when
`len(collected) == totalFiles`.

Entry stride and layout depend on `portVersion` (from `HandShakeRsp`, byte 4,
u16le). Entry count in a frame = `(len(frame) - 11) // stride`.

| portVersion | stride | layout |
|---|---|---|
| `>= 7` | 10 | `+0 u32le sessionId`, `+4 u32le fileSize`, `+8 u8 scene`, `+9 u8 attribute` |
| `2..6` | 9 | `+0 u32le sessionId`, `+4 u32le fileSize`, `+8 u8 attribute` (scene = 0) |
| `< 2` | 8 | `+0 u32le sessionId`, `+4 u32le fileSize` |

The `scene`/`attribute` order for `>= 7` was double-checked in bytecode
(`javap -c` offsets 127–153): the byte at `+8` becomes the 4th `BleFile`
constructor argument (`getScene()`), the byte at `+9` the 3rd
(`getAttribute()`).

### What identifies a file — `BleFile`

**`sessionId` is the identity, and it is also the start time.** There is no
name, no path, no index.

| Accessor | Meaning |
|---|---|
| `getSessionId()` | u32 — unique id, and Unix epoch **seconds** of recording start |
| `getStartTime()` | `sessionId`, or `0` if `sessionId < 100` |
| `getFileSize()` | u32 — bytes of raw Opus payload |
| `getEndTime()` | `startTime + fileSize / 80 * 20 / 1000` seconds |
| `getAttribute()` | u8, meaning undocumented |
| `getScene()` | u8; `4` = music (`isMusic()`) |
| `isDeviceLog()` | `sessionId < 100` — pseudo-files, not audio |

```java
static long calculateOpusDuration(long size, int ch) { return size / (ch * 80L) * 20L; }  // ms
static long calculateOpusOffset  (long ms,   int ch) { return ms / 20L * 80L * ch; }       // bytes
```

**80 bytes = one 20 ms Opus packet per channel.** So byte offsets in this
protocol are exact and seekable: offset = `ms / 20 * 80 * channels`.

---

## 4. Downloading a recording — requests 28 / 29 / 30

### `SyncRecFileStartReq` → req 28

```
  0    u8    protocolType = 1
  1    u16le 28
  3    u32le sessionId
  7    u32le startOffset    — byte offset to resume from; 0 = beginning
  11   u32le endOffset      — 0 = to end of file / follow live
```

Parameter names come from the Kotlin `@Metadata` of
`BluetoothManager.syncHistoricalData(sessionId, startOffset, endOffset, …)`,
which passes them through `IBleAgent.syncFileStart(l, l2, l3)` to
`new SyncRecFileStartReq(l, l2, l3)` in order.
`startRealTimeFileSync` passes `endOffset = 0`.

Host registers interest in confirms **{28, 29}** before writing this. Voice
frames are dispatched to the handler registered for **29**, so if you drop the
29 registration the data stream is discarded
(`"🎵 File Sync Callback is Null"`).

### `SyncFileHeadRsp` ← confirm 28

```
  0    u8    protocolType = 1
  1    u16le 28
  3    u32le sessionId
  7    u8    status         — 0 = OK; > 0 = failure, abort the session
```

### Voice data frames ← protocolType 2

Not a "response" — raw notifications on TX with a different protocolType. Two
layouts, selected by `portVersion`:

```
portVersion >= 7                        portVersion < 7
  0    u8    protocolType = 2             0    u8    protocolType = 2
  1    u32le sessionId                    1    u32le fileOffset
  5    u32le fileOffset                   5    u8    payloadLen
  9    u8    payloadLen                   6..  payload
  10.. payload
```

Frames whose `sessionId` does not match the requested one are dropped
(`>= 7` only). `payloadLen` is clamped to the real frame length:
`if payloadLen + payloadStart > len(frame): payloadLen = len(frame) - payloadStart`.

Payload maximum is `181 - 10 = 171` bytes at MTU 184 **(inferred** — nothing in
the SDK states the device-side chunk size; the length byte permits 255).

### End-of-file frame

`fileOffset == 0xFFFFFFFF` (`BleProtocol.EMPTY_PKG`) marks the end. The status
byte sits one past the length byte:

```
  base+0  u32le 0xFFFFFFFF
  base+4  u8    payloadLen    (not read by the SDK; presumably 1)
  base+5  u8    status        -> ISyncVoiceDataKeepOut.finish(status)
```

where `base` is 5 for portVersion ≥ 7, 1 otherwise. The SDK never enumerates
`status` values; the only test is `status != 1` in the restart path, which
suggests **`1` = "stopped as you asked", anything else = a real end/abort
(inferred)**.

### `SyncFileTailRsp` ← confirm 29

```
  0    u8    protocolType = 1
  1    u16le 29
  3    u32le sessionId
  7    u16le crc            — CRC-16/CCITT-FALSE, seed presumed 0xFFFF
```

Fired when the device finishes the requested range. The SDK logs the CRC and
**never verifies it** — no call site compares it to anything. The algorithm and
seed for the tail CRC are therefore **(inferred)** from `tntGetCrc`; see
Unresolved.

`BluetoothManager` treats the tail as the completion signal:
`isSyncing = false; currentSyncSessionId = null; onComplete()`.

### `SyncRecFileStopReq` → req 29 / `SyncRecFileStopRsp` ← confirm 30

```
Req:  0 u8 1 | 1 u16le 29                (3 bytes, no payload)
Rsp:  0 u8 1 | 1 u16le 30                (payload unread by the SDK)
```

Aborts the current stream. Used to stop a live sync and, internally, as the
first half of every resume.

### `SyncRecFileDelReq` → req 30 / `SyncRecFileDelRsp` ← confirm 31

```
Req:  0 u8 1 | 1 u16le 30 | 3 u32le sessionId

Rsp (portVersion >= 7):  3 u32le sessionId | 7 u8 status
Rsp (portVersion  < 7):  3 u8 status
```

Deletes one recording. `ClearRecordFileReq` (104) wipes everything.

---

## 5. Chunking, flow control, resume

There is **no per-chunk ACK**. Once request 28 is accepted the device pushes
notifications as fast as the link allows. Flow control is entirely
*loss-detection plus restart*.

The host keeps one variable, `recvOffset`, initialised to the `startOffset` it
requested. `BetaComponentHandler.onCallback` for each data frame:

```
delta = frame.fileOffset - recvOffset

delta == 0   accept:  payload = frame[base+5 : base+5+payloadLen]
                      recvOffset += payloadLen
                      emit(payload, frame.fileOffset)
                      lossPending = False

delta  > 0   gap — packets were lost:
                if not lossPending:
                    lossPending = True
                    send SyncRecFileStopReq (29)
                    arm a 5000 ms watchdog
                    on stop-confirmed (or watchdog expiry):
                        send SyncRecFileStartReq(sessionId, recvOffset, endOffset)

delta  < 0   duplicate / stale — silently ignored
```

So **resume-from-offset and loss-recovery are the same mechanism**: stop, then
re-issue request 28 with `startOffset = recvOffset`. Restart is idempotent;
`recvOffset` is never rewound.

Two details that matter if you reimplement the retry:

- The 5000 ms watchdog (`get_display_metrics = 5000L`, decremented in 10 ms
  sleeps) fires the restart even if `SyncRecFileStopRsp` never arrives. It is
  re-armed to 5000 ms on every further gap frame.
- A restart is also triggered by receiving confirm 29 (tail) or an
  `0xFFFFFFFF` frame **while a stop is outstanding** — the device answers the
  stop by ending the stream, and the host immediately reopens at `recvOffset`.

### Completion

Completion is signalled twice and you should accept either:

1. a data frame with `fileOffset == 0xFFFFFFFF` → `finish(status)`;
2. confirm **29** `SyncFileTailRsp` → `onComplete()`.

A simpler and independent test that does not depend on either:
`recvOffset - startOffset == BleFile.getFileSize()`.

---

## 6. Audio format

The wire carries **bare Opus packets, no container**. The SDK builds the Ogg
itself:

```java
OggUtils.init(path, channels)  ->  native init(path, 16000, channels, 320)
MiaplacidusDisplayCoordinator: frameSize = channels * 80
   // buffers the BLE byte stream, slices it into frameSize chunks,
   // feeds each chunk to OggUtils.putPkg() as ONE Opus packet
```

| Parameter | Value |
|---|---|
| Codec | Opus |
| Sample rate | 16000 Hz |
| Frame size | 320 samples = 20 ms |
| Packet size | 80 bytes × channels — **constant** |
| Bitrate | 32 kbit/s per channel |
| Channels | `HandShakeRsp` byte 8 (`getAudioChannel()`), default 1 |

Because packets are fixed-size, the received byte stream is self-delimiting:
slice it into `80 * channels` chunks in arrival order. No page headers, no
lacing, no timestamps on the wire — position comes from the frame's
`fileOffset`.

To produce a playable file, mux with `ogg`/`libopus` (`OpusHead` with
`input_sample_rate = 16000`, `pre_skip = 0`, channel count from the handshake;
granulepos advancing 320 per packet). `ffmpeg`/`faster-whisper` will read the
result directly.

The `PlaudEncryptHeader` / `AudioDecryptor` classes (512-byte `PLAUD.AI` header,
E2EE) are referenced only from `sdk/audio/AudioExporter` — the file-import path,
not the BLE receive path. Nothing in `BetaComponentHandler → ISyncVoiceDataKeepOut
→ OggUtils` decrypts. **(Inferred: BLE-delivered audio is plaintext Opus.)**

---

## 7. The other direction — 112 / 114 / 116

Documented so nobody wires it up backwards. This is **host → device**: the app
pushes a blob (the SDK uses it for an HTTP token) and the *device* drives the
pacing. Confirm names give it away: `STICK_GET_FILE_DATA_REQ = 113` is the
device *asking* for data.

```
FileInfoSyncReq   -> req 112:  3 u8 fileType | 4 u32le totalSize
FileDataSyncReq   -> req 114:  3 u8 fileType | 4 u32le offset | 8 u16le size | 10.. payload
FileDataCheckReq  -> req 116:  3 u8 fileType | 4 u16le crc16

FileInfoSyncRsp   <- confirm 113 (answers BOTH 112 and 114):
   3 u8    fileType
   4 u32le offset      — where the device wants the next slice
   8 u16le size        — how many bytes it wants
   10 u8   finishedFlag
   isFinished = (finishedFlag == 1) || (size <= 0)

FileDataCheckRsp  <- confirm 117:  3 u8 result
```

The loop, from `NiceBuildSdk$sendHttpTokenToDevice$1.invokeSuspend` bytecode:

```
rsp = send(112, type=1, totalSize=len(blob))
while not rsp.isFinished:
    chunk = blob[rsp.offset : rsp.offset + rsp.size]      # bounds-checked
    rsp = send(114, type=1, rsp.offset, rsp.size, chunk)
send(116, type=1, tnt_crc16(blob, 0xFFFF))
```

---

## 8. Python sketch (`bleak`)

Correct about the bytes; not run against hardware.

```python
import asyncio, struct
from bleak import BleakClient

SERVICE = "00001910-0000-1000-8000-00805f9b34fb"
TX      = "00002bb0-0000-1000-8000-00805f9b34fb"   # notify
RX      = "00002bb1-0000-1000-8000-00805f9b34fb"   # write

REQ_GET_REC_SESSIONS  = 26
REQ_SYNC_FILE_START   = 28
REQ_SYNC_FILE_STOP    = 29
REQ_SYNC_FILE_DEL     = 30
CNF_GET_REC_SESSIONS  = 26
CNF_SYNC_FILE_HEAD    = 28
CNF_SYNC_FILE_TAIL    = 29
EMPTY_PKG             = 0xFFFFFFFF

def head(req_type: int) -> bytes:
    return struct.pack("<BH", 1, req_type)          # u8 protoType, u16le reqType


class Pin:
    def __init__(self, client, port_version, channels=1):
        self.c   = client
        self.pv  = port_version                     # HandShakeRsp[4:6] u16le
        self.ch  = channels                         # HandShakeRsp[8]
        self.q   = asyncio.Queue()                  # protocolType-1 frames
        self.vq  = asyncio.Queue()                  # protocolType-2 frames

    def on_notify(self, _sender, data: bytearray):
        b = bytes(data)
        if not b:
            return
        if b[0] == 1:
            self.q.put_nowait(b)
        elif b[0] == 2:
            self.vq.put_nowait(b)

    async def write(self, frame: bytes):
        # write-with-response; the SDK uses WRITE_TYPE_DEFAULT for commands
        await self.c.write_gatt_char(RX, frame, response=True)

    async def await_cnf(self, cnf: int, timeout=5.0):
        while True:
            b = await asyncio.wait_for(self.q.get(), timeout)
            if struct.unpack_from("<H", b, 1)[0] == cnf:
                return b

    # ---- list recordings -------------------------------------------------
    async def list_files(self, uid: int, session_id: int = 0):
        await self.write(head(REQ_GET_REC_SESSIONS)
                         + struct.pack("<IIB", uid, session_id, 0))

        stride = 10 if self.pv >= 7 else (9 if self.pv >= 2 else 8)
        files, total = [], None
        while total is None or len(files) < total:
            b = await self.await_cnf(CNF_GET_REC_SESSIONS)
            if struct.unpack_from("<I", b, 3)[0] != uid:
                continue                                   # not ours
            total = struct.unpack_from("<H", b, 7)[0]
            start = struct.unpack_from("<H", b, 9)[0]
            if start != len(files):
                continue                                   # out of order: drop
            for off in range(11, len(b) - stride + 1, stride):
                sid, size = struct.unpack_from("<II", b, off)
                scene = b[off + 8] if stride == 10 else 0
                attr  = b[off + 9] if stride == 10 else (b[off + 8] if stride == 9 else 0)
                files.append(dict(session_id=sid, size=size,
                                  scene=scene, attribute=attr,
                                  start_epoch=sid if sid >= 100 else 0,
                                  duration_ms=size // (self.ch * 80) * 20))
        return files

    # ---- download one recording -----------------------------------------
    async def download(self, session_id: int, size: int,
                       start_offset: int = 0, end_offset: int = 0):
        base   = 5 if self.pv >= 7 else 1
        out    = bytearray()
        recv   = start_offset
        loss   = False

        async def start(from_off):
            await self.write(head(REQ_SYNC_FILE_START)
                             + struct.pack("<III", session_id, from_off, end_offset))
            h = await self.await_cnf(CNF_SYNC_FILE_HEAD)
            status = h[7]
            if status != 0:
                raise RuntimeError(f"SyncFileHead status={status}")

        await start(recv)

        while recv - start_offset < size:
            try:
                f = await asyncio.wait_for(self.vq.get(), 5.0)
            except asyncio.TimeoutError:
                await self.write(head(REQ_SYNC_FILE_STOP))
                await start(recv)                          # watchdog restart
                loss = False
                continue

            if self.pv >= 7 and struct.unpack_from("<I", f, 1)[0] != session_id:
                continue

            off = struct.unpack_from("<I", f, base)[0]
            if off == EMPTY_PKG:
                break                                      # status at f[base+5]

            if off > recv:
                if not loss:
                    loss = True
                    await self.write(head(REQ_SYNC_FILE_STOP))
                    await start(recv)
                    loss = False
                continue
            if off < recv:
                continue                                   # stale duplicate

            n = f[base + 4]
            p = base + 5
            n = min(n, len(f) - p)
            out += f[p:p + n]
            recv += n
            loss = False

        # tail (confirm 29) carries a u16le CRC the SDK never checks
        return bytes(out)


def to_opus_packets(raw: bytes, channels: int = 1):
    """Wire bytes -> list of Opus packets. Fixed 80*channels per 20 ms."""
    n = 80 * channels
    return [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]
```

Notes for whoever implements this:

- Subscribe to TX *before* writing anything.
- Negotiate MTU 184 if your stack allows it (`bleak` on BlueZ negotiates
  automatically; you can read `client.mtu_size`).
- The handshake (req 1) must complete first — you need `portVersion` before you
  can parse either the session list entries or the voice frames.
- Ordering: `list_files` → pick a `session_id` → `download` → optionally
  `SyncRecFileDelReq`. Nothing else is required between steps; the app waits
  only for `SyncFileHeadRsp.status == 0` before expecting data.

---

## 9. Unresolved

Listed plainly. Every one of these can silently corrupt audio if guessed wrong.

1. ~~**`portVersion` of the NotePin S.**~~ **RESOLVED 2026-07-31: it is 20.**
   Read passively from the advertisement's manufacturer-specific data, not from
   `HandShakeRsp` — `PkgUtils.convertManufacturerSpecificData2BleDevice()`
   parses it there, and `BleAgentImpl.connectionBLE()` feeds that value into
   `HandShakeReq`, so the SDK knows it before writing anything. Byte 15 of the
   manufacturer blob, single byte, **not** a u16: the u16 reading yields 5188 and
   the SDK ships an explicit `i4 == 5188` error branch calling that a parse
   failure, alongside an `i4 == 20` success branch. See `docs/transport-tests.md`
   T8 for the full decode and both cross-checks.

   Consequences: entry stride 10 and a 5-byte voice prefix (both `>= 7`), and
   **ChaCha20 applies** — see item 2, which is now live rather than conditional.

2. **ChaCha20-Poly1305 key exchange — now the primary blocker.** Item 1 resolved
   `portVersion = 20`, so this is no longer an "if": every frame in both
   directions is encrypted and prefixed (post-decrypt) with a u32le replay
   counter, on *this* hardware. The key exchange is still undecoded
   (`BleGattCallback.process_item_data(byte[])`, `SecretUtil
   .encryptWithChaChaPoly1305Separate`); the key/nonce/AD statics are assigned
   from a path nobody has traced. It also stacks on the RSA pre-handshake, which
   `>= 20` makes mandatory and which needs a B2B partner key.

   Empirically confirmed: a plaintext req-1 handshake draws **no reply on any
   characteristic**, across eight `portVersion` candidates (T7). The frame itself
   was correct — `HandShakeReq.enPkg()` matches it byte-for-byte — so the silence
   is the encryption and sequencing, not the layout.

3. ~~**`GetRecSessionsReq` field 2 (`sessionId`, offset 7).**~~ **RESOLVED
   2026-07-29.** It is a start cursor and **`0` means "all"**. Confirmed from
   the vendor's own iOS reference app, which the earlier run could not fetch:
   `plaud-template-app/ios/PlaudTemplateApp/Managers/SyncManager.swift` calls
   `PlaudDeviceAgent.shared.getFileList(startSessionId: 0)` at both call sites
   (lines 76, 84). The same file independently corroborates that `sessionId`
   is a Unix timestamp — line 182:
   `Date(timeIntervalSince1970: Double(bf.sessionId))`.

4. **Tail CRC definition.** `SyncFileTailRsp.getCrc()` is a u16 that the SDK
   logs and discards. That it is `tntGetCrc` with seed `0xFFFF` over the file
   payload is an assumption by analogy with the 116 path — the seed, the byte
   range (whole file? requested range only?), and whether it covers pre-Ogg
   bytes are all unverified. Do not gate a download on it; use
   `recvOffset - startOffset == fileSize` instead.

5. **EOF `status` byte values.** Only `status != 1` is ever tested. The
   mapping is otherwise unknown. Tried: grepped for `finish(` implementations —
   all four just log the integer.

6. **`BleFile.attribute` semantics.** Never read anywhere in the SDK. `scene`
   is partially known (`4` = music). Unknown whether `attribute` flags
   encryption, pause markers, or something else. If it turns out to flag E2EE,
   item 7 becomes live.

7. **Whether any recording can arrive E2EE-encrypted over BLE.** The
   `PLAUD.AI` 512-byte header exists and `NiceBuildSdk.isE2eeEncryptedFile`
   is public API, but the BLE receive path never checks for it. A cheap
   defensive check on first download: if the first 8 bytes of the assembled
   stream are `PLAUD.AI`, stop and reassess.

8. **Device-side chunk size.** The 171-byte figure is derived from
   MTU 184 minus the 10-byte header. The device may use something smaller and
   fixed. Harmless — `payloadLen` is authoritative — but it affects throughput
   estimates.

9. **Whether request 28 works on an unbound device**, and whether a handshake
   is a hard precondition for 26/28. Not answerable without hardware. Same
   caveat as the companion note: binding to a custom client requires unbinding
   from the Plaud app.

10. **`SyncRecFileStopRsp` payload.** The class parses nothing past the header,
    so if the device returns a status there, the SDK ignores it. Unknown.
