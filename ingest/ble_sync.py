#!/usr/bin/env python3
"""Direct BLE sync from a Plaud NotePin S — no Plaud Cloud, no phone.

RESEARCH ONLY — THIS CANNOT WORK ON THE NOTEPIN S, AND IS WIRED INTO NOTHING.
===========================================================================
Kept as reference, not as a working transport. The pin reports
``portVersion = 20``, which means its firmware requires an RSA pre-handshake
keyed by a credential Plaud issues under a B2B agreement, and wraps every frame
in ChaCha20-Poly1305 whose key exchange is undecoded. Neither is implemented
here and neither can be without that key.

Run against real hardware, ``pull --go`` draws **no reply on any
characteristic** — see docs/transport-tests.md T7. The frame this builds is
byte-for-byte correct (confirmed against ``HandShakeReq.enPkg()``); it is simply
plaintext and out of sequence, and silence is the firmware's correct response.

``PV_GUESS = 7`` and ``PV_CANDIDATES`` below are therefore both wrong for this
device and are left as-is rather than "fixed", because no value of them helps:
portVersion is not something to guess. It is readable from the advertisement's
manufacturer data (byte 15, single byte). See
docs/decisions/ADR-005-direct-device-access-is-closed.md.


    ./ble_sync.py init-token                 generate + save the client token
    ./ble_sync.py handshake                   dry run: show the frame, send nothing
    ./ble_sync.py handshake --go              handshake; report portVersion/channels
    ./ble_sync.py list --go                   handshake + enumerate recordings
    ./ble_sync.py pull --go                   download everything to --out
    ./ble_sync.py pull --session ID --go      download one recording

**Writes nothing to the device without `--go`.** Without it every command
prints the exact frames it would send and exits, because the first successful
handshake may bind the pin to this client and unbind it from the official Plaud
iPhone app. Devices bind to one client at a time. Read
docs/decisions/ble-hardware-findings.md §9 before using --go.

Never sends: DepairReq (5), record control (20-23), ClearRecordFile (104),
firmware OTA (50/51), or the host->device upload path (112/114/116). Deleting
recordings from the pin (req 30) is deliberately not implemented.

Protocol: docs/decisions/ble-file-transfer.md (byte layouts, the authority where
it disagrees with ble-protocol-notes.md) and ble-hardware-findings.md §8
(handshake payload, which neither companion document had).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import struct
import sys
import time
import uuid

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("bleak not installed:  pip install bleak")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ogg_opus

NAME_MATCH = "notepin"
TX = "00002bb0-0000-1000-8000-00805f9b34fb"   # notify: device -> us
RX = "00002bb1-0000-1000-8000-00805f9b34fb"   # write:  us -> device

REQ_HANDSHAKE, REQ_LIST = 1, 26
REQ_SYNC_START, REQ_SYNC_STOP = 28, 29
CNF_HANDSHAKE, CNF_LIST = 1, 26
CNF_SYNC_HEAD, CNF_SYNC_TAIL = 28, 29
EMPTY_PKG = 0xFFFFFFFF

TOKEN_PATH = pathlib.Path.home() / ".config" / "atticus" / "ble-token"

# portVersion selects four different wire layouts and is only authoritative from
# HandShakeRsp — but the handshake frame itself depends on it, so we must guess
# first. 7 is the best available hypothesis (see ble-hardware-findings.md §1);
# if it draws no reply we retry the other widths.
PV_GUESS = 7
PV_CANDIDATES = (7, 9, 2)


# ---------------------------------------------------------------- token


def load_token(cli_token: str | None) -> str:
    """The handshake identifier. Stable, self-chosen, not a credential.

    It is the `deviceToken` of the official SDK — the `sub` claim of a partner
    JWT there, and a random UUID in the vendor's own template app when no JWT
    exists (ble-hardware-findings.md §8). The pin cannot verify it. It only has
    to stay the *same* across reconnects so the pin recognises us, which is why
    this is persisted rather than generated per run.
    """
    if cli_token:
        return cli_token
    if os.environ.get("ATTICUS_BLE_TOKEN"):
        return os.environ["ATTICUS_BLE_TOKEN"]
    if TOKEN_PATH.exists():
        t = TOKEN_PATH.read_text().strip()
        if t:
            return t
    sys.exit(
        "No client token. It must be STABLE across runs — a fresh one each time\n"
        "risks re-binding the pin on every connection.\n\n"
        "    ./ble_sync.py init-token\n\n"
        f"writes one to {TOKEN_PATH}. Or pass --token / set ATTICUS_BLE_TOKEN."
    )


def init_token() -> None:
    if TOKEN_PATH.exists():
        print(f"Token already exists at {TOKEN_PATH}:\n  {TOKEN_PATH.read_text().strip()}")
        print("Delete it first if you really want a new one (the pin knows the old one).")
        return
    # 32 hex chars: the max width the handshake accepts, so it survives both the
    # 16- and 32-char truncation paths without changing meaning at 32.
    token = uuid.uuid4().hex
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token + "\n")
    TOKEN_PATH.chmod(0o600)
    print(f"Wrote {TOKEN_PATH}\n  {token}\n\nKeep it. Changing it looks like a new client to the pin.")


# ---------------------------------------------------------------- frames


def head(req: int) -> bytes:
    return struct.pack("<BH", 1, req)


def handshake_frame(token: str, pv: int) -> bytes:
    """req 1. Layout from ble-hardware-findings.md §8."""
    f = bytearray(head(REQ_HANDSHAKE))
    f.append(2)                       # constant, hardcoded in the SDK's enPkg()
    f.append(0)                       # arg1 — SDK passes 0
    if pv >= 3:
        f.append(0)                   # arg2 — only present at pv >= 3
    width = 32 if pv >= 9 else 16
    f += token[:width].ljust(width, "0").encode("ascii")
    return bytes(f)


def list_frame(uid: int, start_session: int = 0) -> bytes:
    # start_session 0 == "all" (ble-file-transfer.md §9 item 3, RESOLVED)
    return head(REQ_LIST) + struct.pack("<IIB", uid, start_session, 0)


def sync_start_frame(sid: int, start_off: int, end_off: int = 0) -> bytes:
    return head(REQ_SYNC_START) + struct.pack("<III", sid, start_off, end_off)


def entry_stride(pv: int) -> int:
    return 10 if pv >= 7 else (9 if pv >= 2 else 8)


def voice_base(pv: int) -> int:
    """Offset of fileOffset inside a protocolType-2 frame."""
    return 5 if pv >= 7 else 1


# ---------------------------------------------------------------- client


class Pin:
    def __init__(self, client, pv: int = PV_GUESS, channels: int = 1):
        self.c = client
        self.pv = pv
        self.channels = channels
        self.cmd: asyncio.Queue[bytes] = asyncio.Queue()
        self.voice: asyncio.Queue[bytes] = asyncio.Queue()

    def on_notify(self, _sender, data: bytearray) -> None:
        b = bytes(data)
        if not b:
            return
        if b[0] == 1:
            self.cmd.put_nowait(b)
        elif b[0] == 2:
            self.voice.put_nowait(b)
        # protocolType 3 (OTA) and 4 (rate test) are not ours; drop them.

    async def write(self, frame: bytes) -> None:
        await self.c.write_gatt_char(RX, frame, response=True)

    async def await_cnf(self, cnf: int, timeout: float = 5.0) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no confirm {cnf} within {timeout}s")
            b = await asyncio.wait_for(self.cmd.get(), left)
            if len(b) >= 3 and struct.unpack_from("<H", b, 1)[0] == cnf:
                return b

    # -------------------------------------------------- handshake

    async def handshake(self, token: str, candidates=PV_CANDIDATES) -> tuple[int, int]:
        """Try each candidate portVersion until one draws a HandShakeRsp.

        Returns (portVersion, channels) as reported by the DEVICE, which
        overrides whichever guess got us in.
        """
        for pv in candidates:
            frame = handshake_frame(token, pv)
            print(f"  → handshake (assuming pv={pv}, {len(frame)}B): {frame.hex(' ')}")
            await self.write(frame)
            try:
                rsp = await self.await_cnf(CNF_HANDSHAKE, timeout=5.0)
            except TimeoutError:
                print(f"    no reply for pv={pv}")
                continue
            print(f"  ← HandShakeRsp {len(rsp)}B: {rsp.hex(' ')}")
            if len(rsp) < 9:
                print(f"    ! only {len(rsp)}B — too short for portVersion/channels")
                continue
            self.pv = struct.unpack_from("<H", rsp, 4)[0]
            self.channels = rsp[8] or 1
            if self.pv != pv:
                print(f"    note: guessed pv={pv}, device reports pv={self.pv}")
            return self.pv, self.channels
        raise RuntimeError(
            "No handshake reply for any candidate portVersion "
            f"{candidates}. The frame layout or the token width is wrong."
        )

    # -------------------------------------------------- listing

    async def list_files(self, timeout: float = 10.0) -> list[dict]:
        uid = int(time.time()) & 0xFFFFFFFF
        await self.write(list_frame(uid))
        stride = entry_stride(self.pv)
        files: list[dict] = []
        total: int | None = None
        deadline = time.monotonic() + timeout

        while total is None or len(files) < total:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(
                    f"listing incomplete: {len(files)}/{total} after {timeout}s")
            b = await self.await_cnf(CNF_LIST, timeout=left)
            if len(b) < 11:
                continue
            got_uid, tot, start = struct.unpack_from("<IHH", b, 3)
            if got_uid != uid:
                continue                              # someone else's reply
            total = tot
            if start != len(files):
                continue                              # must consume strictly in order
            for off in range(11, len(b) - stride + 1, stride):
                sid, size = struct.unpack_from("<II", b, off)
                scene = b[off + 8] if stride == 10 else 0
                attr = b[off + 9] if stride == 10 else (b[off + 8] if stride == 9 else 0)
                files.append({
                    "session_id": sid,
                    "size": size,
                    "scene": scene,
                    "attribute": attr,
                    # sessionId doubles as the Unix start time; < 100 means a
                    # device log pseudo-file, not audio.
                    "is_log": sid < 100,
                    "start_epoch": sid if sid >= 100 else 0,
                    "duration_ms": size // (self.channels * 80) * 20 if size else 0,
                })
        return files

    # -------------------------------------------------- download

    async def download(self, sid: int, size: int, progress=None) -> bytes:
        """Pull one recording. Handles gaps and disconnect-free stalls by
        stop-and-restart at recvOffset, per ble-file-transfer.md §5."""
        base = voice_base(self.pv)
        out = bytearray()
        recv = 0

        async def start(from_off: int) -> None:
            await self.write(sync_start_frame(sid, from_off))
            h = await self.await_cnf(CNF_SYNC_HEAD, timeout=8.0)
            status = h[7] if len(h) > 7 else 0xFF
            if status != 0:
                raise RuntimeError(f"SyncFileHead status={status} for session {sid}")

        await start(recv)
        restarts = 0
        # Mirrors the SDK's lossPending. Without it, every in-flight frame that
        # arrives after a gap triggers another stop/restart, and the transfer
        # livelocks instead of recovering.
        loss_pending = False

        async def restart() -> None:
            nonlocal restarts, loss_pending
            restarts += 1
            if restarts > 50:
                raise RuntimeError(f"gave up after {restarts} restarts at {recv}/{size}B")
            loss_pending = True
            await self.write(head(REQ_SYNC_STOP))
            await asyncio.sleep(0.1)
            await start(recv)

        while recv < size:
            try:
                f = await asyncio.wait_for(self.voice.get(), 5.0)
            except TimeoutError:
                await restart()                       # watchdog
                continue

            if len(f) < base + 5:
                continue
            if self.pv >= 7 and struct.unpack_from("<I", f, 1)[0] != sid:
                continue                              # a different session's frame

            off = struct.unpack_from("<I", f, base)[0]

            if off == EMPTY_PKG:
                # End-of-stream — but the device also ends the stream to answer
                # our own stop. Treating that as completion truncates the file
                # at the gap. Only believe it when we did not ask for it.
                if loss_pending:
                    continue
                break

            if off > recv:                            # gap: packets were lost
                if not loss_pending:
                    await restart()
                continue
            if off < recv:
                continue                              # stale duplicate

            n = f[base + 4]
            p = base + 5
            n = min(n, len(f) - p)                    # payloadLen is clamped to reality
            out += f[p:p + n]
            recv += n
            loss_pending = False                      # we are back in sync
            if progress:
                progress(recv, size)

        if len(out) != size:
            print(f"  ! got {len(out)}B, expected {size}B", file=sys.stderr)
        if out[:8] == b"PLAUD.AI":
            raise RuntimeError(
                "stream starts with the PLAUD.AI E2EE header — encrypted audio, "
                "stop and reassess (ble-file-transfer.md §9 item 7)")
        return bytes(out)


# ---------------------------------------------------------------- plumbing


async def find_pin(seconds: float):
    print(f"Scanning {seconds:.0f}s for {NAME_MATCH!r} …")
    dev = await BleakScanner.find_device_by_filter(
        lambda d, adv: NAME_MATCH in ((adv.local_name or d.name or "").lower()),
        timeout=seconds)
    if dev is None:
        sys.exit("Pin not found. Powered? In range?")
    # The address is a rotating resolvable private address — never persist it.
    print(f"Found {dev.address}  {dev.name}")
    return dev


async def real_mtu(client) -> int:
    """True negotiated ATT MTU. `client.mtu_size` is a hardcoded 23 until this
    runs — see ble-hardware-findings.md §6."""
    backend = getattr(client, "_backend", None)
    acquire = getattr(backend, "_acquire_mtu", None)
    if acquire is not None and getattr(backend, "_mtu_size", None) is None:
        try:
            await acquire()
        except Exception:
            pass
    return client.mtu_size


def describe(f: dict) -> str:
    when = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f["start_epoch"]))
            if f["start_epoch"] else "unknown time")
    secs = f["duration_ms"] / 1000
    tags = []
    if f["is_log"]:
        tags.append("device-log")
    if f["scene"] == 4:
        tags.append("music")
    suffix = f"  [{', '.join(tags)}]" if tags else ""
    return f"{f['session_id']:<12} {when}  {secs:7.1f}s  {f['size']:>9}B{suffix}"


def stamp(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime(epoch)) if epoch else "unknown"


async def run(args) -> int:
    token = load_token(args.token)

    if not args.go:
        pv = args.port_version
        print("DRY RUN — nothing will be written to the pin. Add --go to act.\n")
        print(f"token      {token[:8]}…{token[-4:]}  ({len(token)} chars)")
        print(f"assumed pv {pv}  -> token width {32 if pv >= 9 else 16}, "
              f"entry stride {entry_stride(pv)}, voice prefix {voice_base(pv)}B")
        print(f"\nframes that WOULD be sent for '{args.cmd}':")
        print(f"  handshake     {handshake_frame(token, pv).hex(' ')}")
        if args.cmd in ("list", "pull"):
            print(f"  list (req 26) {list_frame(0x5A5A0001).hex(' ')}")
        if args.cmd == "pull":
            print(f"  start(req 28) {sync_start_frame(args.session or 0, 0).hex(' ')}")
        print("\nWhy this is gated: a successful handshake may bind the pin to this")
        print("client and unbind it from the Plaud iPhone app. One client at a time.")
        return 0

    dev = await find_pin(args.seconds)
    async with BleakClient(dev, timeout=30.0) as c:
        mtu = await real_mtu(c)
        print(f"Connected. ATT MTU {mtu} (payload {mtu - 3}B)")
        pin = Pin(c, pv=args.port_version)
        await c.start_notify(TX, pin.on_notify)      # subscribe BEFORE writing

        pv, ch = await pin.handshake(token)
        print(f"\n✓ handshake OK — portVersion={pv}, channels={ch}")
        print(f"  -> entry stride {entry_stride(pv)}, voice prefix {voice_base(pv)}B, "
              f"{'ChaCha20 APPLIES — frames are encrypted' if pv >= 20 else 'plaintext frames'}")
        if pv >= 20:
            print("  ! portVersion >= 20: the ChaCha20 key exchange is undecoded.\n"
                  "    Everything below will fail. See ble-file-transfer.md §9 item 2.",
                  file=sys.stderr)
        if args.cmd == "handshake":
            return 0

        files = await pin.list_files()
        audio = [f for f in files if not f["is_log"]]
        print(f"\n{len(files)} entr{'y' if len(files) == 1 else 'ies'} "
              f"({len(audio)} audio, {len(files) - len(audio)} device-log)\n")
        print(f"{'sessionId':<12} {'start (local)':<21}{'dur':>8}  {'size':>9}")
        for f in files:
            print(describe(f))
        if args.cmd == "list":
            return 0

        targets = [f for f in audio if args.session in (None, f["session_id"])]
        if not targets:
            print(f"\nNo audio recording matching session {args.session}.", file=sys.stderr)
            return 1

        outdir = pathlib.Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)
        print()
        failures = 0
        for f in targets:
            sid, size = f["session_id"], f["size"]
            name = f"{stamp(f['start_epoch'])}_{sid}"
            dest = outdir / f"{name}.opus"
            if dest.exists() and not args.force:
                print(f"skip  {dest.name} (exists; --force to overwrite)")
                continue
            print(f"pull  {sid}  {size}B …", end="", flush=True)
            t0 = time.monotonic()
            try:
                raw = await pin.download(sid, size)
            except Exception as e:
                print(f"\n  ✗ {type(e).__name__}: {e}", file=sys.stderr)
                failures += 1
                continue
            dt = max(time.monotonic() - t0, 1e-6)
            packets = ogg_opus.split_packets(raw, ch)
            written = ogg_opus.write_ogg_opus(dest, packets, channels=ch)
            if args.keep_raw:
                (outdir / f"{name}.raw.opuspackets").write_bytes(raw)
            print(f" {len(raw)}B in {dt:.1f}s ({len(raw)/dt/1024:.1f} KB/s)"
                  f" -> {dest.name} ({written}B, {ogg_opus.duration_ms(len(packets))/1000:.1f}s)")
        return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Direct BLE sync from a Plaud NotePin S.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Protocol:")[0])
    ap.add_argument("cmd", choices=["init-token", "handshake", "list", "pull"])
    ap.add_argument("--go", action="store_true",
                    help="actually write to the device (default is a dry run)")
    ap.add_argument("--token", help="client identifier; default from config/env")
    ap.add_argument("--session", type=int, help="pull only this sessionId")
    ap.add_argument("--out", default="./pulled", help="output dir for pull")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    ap.add_argument("--keep-raw", action="store_true",
                    help="also write the undecoded Opus packet stream")
    ap.add_argument("--port-version", type=int, default=PV_GUESS,
                    help=f"portVersion to assume for the handshake (default {PV_GUESS})")
    ap.add_argument("--seconds", type=float, default=20.0, help="scan duration")
    args = ap.parse_args()

    if args.cmd == "init-token":
        init_token()
        return 0
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
