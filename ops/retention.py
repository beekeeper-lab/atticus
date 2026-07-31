#!/usr/bin/env python3
"""Expire raw audio; keep transcripts and outputs.

Thirty days is long enough to catch a bad transcription and re-run it, which is
the only real reason to keep the audio. The transcript is the durable artifact.

The motivation is not disk. The vault contains recordings of OTHER PEOPLE — a
birthday conversation, a discussion with someone about beard oil, three minutes
of explaining this project to a third party. They did not consent to permanent
retention, and that exposure compounds every day.

WHAT THIS DOES NOT DO. Removing the file deletes it from the working tree, NOT
from git history. Anyone who clones the vault still gets every byte ever
committed. True deletion needs a periodic `git filter-repo`, which rewrites
history and breaks existing clones.

So this bounds what a CHECKOUT exposes. It does not bound what the history
holds. The structural fix is to stop committing audio to git at all — object
storage or git-annex, with only transcripts and metadata in the repo. That is a
change to the queue model and is on the roadmap, not done.

    retention.py --dry-run     show what would go
    retention.py               expire and commit
"""
import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "processor"))

from config import Config, _parse_env          # noqa: E402
from notify import clear as alarm_clear, notify  # noqa: E402
from vault import OWNED_RETENTION, Git, VaultSyncError, load_records  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=None,
                    help="override ATTICUS_AUDIO_RETENTION_DAYS")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # Refuse to run against the wrong tree. Config falls back to REPO/.scratch-vault
    # when ATTICUS_VAULT_PATH is unset, and this job DELETES audio — a silent
    # "nothing to expire" against the dev scratch vault while the real vault
    # keeps every recording forever is the exact privacy failure it exists to
    # prevent. Check the same two sources Config reads.
    if not (os.environ.get("ATTICUS_VAULT_PATH")
            or _parse_env(REPO / "ops/.env").get("ATTICUS_VAULT_PATH")):
        print("error: ATTICUS_VAULT_PATH is unset — refusing to run against the "
              "scratch-vault fallback and silently report nothing to expire. "
              "Set it in ops/.env or the environment.", file=sys.stderr)
        return 2

    cfg = Config()
    days = args.days if args.days is not None else cfg.audio_retention_days
    if days < 0:
        print(f"error: retention days must be >= 0, got {days} — a negative "
              "window puts the cutoff in the future and would expire ALL "
              "published audio at once", file=sys.stderr)
        return 2
    if not days:
        print("retention disabled (ATTICUS_AUDIO_RETENTION_DAYS=0) — "
              "audio is kept indefinitely")
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=days)
    bad = []
    freed = expired = 0

    for rec in load_records(cfg.vault, on_bad=lambda p, e: bad.append(p)):
        # Only expire work that is FINISHED. A record still in flight, waiting
        # to retry, or failed may yet need its audio.
        if rec.status != "published":
            continue
        if rec.data.get("audio_expired_at"):
            continue
        try:
            when = datetime.fromisoformat(
                rec.data.get("recorded_at", "").replace("Z", "+00:00"))
            # A zone-less timestamp cannot be compared against an aware cutoff —
            # the comparison below raises TypeError mid-loop, AFTER earlier audio
            # was already unlinked, with no commit. Treat naive as unparseable.
            if when.tzinfo is None:
                raise ValueError("naive recorded_at (no timezone)")
        except (ValueError, TypeError) as e:
            # Say so rather than skipping silently — the same principle that
            # made load_records() stop swallowing malformed metadata.
            print(f"  ? {rec.stem}: unparseable recorded_at ({e}), not expiring",
                  file=sys.stderr)
            continue
        if when > cutoff:
            continue

        try:
            audio = rec.audio
        except Exception as e:                      # noqa: BLE001
            # A containment failure here means the record is malformed, which
            # the processor already quarantines. Report and move on rather than
            # deleting anything based on a path we do not trust.
            print(f"  ? {rec.stem}: {e}", file=sys.stderr)
            continue
        if not audio.is_file():
            # The audio is already gone — removed out of band, or a crash
            # between unlink and save(). The metadata still claims audio it no
            # longer has. Marking it stops the record looking perpetually
            # unprocessed and keeps the metadata honest; skipping it silently
            # (the old behaviour) meant audio_expired_at was never set.
            print(f"  ? {rec.stem}: audio {audio.name} already absent — "
                  f"{'would mark' if args.dry_run else 'marking'} expired",
                  file=sys.stderr)
            if not args.dry_run:
                rec.data["audio_expired_at"] = (
                    datetime.now(UTC).replace(microsecond=0)
                    .isoformat().replace("+00:00", "Z"))
                rec.data["audio_retention_days"] = days
                rec.data["audio_missing"] = True
                rec.save()
                expired += 1
            continue

        size = audio.stat().st_size
        print(f"  {'would expire' if args.dry_run else 'expiring'} "
              f"{audio.name}  ({size:,} bytes, recorded {rec.data['recorded_at']})")
        if not args.dry_run:
            audio.unlink()
            # The metadata stays, including the sha256, so the record remains
            # complete and auditable — it simply no longer carries the audio.
            rec.data["audio_expired_at"] = (
                datetime.now(UTC).replace(microsecond=0)
                .isoformat().replace("+00:00", "Z"))
            rec.data["audio_retention_days"] = days
            rec.save()
        freed += size
        expired += 1

    if bad:
        print(f"  ! {len(bad)} malformed record(s) skipped", file=sys.stderr)

    if not expired:
        print(f"nothing older than {days} days to expire")
        # Still sweep. A previous run could have died after unlink()+save() but
        # before the commit below: audio_expired_at is already set, so this run
        # skips every record, `expired` is 0, and it used to return HERE — with
        # the deletions sitting uncommitted until some unrelated `add -A` swept
        # them in under a misleading message. check_sync() counts unpushed
        # COMMITS, not a dirty tree, so that state was invisible.
        if not args.dry_run:
            _sync(cfg, "retention: commit deletions stranded by an earlier run")
        return 0

    print(f"\n{expired} recording(s), {freed / 1e6:.1f} MB "
          f"{'would be' if args.dry_run else ''} removed from the working tree")
    if args.dry_run:
        return 0

    if not _sync(cfg, f"retention: expire {expired} recording(s) older "
                      f"than {days}d"):
        return 1
    print("NOTE: git history still contains these blobs. See this file's "
          "docstring for why, and what would actually remove them.")
    return 0


def _sync(cfg, message: str) -> bool:
    """Commit and push, alarming on failure.

    This module never imported notify, so a VaultSyncError here was an uncaught
    traceback to the journal and nothing else — for the one job that enforces a
    privacy policy. The unit's own comment says a privacy policy that silently
    stops running is exactly the failure the design exists to prevent, so it
    needs the same alarm treatment as every other stage.
    """
    git = Git(cfg.vault, cfg.git_name, cfg.git_email, cfg.push_retries,
              paths=OWNED_RETENTION)
    try:
        if git.commit_push(message):
            alarm_clear("retention")
            return True
        # False means only "not a git repo" — a local-only vault, which the rest
        # of the codebase treats as benign and heartbeat's vault-path check
        # reports on. Deletions already happened on disk, which is the point.
        print("  note: vault is not a git repo; deletions are local only")
        return True
    except VaultSyncError as e:
        problem = str(e)
    print(f"  ! retention could not sync: {problem}", file=sys.stderr)
    notify(cfg, f"Atticus retention could not push: audio may have been "
                f"deleted locally without reaching the remote.\n\n{problem}",
           log=print, key="retention", title="Atticus retention — sync failed")
    return False


if __name__ == "__main__":
    sys.exit(main())
