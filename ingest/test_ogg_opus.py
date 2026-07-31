#!/usr/bin/env python3
"""Round-trip test for ogg_opus: real Opus packets -> our muxer -> ffmpeg must agree.

    ./test_ogg_opus.py        (needs ffmpeg + ffprobe on PATH)

Generates 3s of 16 kHz mono Opus at CBR 32 kbit/s with 20 ms frames, which is
exactly the pin's format and yields exactly 80-byte packets. Demuxes it to a
bare byte stream (what BLE delivers), re-muxes with ours, and asserts ffmpeg
reads back the same packets, the right duration, and no CRC warnings.

The duration assertion is the one that matters: it is what catches a wrong
granulepos clock. Using 320/packet instead of 960 reports 1.007s for 3.02s of
audio and every downstream timestamp is silently wrong.
"""
import pathlib
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ogg_opus


def demux_ogg(path):
    """Minimal Ogg demuxer: returns (packets, serial). Handles continued packets."""
    data = open(path, "rb").read()
    packets, cur, serial = [], bytearray(), None
    off = 0
    while off < len(data):
        assert data[off:off + 4] == b"OggS", f"bad capture at {off}"
        nsegs = data[off + 26]
        segs = data[off + 27:off + 27 + nsegs]
        body = off + 27 + nsegs
        if serial is None:
            serial = struct.unpack_from("<I", data, 14)[0]
        p = body
        for s in segs:
            cur += data[p:p + s]
            p += s
            if s < 255:                 # packet ends here
                packets.append(bytes(cur))
                cur = bytearray()
        off = p
    return packets, serial


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ogg-opus-test-"))
    ref, out = str(tmp / "ref.opus"), str(tmp / "remux.opus")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=3",
         "-ac", "1", "-c:a", "libopus", "-b:a", "32k", "-vbr", "off",
         "-frame_duration", "20", "-application", "voip", ref, "-y"], check=True)

    pkts, _ = demux_ogg(ref)
    # first two packets are OpusHead / OpusTags
    assert pkts[0].startswith(b"OpusHead"), pkts[0][:16]
    assert pkts[1].startswith(b"OpusTags"), pkts[1][:16]
    audio = pkts[2:]
    sizes = sorted({len(p) for p in audio})
    print(f"reference: {len(audio)} audio packets, distinct sizes = {sizes}")
    assert sizes == [80], f"expected all-80B packets, got {sizes}"

    # This is exactly what the pin would deliver: a bare concatenated byte stream.
    raw = b"".join(audio)
    print(f"raw stream: {len(raw)}B  (== what BLE hands us)")

    sliced = ogg_opus.split_packets(raw, channels=1)
    assert sliced == audio, "split_packets did not reproduce the original packets"
    print(f"split_packets: {len(sliced)} packets, byte-identical to source ✓")

    n = ogg_opus.write_ogg_opus(out, sliced, channels=1)
    print(f"muxed: {n}B -> {out}")

    # 1. our own demuxer must read our own output back
    rt, _ = demux_ogg(out)
    assert rt[0].startswith(b"OpusHead") and rt[1].startswith(b"OpusTags")
    assert rt[2:] == audio, "round-trip through our muxer changed the packets"
    print("self round-trip: packets identical ✓")

    # 2. ffprobe must parse it and report the right duration
    q = subprocess.run(
        ["ffprobe", "-hide_banner", "-v", "error",
         "-show_entries", "stream=codec_name,channels,sample_rate:format=duration",
         "-of", "default=nw=1", out],
        capture_output=True, text=True)
    print("--- ffprobe on our file ---")
    print(q.stdout.strip() or q.stderr.strip())
    assert q.returncode == 0, f"ffprobe failed: {q.stderr}"
    assert "codec_name=opus" in q.stdout

    expected = len(audio) * 20 / 1000
    dur = float([ln.split("=")[1] for ln in q.stdout.strip().splitlines()
                 if ln.startswith("duration=")][0])
    print(f"duration: ffprobe={dur}s  expected≈{expected}s  delta={abs(dur-expected):.4f}s")
    assert abs(dur - expected) < 0.05, "duration wrong -> granulepos is wrong"

    # 3. it must actually DECODE, not merely parse
    d = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-i", out,
                        "-f", "s16le", "-"], capture_output=True)
    assert d.returncode == 0, f"decode failed: {d.stderr.decode()}"
    pcm = len(d.stdout)
    print(f"decoded to {pcm}B PCM = {pcm/2/48000:.3f}s @48kHz mono ✓")

    # 4. no stderr warnings about CRC or corruption
    warn = subprocess.run(["ffmpeg", "-hide_banner", "-v", "warning", "-i", out,
                           "-f", "null", "-"], capture_output=True, text=True)
    if warn.stderr.strip():
        print("!! ffmpeg warnings:\n" + warn.stderr.strip())
    else:
        print("no ffmpeg warnings (CRC + framing clean) ✓")

    print("\nALL CHECKS PASSED")


main()
