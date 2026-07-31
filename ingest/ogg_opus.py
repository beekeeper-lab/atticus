#!/usr/bin/env python3
"""Mux bare Opus packets into an Ogg Opus (.opus) file.

The pin's BLE stream carries **raw Opus packets with no container** — see
docs/decisions/ble-file-transfer.md §6. Packets are a fixed `80 * channels`
bytes per 20 ms, so the stream is self-delimiting: slice it and mux.

Implements just enough of RFC 3533 (Ogg) and RFC 7845 (Ogg Opus) to produce a
file `ffmpeg`, `ffprobe` and `faster-whisper` read directly. No dependencies.

    packets = split_packets(raw, channels=1)
    write_ogg_opus("out.opus", packets, channels=1)
"""
from __future__ import annotations

import struct

OGG_CAPTURE = b"OggS"
MAX_SEGMENTS = 255          # per page, hard limit from the Ogg spec
FRAME_MS = 20               # the pin's Opus frame duration
OPUS_RATE = 48000           # granulepos is ALWAYS 48 kHz in Ogg Opus (RFC 7845 §4)
PIN_RATE = 16000            # the pin's actual capture rate, goes in OpusHead

# Samples of granule position per packet. RFC 7845 fixes the granulepos clock at
# 48 kHz regardless of the encoder's input rate, so a 20 ms packet advances
# 960 — not the 320 that 20 ms at 16 kHz would suggest.
SAMPLES_PER_PACKET = OPUS_RATE * FRAME_MS // 1000     # 960


def _crc32_table() -> list[int]:
    """Ogg's CRC-32: poly 0x04c11db7, init 0, MSB-first, no reflection, no final XOR.

    Deliberately not `zlib.crc32`, which is the reflected Ethernet variant with
    a different init and final XOR and produces entirely different values.
    """
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if r & 0x80000000 else (r << 1) & 0xFFFFFFFF
        table.append(r)
    return table


_CRC_TABLE = _crc32_table()


def ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) & 0xFF) ^ b]
    return crc


def _lacing(length: int) -> list[int]:
    """Encode one packet length as Ogg segment-table entries.

    A packet that is an exact multiple of 255 needs a terminating 0, otherwise
    the decoder treats it as continuing onto the next page.
    """
    segs = [255] * (length // 255)
    segs.append(length % 255)
    return segs


def _page(payload: bytes, segments: list[int], granule: int,
          serial: int, seq: int, header_type: int) -> bytes:
    head = bytearray()
    head += OGG_CAPTURE
    head += struct.pack("<BB", 0, header_type)
    head += struct.pack("<q", granule)          # signed; -1 means "no packet ends here"
    head += struct.pack("<II", serial, seq)
    head += b"\x00\x00\x00\x00"                 # CRC placeholder, filled below
    head += struct.pack("<B", len(segments))
    head += bytes(segments)
    page = bytes(head) + payload
    crc = ogg_crc(page)
    return page[:22] + struct.pack("<I", crc) + page[26:]


def opus_head(channels: int, pre_skip: int = 0) -> bytes:
    """19-byte OpusHead identification header (RFC 7845 §5.1)."""
    return (b"OpusHead"
            + struct.pack("<BB", 1, channels)
            + struct.pack("<H", pre_skip)
            + struct.pack("<I", PIN_RATE)       # informational: original rate
            + struct.pack("<h", 0)              # output gain
            + struct.pack("<B", 0))             # channel mapping family 0


def opus_tags(vendor: bytes = b"atticus") -> bytes:
    """Minimal OpusTags comment header (RFC 7845 §5.2)."""
    return (b"OpusTags"
            + struct.pack("<I", len(vendor)) + vendor
            + struct.pack("<I", 0))             # zero user comments


def split_packets(raw: bytes, channels: int = 1) -> list[bytes]:
    """Slice the BLE byte stream into fixed-size Opus packets.

    Any trailing remainder shorter than a whole packet is dropped: a partial
    Opus packet is not decodable, and keeping it would corrupt the last frame.
    """
    n = 80 * channels
    if n <= 0:
        raise ValueError(f"bad channel count: {channels}")
    return [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]


def write_ogg_opus(path, packets, channels: int = 1, serial: int = 1) -> int:
    """Write packets to `path` as Ogg Opus. Returns bytes written."""
    pages: list[bytes] = []
    seq = 0

    # Each header gets its own page, per RFC 7845 §3.
    head = opus_head(channels)
    pages.append(_page(head, _lacing(len(head)), 0, serial, seq, 0x02))   # BOS
    seq += 1
    tags = opus_tags()
    pages.append(_page(tags, _lacing(len(tags)), 0, serial, seq, 0x00))
    seq += 1

    granule = 0
    i, total = 0, len(packets)
    while i < total:
        payload = bytearray()
        segments: list[int] = []
        # Fill the page until the segment table is full. Our packets are 80 bytes
        # so each costs exactly one segment, but honour the general case.
        while i < total:
            segs = _lacing(len(packets[i]))
            if len(segments) + len(segs) > MAX_SEGMENTS:
                break
            segments += segs
            payload += packets[i]
            granule += SAMPLES_PER_PACKET
            i += 1
        eos = 0x04 if i >= total else 0x00
        pages.append(_page(bytes(payload), segments, granule, serial, seq, eos))
        seq += 1

    if total == 0:
        # No audio: still emit an EOS page so the file is well-formed.
        pages.append(_page(b"", [0], 0, serial, seq, 0x04))

    blob = b"".join(pages)
    with open(path, "wb") as f:
        f.write(blob)
    return len(blob)


def duration_ms(packet_count: int) -> int:
    return packet_count * FRAME_MS
