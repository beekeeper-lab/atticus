#!/usr/bin/env python3
"""Create a scratch vault with a fake recording, for testing the processor
without a pin, without Plaud, and without WarDog's ingest side.

    mkfixture.py --say "Research what an agentic harness is..."
        synthesise speech with espeak-ng and ingest it as a recording

    mkfixture.py --audio path/to/clip.mp3
        use a real recording you already have

    mkfixture.py --text "..."   (no audio)
        skip transcription entirely — inject a transcript and start at 'routed'

Writes to ./.scratch-vault by default, git-init'd but with no remote, so
commits work and pushes are skipped.
"""
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault import write_atomic  # noqa: E402


def synth(text: str, dest: Path) -> Path:
    """espeak-ng → wav. Robotic, but it exercises the real STT path."""
    if not shutil.which("espeak-ng"):
        sys.exit("espeak-ng not installed — use --audio with a real file instead")
    wav = dest.with_suffix(".wav")
    subprocess.run(["espeak-ng", "-s", "150", "-w", str(wav), text], check=True)
    return wav


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--say", help="synthesise this text as speech")
    g.add_argument("--audio", type=Path, help="use an existing audio file")
    g.add_argument("--text", help="inject a transcript, skip transcription")
    ap.add_argument("--vault", type=Path, default=REPO / ".scratch-vault")
    ap.add_argument("--clean", action="store_true", help="wipe the vault first")
    args = ap.parse_args()

    vault = args.vault
    if args.clean and vault.exists():
        shutil.rmtree(vault)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
    ym = now.strftime("%Y/%m")
    rid = sha256(stamp.encode()).hexdigest()[:12]
    stem = f"{stamp}_{rid}"

    inbox = vault / "inbox" / ym
    inbox.mkdir(parents=True, exist_ok=True)
    for d in ("processed", "failures", ".state"):
        (vault / d).mkdir(parents=True, exist_ok=True)

    meta = {
        "plaud_id": rid, "source": "fixture",
        "recorded_at": now.isoformat().replace("+00:00", "Z"),
        "ingested_at": now.isoformat().replace("+00:00", "Z"),
        "transport": "fixture", "status": "raw", "attempts": 0,
    }

    if args.text:
        meta["audio_filename"] = f"{stem}.none"
        meta["status"] = "transcribed"
        meta["word_count"] = len(args.text.split())
        tp = vault / "processed" / ym / f"{stem}.transcript.txt"
        write_atomic(tp, args.text.strip() + "\n")
        meta["transcript_path"] = str(tp.relative_to(vault))
        print(f"injected transcript ({meta['word_count']} words)")
    else:
        import tempfile
        tmpdir = Path(tempfile.mkdtemp(prefix="atticus-fixture-"))
        try:
            src = synth(args.say, tmpdir / "speech") if args.say else args.audio
            if not src.is_file():
                sys.exit(f"no such audio: {src}")
            dest = inbox / f"{stem}{src.suffix}"
            shutil.copy2(src, dest)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        meta["audio_filename"] = dest.name
        meta["audio_sha256"] = sha256(dest.read_bytes()).hexdigest()
        meta["bytes"] = dest.stat().st_size
        print(f"audio: {dest.relative_to(vault)}  ({meta['bytes']:,} bytes)")

    write_atomic(inbox / f"{stem}.json", json.dumps(meta, indent=2) + "\n")

    if not (vault / ".git").exists():
        subprocess.run(["git", "-C", str(vault), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(vault), "config", "user.email", "t@localhost"], check=True)
        subprocess.run(["git", "-C", str(vault), "config", "user.name", "fixture"], check=True)

    print(f"vault:  {vault}")
    print(f"record: {stem}  [{meta['status']}]")
    print(f"\nnext:   ATTICUS_VAULT_PATH={vault} python3 processor/pipeline.py")


if __name__ == "__main__":
    main()
