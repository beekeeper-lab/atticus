#!/usr/bin/env python3
"""Read-only characteristic dump for the Plaud NotePin S.

    ./ble_read.py

Complements `ble_scan.py`, which enumerates the GATT tree but reads nothing.
This reads every READ-able characteristic plus any 0x2901 user-description
descriptor, and renders each value as hex, ASCII, and little-endian ints.

Two things worth hunting for (see docs/decisions/ble-file-transfer.md §9):

    portVersion  — selects the voice-frame layout, the session-list entry
                   stride, and whether ChaCha20 wraps every frame (>= 20).
                   0x1910/b001 is the candidate; verify against HandShakeRsp.
    channels     — 80 bytes per 20ms PER CHANNEL, so this scales every offset.

Writes nothing and sends no Plaud protocol command. Safe to run while the pin
is still bound to the official iPhone app.

Discovery is by NAME, never by address: the pin uses a resolvable private
address that rotates every ~15 minutes.
"""
import argparse
import asyncio
import sys

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("bleak not installed:  pip install bleak")

NAME_MATCH = "notepin"

KNOWN = {
    "2a00": "Device Name",
    "2a01": "Appearance",
    "2a04": "Preferred connection params",
    "2a07": "TX power",
    "2a19": "Battery level",
    "b001": "★ candidate portVersion",
}


def short(uuid):
    u = str(uuid).lower()
    if u.startswith("0000") and u.endswith("-0000-1000-8000-00805f9b34fb"):
        return u[4:8]
    return u


def render(b: bytes) -> str:
    out = f"{len(b):>3}B  {b.hex(' ')}"
    if any(32 <= c < 127 for c in b):
        out += "   |" + "".join(chr(c) if 32 <= c < 127 else "." for c in b) + "|"
    ints = []
    if len(b) >= 2:
        ints.append(f"u16le={int.from_bytes(b[:2], 'little')}")
    if len(b) >= 4:
        ints.append(f"u32le={int.from_bytes(b[:4], 'little')}")
    if ints:
        out += f"   [{' '.join(ints)}]"
    return out


async def real_mtu(client):
    """The true negotiated ATT MTU, not bleak's placeholder.

    `BleakClient.mtu_size` returns a hardcoded 23 (with a warning) until
    `_mtu_size` is populated, which is badly misleading — the real value here is
    247. Populating it needs the BlueZ backend's `_acquire_mtu()`, which calls
    D-Bus `AcquireWrite` on the first write-without-response characteristic
    (0x2BB1, as it happens) and reads the MTU out of the reply.

    Private API, so guard it: bleak 3.0.2 already moved this once. It lives on
    the *backend*, not on BleakClient.
    """
    backend = getattr(client, "_backend", None)
    acquire = getattr(backend, "_acquire_mtu", None)
    if acquire is not None and getattr(backend, "_mtu_size", None) is None:
        try:
            await acquire()
        except Exception as e:
            return f"unknown ({type(e).__name__}) — bleak would claim {client.mtu_size}"
    mtu = client.mtu_size
    return f"{mtu}  (ATT payload {mtu - 3}B)"


async def find(seconds):
    print(f"Scanning {seconds}s for a name containing {NAME_MATCH!r} …")
    dev = await BleakScanner.find_device_by_filter(
        lambda d, adv: NAME_MATCH in ((adv.local_name or d.name or "").lower()),
        timeout=seconds,
    )
    if dev is None:
        sys.exit(f"No {NAME_MATCH!r} device found. Powered? In range?")
    print(f"Found {dev.address}  {dev.name}   (address rotates — do not persist it)\n")
    return dev


async def main(seconds):
    dev = await find(seconds)
    async with BleakClient(dev, timeout=30.0) as c:
        print(f"Connected: {c.is_connected}")
        print(f"ATT MTU: {await real_mtu(c)}\n")
        for svc in c.services:
            readable = [ch for ch in svc.characteristics if "read" in ch.properties]
            if not readable:
                continue
            print(f"service {short(svc.uuid)}")
            for ch in readable:
                desc = ""
                for d in ch.descriptors:
                    if short(d.uuid) == "2901":
                        try:
                            raw = await c.read_gatt_descriptor(d.handle)
                            desc = raw.decode("utf-8", "replace").rstrip("\x00")
                        except Exception as e:
                            desc = f"<{type(e).__name__}>"
                try:
                    val = bytes(await c.read_gatt_char(ch))
                    print(f"  {short(ch.uuid)}  {render(val)}")
                except Exception as e:
                    print(f"  {short(ch.uuid)}  <read failed: {type(e).__name__}: {e}>")
                note = KNOWN.get(short(ch.uuid), "")
                if note or desc:
                    bits = [b for b in (note, f"desc={desc!r}" if desc else "") if b]
                    print(f"        ^ {'  '.join(bits)}")
        print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=20, help="scan duration")
    asyncio.run(main(ap.parse_args().seconds))
