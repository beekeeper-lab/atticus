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
from notify import notify, throttled  # noqa: E402
from vault import load_records     # noqa: E402


def unit_exists(unit: str) -> bool:
    """True if the unit is loaded. An ingest-only host has no processor unit and
    vice versa; checking a not-found unit would false-alarm about a role this
    host was never given."""
    r = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "LoadState", "--value"],
        capture_output=True, text=True)
    return (r.stdout or "").strip() == "loaded"


def unit_last_run(unit: str):
    """When did this unit last finish, and did it succeed?

    Returns (when, ok) where `when` is a UTC datetime (or None if never/unknown)
    and `ok` is False when the last run exited non-zero. ExecMainExitTimestamp
    updates on EVERY exit regardless of status, so a crash-looping service reads
    as "ran 0.1h ago" and the heartbeat stays green forever. Pull the exit
    status in the same call and treat a non-zero last run as a problem."""
    r = subprocess.run(
        ["systemctl", "--user", "show", unit,
         "-p", "ExecMainExitTimestamp", "-p", "ExecMainStatus", "-p", "Result"],
        capture_output=True, text=True)
    props = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v.strip()

    status = props.get("ExecMainStatus", "").strip()
    result = props.get("Result", "").strip()
    ok = (status in ("", "0")) and (result in ("", "success"))

    raw = props.get("ExecMainExitTimestamp", "").strip()
    if not raw:
        return None, ok
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).astimezone(UTC), ok
        except ValueError:
            continue
    return None, ok


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

    def check(label, fn):
        """Run one check. An exception in one must not abort the rest — that
        would suppress problems the earlier checks already found, and silence is
        the one failure this whole job exists to catch."""
        try:
            fn()
        except Exception as e:                  # noqa: BLE001
            problems.append(f"{label} check crashed: {type(e).__name__}: {e}")

    # 1. Are the timers even scheduled?
    def check_scheduled():
        for t in ("atticus-ingest.timer", "atticus-processor.timer"):
            if not unit_exists(t):
                continue        # this host does not have that role
            if not timer_is_scheduled(t):
                problems.append(f"{t} has NO next elapse — it will never fire again")
            else:
                notes.append(f"{t} scheduled")
    check("timers-scheduled", check_scheduled)

    # 2. Has anything run recently — AND did the last run succeed?
    def check_recent():
        for u in ("atticus-ingest.service", "atticus-processor.service"):
            if not unit_exists(u):
                continue        # this host does not have that role
            last, ok = unit_last_run(u)
            if last is None:
                problems.append(f"{u} has no record of ever completing")
                continue
            if not ok:
                # A crash-looping service still updates its exit timestamp on
                # every exit, so "ran recently" is not "ran successfully".
                problems.append(f"{u} last run exited non-zero (crash-looping?)")
            quiet = (now - last).total_seconds() / 3600
            if quiet > args.max_quiet_hours:
                problems.append(f"{u} last completed {quiet:.1f}h ago")
            elif ok:
                notes.append(f"{u} ran {quiet:.1f}h ago")
    check("units-recent", check_recent)

    # 3. Is work stuck in flight?
    def check_stuck():
        bad = []
        try:
            recs = load_records(cfg.vault, on_bad=lambda p, e: bad.append(p))
        except Exception as e:                  # noqa: BLE001
            problems.append(f"cannot read the vault: {e}")
            recs = []
        if bad:
            problems.append(f"{len(bad)} malformed record(s) in the vault")

        cutoff = now - timedelta(hours=args.max_stuck_hours)
        for r in recs:
            if r.status in ("published", "failed", "retry_wait"):
                continue
            raw = r.data.get("ingested_at", "")
            try:
                when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                # A zone-less ingested_at makes the `when < cutoff` comparison
                # raise TypeError, which used to crash main() before the alarm
                # went out — suppressing every problem found above. Normalise it
                # AND flag it, since a naive timestamp is itself a writer bug.
                if when.tzinfo is None:
                    problems.append(f"{r.stem} has a naive ingested_at ({raw!r}) "
                                    "— assuming UTC")
                    when = when.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                problems.append(f"{r.stem} has an unreadable ingested_at ({raw!r})")
                continue
            if when < cutoff:
                problems.append(f"{r.stem} stuck in '{r.status}' since {raw}")
    check("stuck-in-flight", check_stuck)

    # 4. Is the vault actually in sync?
    def check_sync():
        if not (cfg.vault / ".git").exists():
            return
        r = subprocess.run(["git", "-C", str(cfg.vault), "rev-list", "--count",
                            "@{u}..HEAD"], capture_output=True, text=True)
        n = (r.stdout or "").strip()
        if r.returncode != 0:
            # No upstream or a detached HEAD: an empty count is NOT proof of
            # sync. The old code read that empty string as healthy.
            problems.append("vault has no upstream / detached HEAD — "
                            "cannot tell whether commits are pushed")
        elif not n.isdigit():
            problems.append(f"unexpected 'rev-list --count' output: {n!r}")
        elif int(n):
            problems.append(f"{n} vault commit(s) never pushed — invisible downstream")
        else:
            notes.append("vault level with remote")
    check("vault-in-sync", check_sync)

    for n in notes:
        print(f"  ok   {n}")
    for p in problems:
        print(f"  BAD  {p}")

    if problems and not args.dry_run:
        # log=print so the watcher's OWN failure is not silent, and check the
        # return: a heartbeat that cannot deliver its alarm is the worst case.
        delivered = notify(
            cfg, "Atticus heartbeat found problems:\n\n" + "\n".join(problems),
            title="Atticus heartbeat", tags="rotating_light", priority="high",
            key="heartbeat", log=print)
        if not delivered:
            if not getattr(cfg, "notify_url", None):
                print("  BAD  alarm NOT delivered — ATTICUS_NOTIFY_URL is unset")
            elif throttled("heartbeat", getattr(cfg, "alarm_throttle_hours", 6)):
                pass    # already alarmed this window; healthy, notify logged why
            else:
                print("  BAD  alarm NOT delivered — notify() failed (see above)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
