#!/usr/bin/env python3
"""Reminders: a delivery at a time. Resolves issue #52.

A reminder is not a todo. A todo has content and no deadline behaviour; a
reminder is nothing *but* deadline behaviour — the text is almost incidental, and
if it arrives at the wrong moment it has failed completely. That difference is why
this is a store plus a drain rather than a file the agent appends to.

## Nothing here is new infrastructure

Delivery is `notify.py`, which is already load-bearing for failure alarms.
Scheduling is a systemd timer, which this project uses heavily. Parsing "at four"
is the model's job and needs no credential. So the only real work is the seam
between them, and the seam is a timezone.

## The timezone, which is the whole difficulty

Every timestamp in this project is UTC ISO-8601 by convention. "Remind me at
four" is unambiguously **local**. Get that wrong and the feature does not look
misconfigured, it looks broken: a push four hours late reads as a bug in the
reminder, not as a wrong `TZ`.

So, three rules:

1. **Store UTC.** `at` in the ledger is always UTC, like every other timestamp
   here, and it is what the drain compares against.
2. **Resolve against an explicit zone.** `ATTICUS_LOCAL_TZ` names an IANA zone.
   A *name*, never a fixed offset, because the reminder may be on the far side of
   a DST boundary from the moment it was set — `datetime.now().astimezone()`
   captures today's offset and would be an hour out in the autumn.
3. **Put the local time in the notification.** Always, even when it is not late.
   That single line is what makes rule 1 or 2 going wrong visible the *first*
   time it happens instead of after weeks of vaguely mistimed pushes.

An unset `ATTICUS_LOCAL_TZ` falls back to the host zone, read from
`/etc/localtime` so it is still a real IANA name and still DST-correct. That
fallback is deliberate — refusing to set a reminder without a config line would
kill the feature on arrival — but a *wrong* `ATTICUS_LOCAL_TZ` is refused loudly
rather than silently treated as UTC, because silently-UTC is exactly the
four-hours-late bug.

## Mark, do not delete

`.state/reminders.jsonl` is append-only, latest-event-per-id wins, exactly like
`.state/audio-requests.jsonl` and the `usage-*.jsonl` ledgers. Delivery appends a
`delivered` event; it never rewrites or removes the `pending` one. A push that
did not arrive on the phone is then answerable from the vault — "was it stored?
did it fire? how late? what did it hear?" — and a deleted row can answer none of
those.

## A reminder whose time passed while the box was down: fire late, with a note

Bounded by `ATTICUS_REMINDER_MAX_LATE_HOURS` (default 24). Inside the window it
fires with "this was due at 4:00 PM … — 3h 12m ago", because a late reminder is
usually still worth having and dropping it silently is the one outcome with no
recovery. Past the window it is marked `expired` and reported in a single
grouped push, because an unbounded catch-up means a box down for a week wakes up
and fires nine days of stale errands at once — noise that also makes the useful
ones unreadable. Grouped-and-reported rather than dropped, so the operator still
learns that something was owed to them.
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))

import notify as nf                                              # noqa: E402

LEDGER = ".state/reminders.jsonl"

PENDING, DEFERRED, DELIVERED, EXPIRED, CANCELLED = (
    "pending", "deferred", "delivered", "expired", "cancelled")
# The two states that still owe a push. `deferred` is a pending reminder whose
# push has failed at least once; it keeps retrying on the ordinary cadence.
OPEN = (PENDING, DEFERRED)

# A push lands on a lock screen. Longer than this is not a reminder, it is a note
# that the agent should have written into the report instead.
MAX_TEXT = 200
# Slop on "already late". The drain runs on a one-minute timer and the handler
# runs minutes after the words were spoken, so a couple of minutes of lateness is
# scheduling granularity, not an error worth mentioning to the operator.
LATE_SLOP_SECONDS = 120


class ReminderError(ValueError):
    """A reminder request that cannot be honoured, with a reason to show a human.

    Deliberately not `outbox.OutboxError`: this module is also the drain, which
    runs from its own timer with no outbox in sight. The handler translates.
    """


# ---------------------------------------------------------------------------
#  time
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso_z(dt: datetime) -> str:
    """UTC ISO-8601 with a Z, matching every other timestamp in the vault."""
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def host_zone() -> tuple[tzinfo, str]:
    """The host's zone as an IANA name where possible, so DST still applies.

    `datetime.now().astimezone().tzinfo` is a FIXED offset frozen at this instant.
    Using it to resolve a reminder on the other side of a DST change is wrong by
    an hour, which is precisely the class of error this module exists to prevent.
    `/etc/localtime` is a symlink into the zoneinfo database on every systemd
    host, so the real name is right there.
    """
    try:
        target = Path("/etc/localtime").resolve()
        parts = target.parts
        if "zoneinfo" in parts:
            name = "/".join(parts[parts.index("zoneinfo") + 1:])
            if name:
                return ZoneInfo(name), name
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass
    # No usable name. Fall back to the current offset and SAY so in the label —
    # the label goes into the notification, so the guess is visible rather than
    # assumed correct.
    off = datetime.now().astimezone().utcoffset() or timedelta(0)
    return timezone(off), f"UTC{_offset_label(off)} (host offset; set ATTICUS_LOCAL_TZ)"


def _offset_label(off: timedelta) -> str:
    total = int(off.total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def local_zone(cfg) -> tuple[tzinfo, str]:
    """(zone, label). The label is what the operator reads in the push."""
    name = (getattr(cfg, "local_tz", "") or "").strip()
    if not name:
        return host_zone()
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        # Do NOT fall back. A typo'd zone silently becoming UTC is the exact
        # failure that ships as "reminders are four hours late".
        raise ReminderError(
            f"ATTICUS_LOCAL_TZ={name!r} is not a known IANA timezone "
            f"(want something like 'America/New_York' or 'Europe/London')")


def fmt_local(dt: datetime, zone: tzinfo) -> str:
    """"4:00 PM, Sat 1 Aug" — built by hand because %-I and %-d are glibc-only.

    The date is always included even when the reminder is for today. It costs
    four words and it is the difference between spotting a day-boundary bug and
    puzzling over it.
    """
    lt = dt.astimezone(zone)
    hour = lt.hour % 12 or 12
    ampm = "AM" if lt.hour < 12 else "PM"
    return f"{hour}:{lt.minute:02d} {ampm}, {lt:%a} {lt.day} {lt:%b}"


def human_delta(seconds: float) -> str:
    s = int(abs(seconds))
    if s < 90:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    h, m = divmod(s // 60, 60)
    if h < 24:
        return f"{h}h {m:02d}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


_HHMM = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def resolve_when(req: dict, cfg, *, now: datetime | None = None) -> tuple[datetime, str]:
    """(UTC instant, zone label) for a `reminders.set` request.

    Three accepted spellings, in order of preference, and the ordering is the
    point:

      `in_minutes`  a relative horizon. Needs NO timezone at all, so "remind me
                    in twenty minutes" cannot be broken by a misconfigured zone.
                    Preferred whenever the request was relative.
      `at`          local wall clock, no zone suffix: "2026-08-01T16:00". This is
                    what the agent should write for "at four" — it can read the
                    host's local clock (/etc/localtime is bound into its sandbox)
                    but it does not know the operator's zone NAME and must not
                    guess one. Naming the zone is the pipeline's job.
      `at` with a zone
                    "…T16:00:00Z" or "…+01:00" is honoured as given, for the rare
                    request that really was expressed in an absolute zone.

    A bare "16:00" is accepted as the next occurrence of that local wall time.
    That is a safety net, not the contract: it is exactly what "at four" means,
    so an agent that omits the date gets the right answer instead of a refusal.
    """
    now = now or _utcnow()
    zone, label = local_zone(cfg)

    if req.get("in_minutes") not in (None, ""):
        try:
            mins = float(req["in_minutes"])
        except (TypeError, ValueError):
            raise ReminderError(f"in_minutes must be a number, got {req['in_minutes']!r}")
        if mins <= 0:
            raise ReminderError(f"in_minutes must be positive, got {mins:g}")
        return _bound(now + timedelta(minutes=mins), now, cfg), label

    raw = str(req.get("at") or "").strip()
    if not raw:
        raise ReminderError(
            "reminders.set needs 'at' (local wall-clock ISO-8601, e.g. "
            "\"2026-08-01T16:00\") or 'in_minutes'")

    m = _HHMM.match(raw)
    if m:
        # Time with no date: the next occurrence of it, locally.
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if h > 23 or mi > 59 or s > 59:
            raise ReminderError(f"{raw!r} is not a valid time of day")
        base = now.astimezone(zone).replace(hour=h, minute=mi, second=s, microsecond=0)
        if base <= now.astimezone(zone) + timedelta(seconds=LATE_SLOP_SECONDS):
            base += timedelta(days=1)
        return _bound(base.astimezone(UTC), now, cfg), label

    try:
        when = datetime.fromisoformat(raw.replace(" ", "T"))
    except ValueError:
        raise ReminderError(
            f"could not read {raw!r} as a time — want local wall-clock ISO-8601 "
            f'like "2026-08-01T16:00", or "16:00" for the next such time')
    if when.tzinfo is None:
        # THE load-bearing line in this file. A naive timestamp is LOCAL wall
        # clock, never UTC. Reading it as UTC is the four-hours-late bug.
        when = when.replace(tzinfo=zone)
    return _bound(when.astimezone(UTC), now, cfg), label


def _bound(when: datetime, now: datetime, cfg) -> datetime:
    """Refuse a time that is almost certainly a misparse rather than a request.

    Both bounds catch the same failure from opposite ends: a mangled year, or a
    date the model resolved against the wrong "now". Storing one silently means a
    reminder that never fires (far future) or fires immediately with an absurd
    "due 400 days ago" note (far past), and in both cases the operator's only
    evidence is a line in a JSONL file.
    """
    max_days = int(getattr(cfg, "reminder_max_days", 365) or 0)
    if max_days and when > now + timedelta(days=max_days):
        raise ReminderError(
            f"{iso_z(when)} is more than {max_days} days away — refusing it as a "
            f"misparsed date rather than storing a reminder that never fires")
    max_late = float(getattr(cfg, "reminder_max_late_hours", 24) or 0)
    if max_late and when < now - timedelta(hours=max_late):
        raise ReminderError(
            f"{iso_z(when)} is already more than {max_late:g}h in the past — "
            f"refusing it as a misparsed date")
    return when


# ---------------------------------------------------------------------------
#  the ledger
# ---------------------------------------------------------------------------

def ledger_path(vault: Path) -> Path:
    return Path(vault) / LEDGER


def clean_text(text: str) -> str:
    """One line, bounded. This string goes onto a lock screen."""
    t = " ".join(str(text or "").split())
    if not t:
        raise ReminderError("a reminder needs some text to remind you of")
    return t[:MAX_TEXT]


def reminder_id(when: datetime, text: str) -> str:
    """Deterministic in (instant, text), so setting the same reminder twice is
    one reminder.

    This is not cosmetic. `pipeline.py --retry <id>` re-runs a record's outbox,
    and CLAUDE.md's own convention is that nothing gets processed twice. Without
    a derived id, re-running one recording would double every reminder it set —
    and a duplicate push is indistinguishable from a bug in the drain.
    """
    h = hashlib.sha256(f"{iso_z(when)}|{text}".encode()).hexdigest()
    return h[:12]


def _events(vault: Path) -> list[dict]:
    p = ledger_path(vault)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            # A truncated write must not blind the drain to the rows around it —
            # same rule as usage.load().
            continue
        if isinstance(d, dict) and d.get("id"):
            out.append(d)
    return out


def state(vault: Path) -> dict[str, dict]:
    """id -> the reminder as it now stands: first event, updated by later ones.

    Later events carry only what changed, so the `at`/`text` written when the
    reminder was set survive into the delivered row without being repeated.
    """
    out: dict[str, dict] = {}
    for ev in _events(vault):
        cur = out.setdefault(ev["id"], {})
        cur.update({k: v for k, v in ev.items() if v is not None})
    return out


def open_reminders(vault: Path) -> list[dict]:
    """Everything still owed a push, soonest first."""
    items = [r for r in state(vault).values() if r.get("status") in OPEN]
    return sorted(items, key=lambda r: str(r.get("at") or ""))


def append(vault: Path, rid: str, status: str, **fields) -> dict:
    ev = {"id": rid, "status": status, "event_at": iso_z(_utcnow())}
    ev.update({k: v for k, v in fields.items() if v is not None})
    p = ledger_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(ev, sort_keys=True) + "\n")
    return ev


def add(vault: Path, *, when: datetime, text: str, zone_label: str,
        said: str = "", source: str = "", stem: str = "") -> dict:
    """Store one pending reminder. Returns the event, with `duplicate` set if it
    was already there."""
    rid = reminder_id(when, text)
    existing = state(vault).get(rid)
    if existing is not None:
        return {**existing, "id": rid, "duplicate": True}
    return {**append(vault, rid, PENDING,
                     at=iso_z(when), text=text, tz=zone_label,
                     said=said or None, source=source or None, stem=stem or None),
            "duplicate": False}


# ---------------------------------------------------------------------------
#  the drain
# ---------------------------------------------------------------------------

def _message(rec: dict, when: datetime, late: float, zone: tzinfo, label: str) -> str:
    """The push body. The local time is ALWAYS here — see this module's docstring."""
    local = fmt_local(when, zone)
    if late > LATE_SLOP_SECONDS:
        # Say it was late, and by how much. Otherwise a reminder delivered after
        # an outage reads as a reminder set for the wrong time.
        tail = (f"This was due at {local} ({label}) — {human_delta(late)} ago.")
    else:
        tail = f"Set for {local} ({label})."
    return f"{rec.get('text') or '(no text)'}\n\n{tail}"


def drain(cfg, *, log=print, now: datetime | None = None) -> dict:
    """Fire everything due. Never raises for a per-reminder problem.

    Returns counts. Silent and free when nothing is due, which is almost every
    run — this is on a one-minute timer.
    """
    now = now or _utcnow()
    # Resolve the zone FIRST, before the "nothing due" exit. An unusable
    # ATTICUS_LOCAL_TZ means every reminder on this host is mistimed or stalled, and
    # checking it only when something happens to be due would hide that for hours
    # — the drain would exit 0 all afternoon and heartbeat would report it healthy.
    zone, label = local_zone(cfg)
    items = open_reminders(cfg.vault)
    due = []
    for r in items:
        try:
            when = datetime.fromisoformat(str(r.get("at")).replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError("naive 'at' in the ledger")
        except (TypeError, ValueError) as e:
            log(f"  ? reminder {r.get('id')}: unreadable 'at' ({e}) — not firing")
            continue
        if when <= now:
            due.append((r, when))
    if not due:
        return {"due": 0, "fired": 0, "expired": 0, "failed": 0,
                "open": len(items)}

    target = nf.ResultTarget(cfg)
    if not getattr(target, "notify_url", None):
        # Nothing to mark. A reminder held for want of a URL must stay pending, or
        # configuring ATTICUS_NOTIFY_URL later would silently deliver nothing.
        log(f"  {len(due)} reminder(s) due but no ATTICUS_NOTIFY_URL is set — "
            f"they stay pending")
        return {"due": len(due), "fired": 0, "expired": 0, "failed": len(due),
                "open": len(items), "held": True}

    max_late = float(getattr(cfg, "reminder_max_late_hours", 24) or 0)
    fired = expired = failed = 0
    stale = []

    for rec, when in due:
        late = (now - when).total_seconds()
        if max_late and late > max_late * 3600:
            append(cfg.vault, rec["id"], EXPIRED, late_seconds=int(late),
                   reason=f"still undelivered {human_delta(late)} after it was due "
                          f"(ATTICUS_REMINDER_MAX_LATE_HOURS={max_late:g})")
            log(f"  ⧗ expired: {rec.get('text')!r} was due {fmt_local(when, zone)}, "
                f"{human_delta(late)} ago")
            stale.append((rec, when))
            expired += 1
            continue

        errors: list[str] = []
        sent = nf.notify(target, _message(rec, when, late, zone, label),
                         log=errors.append, title="Atticus reminder",
                         # High, deliberately. The operator asked to be
                         # interrupted at this moment; a reminder that loses to a
                         # notification summary has not been delivered.
                         tags="alarm_clock", priority="high")
        if sent:
            append(cfg.vault, rec["id"], DELIVERED, late_seconds=int(max(0, late)))
            log(f"  ✓ reminder: {rec.get('text')!r} (due {fmt_local(when, zone)}"
                + (f", {human_delta(late)} late)" if late > LATE_SLOP_SECONDS else ")"))
            fired += 1
        else:
            why = (errors[-1] if errors else "push failed")[:200]
            log(f"  ✗ reminder {rec['id']}: {why} — will retry")
            # Record the TRANSITION into failing, not every attempt. A dead
            # endpoint on a one-minute timer would otherwise write 1,440 lines a
            # day into a ledger that is committed to git.
            if rec.get("status") != DEFERRED:
                append(cfg.vault, rec["id"], DEFERRED, reason=why)
            failed += 1

    if stale:
        _report_expired(target, stale, zone, label, log=log)

    return {"due": len(due), "fired": fired, "expired": expired,
            "failed": failed, "open": len(items)}


def _report_expired(target, stale: list, zone: tzinfo, label: str, *, log=print):
    """One grouped push for reminders too old to fire on their own.

    Dropping them silently is the only outcome with no recovery: the operator
    asked for something at a time, nothing arrived, and nothing ever says so.
    Grouped because a week of downtime should produce one message, not thirty.
    """
    lines = [f"· {r.get('text')} — was due {fmt_local(w, zone)}"
             for r, w in stale[:5]]
    if len(stale) > 5:
        lines.append(f"· …and {len(stale) - 5} more")
    nf.notify(target,
              f"{len(stale)} reminder(s) passed unfired and are now too old to "
              f"deliver — Atticus was probably down.\n\n" + "\n".join(lines)
              + f"\n\nTimes are {label}.",
              log=log, title="Atticus — reminders missed",
              tags="warning", priority="default")


# ---------------------------------------------------------------------------
#  CLI — the drain's entry point, and a way to look at the queue
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show open reminders and exit")
    ap.add_argument("--env", type=Path, help="alternate ops/.env")
    args = ap.parse_args(argv)

    from config import Config
    from lock import AlreadyRunning, single_instance
    try:
        cfg = Config(args.env)
    except Exception as e:                          # noqa: BLE001
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if not Path(cfg.vault).is_dir():
        print(f"vault not found: {cfg.vault}", file=sys.stderr)
        return 2

    if args.list:
        try:
            zone, label = local_zone(cfg)
        except ReminderError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        items = open_reminders(cfg.vault)
        if not items:
            print("no open reminders")
            return 0
        print(f"{len(items)} open reminder(s), times in {label}")
        for r in items:
            when = datetime.fromisoformat(str(r["at"]).replace("Z", "+00:00"))
            print(f"  {fmt_local(when, zone):<24} {r.get('status'):<9} "
                  f"{r.get('text')}")
        return 0

    # Serialise against another drain. systemd will not start this unit twice, but
    # a manual run racing the timer would deliver the same reminder twice, and a
    # duplicate push is indistinguishable from a bug in here.
    try:
        with single_instance("reminders", vault=cfg.vault):
            try:
                res = drain(cfg)
            except ReminderError as e:
                # A bad ATTICUS_LOCAL_TZ. Loud and non-zero: every reminder on
                # this host is stalled until it is fixed, and heartbeat watches
                # for a unit that keeps exiting non-zero.
                print(f"error: {e}", file=sys.stderr)
                return 2
    except AlreadyRunning as e:
        print(f"skipped: {e}", file=sys.stderr)
        return 0
    # NOTE: this deliberately does NOT commit or push. The marks land in the
    # vault's working tree and the next processor pass sweeps them up — `.state`
    # is in OWNED_PROCESSOR, and both run on the same host against the same
    # checkout. A one-minute timer that pulled and pushed would mean ~1,440 git
    # round trips a day, all of them contending the vault git lock with ingest
    # and the processor, to commit a line that arrives within five minutes anyway.
    return 1 if res.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
