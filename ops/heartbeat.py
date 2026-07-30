#!/usr/bin/env python3
"""Alarm on ABSENCE, which nothing else here does.

Every other alarm in Atticus fires when something recognisably fails: a dead
session, a failed push, a malformed record. None of them fire when the system
simply stops — a disabled timer, a full disk, an import error before the error
handler, a systemd unit that silently stalled.

That last one is not hypothetical. atticus-vault-site.timer sat enabled, active
and scheduled NEVER for 76 minutes, reporting healthy the whole time.

So this asks the opposite question: has anything actually happened lately?

    heartbeat.py            check and alarm
    heartbeat.py --dry-run  print findings, send nothing

Exit: 0 healthy · 1 problems found (and alarmed)
"""
import argparse
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "processor"))

from config import Config          # noqa: E402
from notify import notify          # noqa: E402
from vault import load_records     # noqa: E402


def unit_last_run(unit: str):
    """When did this unit last finish? None if never or unknown."""
    r = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "ExecMainExitTimestamp",
         "--value"], capture_output=True, text=True)
    raw = (r.stdout or "").strip()
    if not raw:
        return None
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).astimezone(UTC)
        except ValueError:
            continue
    return None


def timer_is_scheduled(timer: str) -> bool:
    """A timer with no next elapse will never fire again, however healthy
    `is-enabled` and `is-active` look."""
    r = subprocess.run(
        ["systemctl", "--user", "show", timer, "-p",
         "NextElapseUSecRealtime", "-p", "NextElapseUSecMonotonic"],
        capture_output=True, text=True)
    out = (r.stdout or "")
    if "NextElapseUSecRealtime=" in out:
        val = out.split("NextElapseUSecRealtime=")[1].split("\n")[0].strip()
        if val and val != "n/a":
            return True
    mono = out.split("NextElapseUSecMonotonic=")[1].split("\n")[0].strip() if \
        "NextElapseUSecMonotonic=" in out else ""
    return bool(mono) and mono not in ("infinity", "n/a", "0")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-quiet-hours", type=float, default=6.0,
                    help="alarm if no successful pass in this long")
    ap.add_argument("--max-stuck-hours", type=float, default=2.0,
                    help="alarm if a record has been in flight this long")
    args = ap.parse_args()

    cfg = Config()
    now = datetime.now(UTC)
    problems, notes = [], []

    # 1. Are the timers even scheduled?
    for t in ("atticus-ingest.timer", "atticus-processor.timer"):
        if not timer_is_scheduled(t):
            problems.append(f"{t} has NO next elapse — it will never fire again")
        else:
            notes.append(f"{t} scheduled")

    # 2. Has anything run recently?
    for u in ("atticus-ingest.service", "atticus-processor.service"):
        last = unit_last_run(u)
        if last is None:
            problems.append(f"{u} has no record of ever completing")
        else:
            quiet = (now - last).total_seconds() / 3600
            if quiet > args.max_quiet_hours:
                problems.append(f"{u} last completed {quiet:.1f}h ago")
            else:
                notes.append(f"{u} ran {quiet:.1f}h ago")

    # 3. Is work stuck in flight?
    bad = []
    try:
        recs = load_records(cfg.vault, on_bad=lambda p, e: bad.append(p))
    except Exception as e:
        problems.append(f"cannot read the vault: {e}")
        recs = []
    if bad:
        problems.append(f"{len(bad)} malformed record(s) in the vault")

    cutoff = now - timedelta(hours=args.max_stuck_hours)
    for r in recs:
        if r.status in ("published", "failed", "retry_wait"):
            continue
        try:
            when = datetime.fromisoformat(
                r.data.get("ingested_at", "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < cutoff:
            problems.append(f"{r.stem} stuck in '{r.status}' since {r.data['ingested_at']}")

    # 4. Is the vault actually in sync?
    if (cfg.vault / ".git").exists():
        r = subprocess.run(["git", "-C", str(cfg.vault), "rev-list", "--count",
                            "@{u}..HEAD"], capture_output=True, text=True)
        n = (r.stdout or "").strip()
        if n.isdigit() and int(n):
            problems.append(f"{n} vault commit(s) never pushed — invisible downstream")
        else:
            notes.append("vault level with remote")

    for n in notes:
        print(f"  ok   {n}")
    for p in problems:
        print(f"  BAD  {p}")

    if problems and not args.dry_run:
        notify(cfg, "Atticus heartbeat found problems:\n\n" + "\n".join(problems),
               title="Atticus heartbeat", tags="rotating_light", priority="high",
               key="heartbeat")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
