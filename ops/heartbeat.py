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
import hashlib
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


def unit_is_running(unit: str) -> bool:
    """True while the unit is starting or running.

    systemd clears ExecMainExitTimestamp for the duration of a run and a timer
    whose service is executing can report no next elapse, so sampling a unit
    mid-run made the checks below announce that a perfectly healthy unit "has no
    record of ever completing" and that its "timer will never fire again". Both
    were observed: the 14:05:06 heartbeat and the 14:05:06 processor run started
    in the same second, and every timer here fires on a :0X boundary, so the
    collision is structural rather than unlucky.
    """
    r = subprocess.run(["systemctl", "--user", "show", unit, "-p", "ActiveState"],
                       capture_output=True, text=True)
    return (r.stdout or "").strip().endswith(("=active", "=activating",
                                              "=reloading", "=deactivating"))


def timer_has_fired(timer: str) -> bool:
    """False for a timer that is installed and scheduled but has not run yet.

    A daily timer installed at noon has genuinely never triggered until midnight.
    Reporting that as "no record of ever completing" is true and useless: it fires
    every hour until the timer's first run and trains the operator to ignore the
    alarm that matters.
    """
    r = subprocess.run(
        ["systemctl", "--user", "show", timer, "-p", "LastTriggerUSec"],
        capture_output=True, text=True)
    val = (r.stdout or "").split("=", 1)[-1].strip()
    return bool(val) and val not in ("n/a", "0")


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

    # 0. Is the vault path even right?
    #
    # This had no check at all, and every downstream one reads as healthy when
    # the path is wrong: load_records() returns [] for a missing inbox, so
    # check_stuck finds nothing, and check_sync returns early with no .git. A
    # typo'd or stale ATTICUS_VAULT_PATH therefore produced a completely green
    # heartbeat while the real vault accumulated unprocessed work — and these
    # repos DID move into PLAUD/, which is the move that killed the site timer.
    # retention.py refuses the UNSET case; nothing caught the WRONG one.
    def check_vault_path():
        if not cfg.vault.is_dir():
            problems.append(f"ATTICUS_VAULT_PATH does not exist: {cfg.vault}")
        elif not (cfg.vault / "inbox").is_dir():
            problems.append(f"{cfg.vault} has no inbox/ — not a vault, so every "
                            "other check below is meaningless")
        else:
            notes.append(f"vault present at {cfg.vault}")
    check("vault-path", check_vault_path)

    # 1. Are the timers even scheduled?
    #
    # retention was NOT in this list, and its own unit comment says "a privacy
    # policy that silently stops running is exactly the failure this whole file
    # exists to prevent" — yet a dead retention timer was invisible here. The
    # vault-site timer is worse: it is the unit that actually sat parked at
    # next_elapse=infinity for 76 minutes and motivated this file, and it was
    # never watched either. unit_exists() skips whatever this host does not have.
    def check_scheduled():
        for t in ("atticus-ingest.timer", "atticus-processor.timer",
                  "atticus-retention.timer", "atticus-vault-site.timer"):
            if not unit_exists(t):
                continue        # this host does not have that role
            if timer_is_scheduled(t):
                notes.append(f"{t} scheduled")
            elif unit_is_running(t.replace(".timer", ".service")):
                # A timer reports no next elapse while its own service is mid-run.
                # That is not the parked-at-infinity failure this check exists for.
                notes.append(f"{t} has no next elapse because its service is "
                             f"running — will reschedule when it finishes")
            else:
                problems.append(f"{t} has NO next elapse — it will never fire again")
    check("timers-scheduled", check_scheduled)

    # 2. Has anything run recently — AND did the last run succeed?
    def check_recent():
        # Per-unit quiet budgets. retention is on a DAILY timer, so the 6h
        # default that suits the 5- and 15-minute timers would false-alarm every
        # single run — and an alarm that always fires is one the operator learns
        # to ignore, which costs more than the check is worth.
        budgets = {"atticus-retention.service": 36.0}
        for u in ("atticus-ingest.service", "atticus-processor.service",
                  "atticus-retention.service"):
            if not unit_exists(u):
                continue        # this host does not have that role
            if unit_is_running(u):
                notes.append(f"{u} is running right now")
                continue
            last, ok = unit_last_run(u)
            if last is None:
                # Distinguish "installed, scheduled, simply not due yet" from
                # "should have run and never has". Only the second is a problem.
                timer = u.replace(".service", ".timer")
                if unit_exists(timer) and not timer_has_fired(timer):
                    notes.append(f"{u} has not run yet — its timer is scheduled "
                                 f"and has not fired")
                    continue
                problems.append(f"{u} has no record of ever completing")
                continue
            if not ok:
                # A crash-looping service still updates its exit timestamp on
                # every exit, so "ran recently" is not "ran successfully".
                problems.append(f"{u} last run exited non-zero (crash-looping?)")
            quiet = (now - last).total_seconds() / 3600
            budget = budgets.get(u, args.max_quiet_hours)
            if quiet > budget:
                problems.append(f"{u} last completed {quiet:.1f}h ago "
                                f"(budget {budget:.0f}h)")
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

        # ATTICUS_BACKLOG_ALARM_MINUTES was documented in ops/.env,
        # ops/.env.example and docs/configuration.md as T-74's mitigation and was
        # read by NO code — the operator believed an alarm was armed that did not
        # exist. This is that knob, and it also replaces a hardcoded 2h. The CLI
        # flag still wins when passed explicitly.
        backlog_min = getattr(cfg, "backlog_alarm_minutes", 0)
        stuck_hours = args.max_stuck_hours
        if backlog_min > 0 and "--max-stuck-hours" not in sys.argv:
            stuck_hours = backlog_min / 60
        cutoff = now - timedelta(hours=stuck_hours)
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
                problems.append(f"{r.stem} stuck in '{r.status}' since {raw} "
                                f"(backlog threshold {stuck_hours * 60:.0f} min)")
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
        # Key on WHAT is wrong, not merely "heartbeat". A single shared key meant
        # a 09:00 "1 malformed record" alarm suppressed a 09:40 "timer parked at
        # infinity" — strictly worse news — until ~15:00. Per-condition keys are
        # the pattern poller.py already uses (plaud-auth / vault-push /
        # too-long-<id>). Hashed so the key is stable for the same set of
        # conditions but changes the moment a new one appears.
        fingerprint = hashlib.sha256(
            "\n".join(sorted(p.split(" since ")[0].split(" (budget")[0]
                             for p in problems)).encode()
        ).hexdigest()[:12]
        # log=print so the watcher's OWN failure is not silent, and check the
        # return: a heartbeat that cannot deliver its alarm is the worst case.
        delivered = notify(
            cfg, "Atticus heartbeat found problems:\n\n" + "\n".join(problems),
            title="Atticus heartbeat", tags="rotating_light", priority="high",
            key=f"heartbeat-{fingerprint}", log=print)
        if not delivered:
            if not getattr(cfg, "notify_url", None):
                print("  BAD  alarm NOT delivered — ATTICUS_NOTIFY_URL is unset")
            elif throttled(f"heartbeat-{fingerprint}",
                           getattr(cfg, "alarm_throttle_hours", 6)):
                pass    # already alarmed this window; healthy, notify logged why
            else:
                print("  BAD  alarm NOT delivered — notify() failed (see above)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
