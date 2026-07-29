#!/usr/bin/env python3
"""BLE recon for the Plaud NotePin S — run this the moment the pin powers on.

    ./ble_scan.py                 scan and report Plaud-looking devices
    ./ble_scan.py --connect ADDR  connect and enumerate GATT services

Run BEFORE binding the pin to the official Plaud app. Devices bind to one
client at a time, and we want to see how an unbound device behaves.

What we're looking for (see docs/decisions/ble-protocol-notes.md):

    service        0x1910   ← the command/data channel
    characteristic 0x2BB0   TX — device transmits; we subscribe (notify)
    characteristic 0x2BB1   RX — device receives;  we write
    service        0x180F   battery (standard)

If those are advertised and connectable from Linux, direct BLE ingest is live
and Plaud Cloud can leave the design entirely.
"""
import argparse
import asyncio
import sys

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("bleak not installed:  pip install bleak")

TARGET = {
    "1910": "★ Plaud command/data service",
    # TX/RX are named from the DEVICE's point of view, so they invert for us:
    # the device transmits on 2BB0 (we subscribe) and receives on 2BB1 (we write).
    "2bb0": "★ TX — device→us, SUBSCRIBE here",
    "2bb1": "★ RX — us→device, WRITE here",
    "180f": "battery service",
    "2a19": "battery level",
    "2902": "CCCD (notify enable)",
}


def short(uuid):
    """0000180f-0000-1000-8000-00805f9b34fb → '180f'"""
    u = str(uuid).lower()
    return u[4:8] if u.startswith("0000") and u.endswith("-0000-1000-8000-00805f9b34fb") else u


def annotate(uuid):
    return TARGET.get(short(uuid), "")


async def scan(seconds):
    print(f"Scanning {seconds}s…  (power on the pin; press its button if idle)\n")
    devices = await BleakScanner.discover(timeout=seconds, return_adv=True)
    if not devices:
        print("Nothing found. Is Bluetooth up?  rfkill list bluetooth")
        return

    rows = []
    for addr, (dev, adv) in devices.items():
        name = adv.local_name or dev.name or ""
        uuids = [short(u) for u in (adv.service_uuids or [])]
        hit = "1910" in uuids or "plaud" in name.lower() or "notepin" in name.lower()
        rows.append((hit, addr, name, adv.rssi, uuids))

    rows.sort(key=lambda r: (not r[0], -(r[3] or -999)))
    for hit, addr, name, rssi, uuids in rows:
        mark = "★" if hit else " "
        print(f"{mark} {addr}  {str(rssi):>4}dBm  {name or '(no name)'}")
        if uuids:
            print(f"    services: {', '.join(uuids)}")

    hits = [r for r in rows if r[0]]
    print()
    if hits:
        print(f"★ {len(hits)} candidate(s). Next:")
        print(f"    ./ble_scan.py --connect {hits[0][1]}")
    else:
        print("No obvious Plaud device. It may not advertise 0x1910 until woken —")
        print("press the pin's button, or start a recording, then rescan.")


async def connect(address):
    print(f"Connecting to {address} …")
    try:
        async with BleakClient(address, timeout=30.0) as client:
            print(f"Connected: {client.is_connected}\n")
            found = set()
            for svc in client.services:
                note = annotate(svc.uuid)
                print(f"service {short(svc.uuid)}  {note}")
                found.add(short(svc.uuid))
                for ch in svc.characteristics:
                    props = ",".join(ch.properties)
                    print(f"   char {short(ch.uuid)}  [{props}]  {annotate(ch.uuid)}")
                    found.add(short(ch.uuid))
                    for d in ch.descriptors:
                        print(f"      desc {short(d.uuid)}  {annotate(d.uuid)}")

            print()
            need = {"1910", "2bb0", "2bb1"}
            missing = need - found
            if not missing:
                print("★★★ Full Plaud command channel present and connectable.")
                print("     Direct BLE ingest is viable — Plaud Cloud can leave the design.")
            else:
                print(f"Missing: {', '.join(sorted(missing))}")
                print("The device may hide these until it is bound, or use different UUIDs")
                print("than the Android SDK suggested. Record what you DID see.")
    except Exception as e:
        print(f"Connect failed: {type(e).__name__}: {e}")
        print("\nIf this is a pairing/auth rejection, that is itself a finding —")
        print("it means the pin will not talk to an unbound client.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--connect", metavar="ADDR", help="connect and enumerate GATT")
    ap.add_argument("--seconds", type=int, default=15, help="scan duration")
    args = ap.parse_args()
    asyncio.run(connect(args.connect) if args.connect else scan(args.seconds))


if __name__ == "__main__":
    main()
