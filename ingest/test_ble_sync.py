#!/usr/bin/env python3
"""Frame-level tests for ble_sync against a simulated pin.

    ./test_ble_sync.py

No hardware and no bleak connection: a fake client captures writes and scripts
the notifications a device would send back. This exercises the paths that are
hardest to reach on real hardware and most damaging if wrong — packet loss,
resume-at-offset, and the end-of-stream frame that arrives in answer to our own
stop request.
"""
from __future__ import annotations

import asyncio
import pathlib
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ble_sync
from ble_sync import Pin


class FakePin:
    """Stands in for BleakClient. Replies to req 28 and records every write."""

    def __init__(self, pin: Pin, sid: int, payload: bytes, script):
        self.pin = pin
        self.sid = sid
        self.payload = payload
        self.script = script          # list of (kind, ...) directives per req-28
        self.writes: list[bytes] = []
        self.starts: list[int] = []   # startOffset of each req 28
        self.stops = 0

    async def write_gatt_char(self, _uuid, frame, response=True):
        self.writes.append(frame)
        req = struct.unpack_from("<H", frame, 1)[0]
        if req == ble_sync.REQ_SYNC_STOP:
            self.stops += 1
            return
        if req != ble_sync.REQ_SYNC_START:
            return
        sid, start, _end = struct.unpack_from("<III", frame, 3)
        self.starts.append(start)
        # SyncFileHeadRsp: confirm 28, status 0
        self.pin.cmd.put_nowait(struct.pack("<BHIB", 1, ble_sync.CNF_SYNC_HEAD, sid, 0))
        directives = self.script[len(self.starts) - 1] if len(self.starts) <= len(self.script) else ["stream"]
        for d in directives:
            if d == "stream":
                self._stream(start)
            elif d == "gap":
                self._gap(start)
            elif d == "empty":
                self._empty()
            elif d == "dup":
                self._dup(start)

    # -- frame builders (portVersion >= 7 layout: 5-byte prefix) --
    def _voice(self, off: int, chunk: bytes) -> bytes:
        return struct.pack("<BII B", 2, self.sid, off, len(chunk))[:10] + chunk

    def _stream(self, start: int, step: int = 160):
        off = start
        while off < len(self.payload):
            chunk = self.payload[off:off + step]
            self.pin.voice.put_nowait(self._voice(off, chunk))
            off += len(chunk)
        self.pin.voice.put_nowait(self._voice(0xFFFFFFFF, b"\x00"))

    def _gap(self, start: int, step: int = 160):
        """One good frame, then several already-in-flight frames past the gap.

        More than one post-gap frame matters: with a single one, a client that
        restarts on *every* gap frame is indistinguishable from one that
        restarts once. Three of them make the missing lossPending guard show up
        as three stops and three redundant req-28s.
        """
        self.pin.voice.put_nowait(self._voice(start, self.payload[start:start + step]))
        for i in range(3, 6):
            off = start + step * i
            self.pin.voice.put_nowait(self._voice(off, self.payload[off:off + step]))

    def _empty(self):
        self.pin.voice.put_nowait(self._voice(0xFFFFFFFF, b"\x00"))

    def _dup(self, start: int, step: int = 160):
        self.pin.voice.put_nowait(self._voice(start, self.payload[start:start + step]))
        self.pin.voice.put_nowait(self._voice(start, self.payload[start:start + step]))
        self._stream(start + step)


def make(payload: bytes, script):
    pin = Pin.__new__(Pin)
    pin.pv, pin.channels = 7, 1
    pin.cmd, pin.voice = asyncio.Queue(), asyncio.Queue()
    fake = FakePin(pin, 1700000000, payload, script)
    pin.c = fake
    return pin, fake


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}{('  — ' + detail) if detail and not cond else ''}")
    if not cond:
        raise AssertionError(name)


async def t_clean():
    print("clean transfer")
    data = bytes(range(256)) * 4          # 1024B
    pin, fake = make(data, [["stream"]])
    got = await pin.download(1700000000, len(data))
    check("bytes identical", got == data, f"{len(got)} vs {len(data)}")
    check("one req 28", fake.starts == [0], str(fake.starts))
    check("no stops", fake.stops == 0, str(fake.stops))


async def t_gap():
    print("packet loss -> resume at recvOffset")
    data = bytes(range(256)) * 8          # 2048B
    # First 28 loses packets; the restart streams cleanly from recvOffset.
    pin, fake = make(data, [["gap"], ["stream"]])
    got = await pin.download(1700000000, len(data))
    check("recovered full payload", got == data, f"{len(got)} vs {len(data)}")
    check("issued a stop", fake.stops >= 1, str(fake.stops))
    # Exactly one restart: the three in-flight post-gap frames must be swallowed
    # by lossPending, not each trigger their own stop/req-28.
    check("restarted ONCE at recvOffset", fake.starts == [0, 160], str(fake.starts))
    check("exactly one stop", fake.stops == 1, f"{fake.stops} stops")


async def t_empty_during_loss():
    print("EOF frame answering our own stop must NOT truncate")
    data = bytes(range(256)) * 8
    # The device answers the stop by ending the stream, exactly as the SDK notes.
    pin, fake = make(data, [["gap", "empty"], ["stream"]])
    got = await pin.download(1700000000, len(data))
    check("did not truncate at the gap", len(got) == len(data), f"got {len(got)}B of {len(data)}B")
    check("bytes identical", got == data)


async def t_dup():
    print("stale duplicates are ignored")
    data = bytes(range(256)) * 4
    pin, fake = make(data, [["dup"]])
    got = await pin.download(1700000000, len(data))
    check("no duplicated bytes", got == data, f"{len(got)} vs {len(data)}")
    check("no spurious restart", fake.starts == [0], str(fake.starts))


async def t_frames():
    print("frame encoders")
    hs = ble_sync.handshake_frame("0123456789abcdef0123456789abcdef", 7)
    check("pv7 handshake is 22B", len(hs) == 22, str(len(hs)))
    check("pv7 header", hs[:6] == bytes([1, 1, 0, 2, 0, 0]), hs[:6].hex())
    check("pv7 token truncated to 16", hs[6:] == b"0123456789abcdef", hs[6:].decode())

    hs9 = ble_sync.handshake_frame("0123456789abcdef0123456789abcdef", 9)
    check("pv9 handshake is 38B", len(hs9) == 38, str(len(hs9)))
    check("pv9 token is 32 chars", len(hs9[6:]) == 32)

    hs2 = ble_sync.handshake_frame("abc", 2)
    check("pv2 omits arg2", len(hs2) == 21, str(len(hs2)))
    check("pv2 pads with '0'", hs2[5:] == b"abc0000000000000", hs2[5:].decode())

    lf = ble_sync.list_frame(0xAABBCCDD)
    check("list is 12B", len(lf) == 12, str(len(lf)))
    check("list req is 26", struct.unpack_from("<H", lf, 1)[0] == 26)
    check("uid round-trips LE", struct.unpack_from("<I", lf, 3)[0] == 0xAABBCCDD)

    check("stride pv7=10", ble_sync.entry_stride(7) == 10)
    check("stride pv3=9", ble_sync.entry_stride(3) == 9)
    check("stride pv1=8", ble_sync.entry_stride(1) == 8)
    check("voice base pv7=5", ble_sync.voice_base(7) == 5)
    check("voice base pv6=1", ble_sync.voice_base(6) == 1)


async def main():
    for t in (t_frames, t_clean, t_gap, t_empty_during_loss, t_dup):
        await t()
    print("\nALL CHECKS PASSED")


asyncio.run(main())
