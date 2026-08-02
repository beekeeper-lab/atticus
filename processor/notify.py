"""Out-of-band alarms.

Both halves of the pipeline need this, for the same reason: the failures that
matter here are the *quiet* ones. A dead Plaud session and an idle Sunday look
identical in the journal, and nobody reads the journal of a job that runs 96
times a day.

`ATTICUS_NOTIFY_URL` is any endpoint that accepts a POST body — an ntfy topic
is the intended shape. Blank disables everything here.

    notify(cfg, "text")                 send, unconditionally
    notify(cfg, "text", key="auth")     send at most once per throttle window

The `key` form exists because the ingest timer re-discovers a dead session
every 15 minutes. Alarming each time trains you to ignore the alarm, so each
key gets one message per `ATTICUS_ALARM_THROTTLE_HOURS` (default 6) until it
clears. State is a stamp file under the user's cache dir, deliberately *not*
in the vault: an alarm is local operational noise, not part of the record.

## Severity, and why one channel was never enough (#91)

Everything above describes *whether* to send. `alarm()` below decides *where*,
and it exists because two multi-day outages in one week were both delivery
failures rather than detection failures: ingest dead for 2d6h (#77) and the
site path-watcher dead for 1.5 days. In both cases the alarm fired correctly,
into ntfy, and drowned among routine pushes — which is the same verdict the
operator reached about reminders in #66.

Three severities, drawn on *consequence* rather than feeling:

    critical  something is broken or silently losing work and only a human can
              fix it. Escalates to a calendar alert, which breaks through iOS
              Focus (see calendar_alert.py). Ignores quiet hours.
    alert     needs a decision, nothing is being lost. ntfy; deferred overnight.
    routine   a result. ntfy at low priority; deferred overnight.

Two guards keep the strong channel meaningful:

  * **Escalate on persistence, not on one bad pass.** A single failure is
    usually transient; `streak=` lets a caller say how many consecutive
    failures it has seen and nothing escalates below
    `ATTICUS_ESCALATE_AFTER_FAILURES` (default 3).
  * **A separate, longer throttle for the calendar channel**
    (`ATTICUS_ESCALATE_THROTTLE_HOURS`, default 12). A 15-minute timer failing
    all night must not produce 96 calendar events; it produces one.

Quiet hours (`ATTICUS_QUIET_HOURS`, e.g. `22:00-07:00`) never *drop* anything:
routine and alert messages inside the window are appended to
`.state/deferred-notifications.jsonl` and reported by the 07:00 brief. Critical
ignores the window entirely — that is the whole point of the class.
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

STATE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "atticus"

CRITICAL, ALERT, ROUTINE = "critical", "alert", "routine"
SEVERITIES = (CRITICAL, ALERT, ROUTINE)

# Where deferred (quiet-hours) messages wait for the morning brief. In the
# VAULT, unlike the throttle stamps: a message the operator has not seen yet is
# owed to them and must survive a reboot, whereas a throttle stamp is local
# noise that is correct to lose.
DEFERRED = ".state/deferred-notifications.jsonl"


def _stamp(key: str) -> Path:
    return STATE / f"alarm-{key}"


def _ascii(s: str) -> str:
    """Header-safe. Maps the typography we actually use to ASCII rather than
    mangling it: em/en dashes to '-', curly quotes to straight."""
    for a, b in (("—", "-"), ("–", "-"), ("‘", "'"),
                 ("’", "'"), ("“", '"'), ("”", '"'),
                 ("…", "...")):
        s = s.replace(a, b)
    return s.encode("ascii", "replace").decode("ascii")


def throttled(key: str, hours: float) -> bool:
    """True if `key` alarmed recently enough that we should stay quiet."""
    p = _stamp(key)
    try:
        return (time.time() - p.stat().st_mtime) < hours * 3600
    except OSError:
        return False


def clear(key: str):
    """Call when the underlying condition recovers, so the next failure
    alarms immediately instead of waiting out the window.

    Clears the ESCALATION stamp as well (#91). Those windows are longer — 12h
    by default — so a condition that broke, recovered and broke again would
    otherwise stay on ntfy alone for the rest of the day, which is exactly the
    silence #77 was about.
    """
    _stamp(key).unlink(missing_ok=True)
    _stamp(f"escalate-{key}").unlink(missing_ok=True)


class ResultTarget:
    """Lets notify() post to the RESULT topic without mutating cfg.

    Lived in pipeline.py; moved here because it is a notify concern and there are
    now two callers. brief.py had reimplemented it as `copy.copy(cfg)` with the
    url swapped, which works but copies a whole Config to change one attribute —
    and would silently pick up any future notify() dependency on other cfg fields.

    Note that `result_notify_url` falls back to the alarm url when unset
    (config.py), so on a host that has not split them these post to the same
    topic. That is the documented default, not a bug, but it does mean "routed to
    the result channel" describes intent rather than observable behaviour there.
    """

    def __init__(self, cfg):
        self.notify_url = getattr(cfg, "result_notify_url", None)
        self.alarm_throttle_hours = getattr(cfg, "alarm_throttle_hours", 6)
        # Carried so alarm() can honour quiet hours for results (#91): the
        # deferral ledger lives in the vault, and the window is a local-time
        # setting. A stand-in that dropped these would silently disable quiet
        # hours for the one class of message they exist to park.
        self.vault = getattr(cfg, "vault", None)
        self.quiet_hours = getattr(cfg, "quiet_hours", "")
        self.local_tz = getattr(cfg, "local_tz", "")
        self.notify_escalate = getattr(cfg, "notify_escalate", "on")


def notify(cfg, text: str, log=None, key: str | None = None,
           title: str = "Atticus", tags: str = "warning",
           priority: str = "high") -> bool:
    """Returns True if a message was actually sent.

    `Title`/`Priority`/`Tags` are ntfy's conventions — without a title the
    lock screen shows the raw topic name, which is unreadable at a glance.
    Any other endpoint ignores headers it does not know, so this stays a
    generic POST.
    """
    say = log or (lambda m: None)
    if not getattr(cfg, "notify_url", None):
        return False
    hours = getattr(cfg, "alarm_throttle_hours", 6)
    if key and throttled(key, hours):
        say(f"alarm '{key}' suppressed — already sent within {hours}h")
        return False
    # Validate the scheme. This URL comes from config, and urlopen() will
    # happily accept file:// or a custom handler — a typo or a hostile .env
    # should not turn an alarm into a local file operation.
    if not str(cfg.notify_url).lower().startswith(("http://", "https://")):
        say(f"refusing to notify: {str(cfg.notify_url)[:32]!r} is not http(s)")
        return False
    try:
        import urllib.request
        req = urllib.request.Request(  # noqa: S310 — scheme checked above
            cfg.notify_url, data=text.encode(), method="POST",
            headers={
                # HTTP headers are latin-1. A title with an em-dash in it
                # raises UnicodeEncodeError and takes the whole alarm down —
                # observed, and absurd: an alarm must not be defeated by
                # punctuation. Force ASCII here and let the body carry the
                # typography, since the body is sent as bytes.
                "Title": _ascii(title),
                # Alarms default to high: they only fire when something is
                # already broken and unattended. Results pass a lower priority —
                # a finished report is good news, not an emergency.
                "Priority": priority,
                "Tags": _ascii(tags),
            })
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310 — scheme checked above
            pass
    except Exception as e:
        say(f"notification failed: {type(e).__name__}: {e}")
        return False
    if key:
        STATE.mkdir(parents=True, exist_ok=True)
        _stamp(key).touch()
    return True


# ---------------------------------------------------------------------------
#  severity routing (#91)
# ---------------------------------------------------------------------------

def _local_now(cfg) -> datetime:
    name = (getattr(cfg, "local_tz", "") or "").strip()
    if name:
        try:
            return datetime.now(ZoneInfo(name))
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    return datetime.now().astimezone()


def in_quiet_hours(cfg, *, now: datetime | None = None) -> bool:
    """Is the local clock inside ATTICUS_QUIET_HOURS ("22:00-07:00")?

    Windows that cross midnight are the normal case, so the comparison is
    written for that and the same-day case falls out of it. A malformed value
    disables quiet hours rather than silencing everything — the failure mode of
    a typo here should be too many notifications, never too few.
    """
    raw = (getattr(cfg, "quiet_hours", "") or "").strip()
    if not raw:
        return False
    try:
        start_s, end_s = raw.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except (ValueError, TypeError):
        return False
    now = now or _local_now(cfg)
    cur = now.hour * 60 + now.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return False
    if start < end:                       # 01:00-06:00
        return start <= cur < end
    return cur >= start or cur < end      # 22:00-07:00, crossing midnight


def defer(cfg, text: str, *, title: str, severity: str, log=None) -> bool:
    """Park a message for the morning brief instead of buzzing at 3am."""
    vault = getattr(cfg, "vault", None)
    if not vault:
        return False
    p = Path(vault) / DEFERRED
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "severity": severity, "title": title, "text": text[:1000]}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Heal a missing trailing newline first. A writer killed mid-append
        # leaves one, and a bare append would glue this row onto the torn one —
        # losing BOTH, since the join parses as neither. Same rule as the todo
        # and approvals ledgers.
        lead = ""
        try:
            with p.open("rb") as f:
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    lead = "\n"
        except OSError:
            pass
        with p.open("a") as f:
            f.write(lead + json.dumps(row, sort_keys=True) + "\n")
    except OSError as e:
        (log or (lambda m: None))(f"could not defer notification: {e}")
        return False
    (log or (lambda m: None))(f"deferred to the morning brief ({severity})")
    return True


def take_deferred(cfg) -> list[dict]:
    """Everything parked overnight, and clears the file. For the 07:00 brief.

    Read-then-truncate rather than read-then-delete-later: the brief runs once
    and the alternative is a growing file nobody empties.
    """
    vault = getattr(cfg, "vault", None)
    if not vault:
        return []
    p = Path(vault) / DEFERRED
    if not p.is_file():
        return []
    rows = []
    try:
        for line in p.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                rows.append(d)
        p.unlink()
    except OSError:
        return rows
    return rows


def alarm(cfg, text: str, *, severity: str = ALERT, key: str | None = None,
          title: str = "Atticus", tags: str = "warning", streak: int = 1,
          priority: str | None = None, log=None) -> dict:
    """Send `text` on the channels its severity earns.

    Returns {"ntfy": bool, "calendar": bool, "deferred": bool} so a caller can
    report what actually happened rather than assuming.

    This is the function operational code should call. Plain `notify()` remains
    for callers that genuinely mean "one push, no policy" — the reminder drain,
    which has its own delivery contract, and results.
    """
    say = log or (lambda m: None)
    if severity not in SEVERITIES:
        severity = ALERT
    out = {"ntfy": False, "calendar": False, "deferred": False}

    # Quiet hours: park it, unless it is critical. Deferring BEFORE the throttle
    # check is deliberate — a throttled message was already delivered once, but
    # a deferred one has never been seen and is still owed.
    if severity != CRITICAL and in_quiet_hours(cfg):
        out["deferred"] = defer(cfg, text, title=title, severity=severity, log=say)
        return out

    prio = priority or {"critical": "urgent", "alert": "high",
                        "routine": "default"}[severity]
    out["ntfy"] = notify(cfg, text, log=say, key=key, title=title,
                         tags=tags, priority=prio)

    if severity != CRITICAL:
        return out
    if (getattr(cfg, "notify_escalate", "on") or "on").strip().lower() == "off":
        return out
    # Persistence, not one bad pass.
    need = int(getattr(cfg, "escalate_after_failures", 3) or 1)
    if streak < need:
        say(f"not escalating yet: {streak} of {need} consecutive failures")
        return out
    # A second, longer throttle so an overnight crash-loop books one event.
    ekey = f"escalate-{key or _ascii(title)[:40]}"
    ehours = float(getattr(cfg, "escalate_throttle_hours", 12) or 0)
    if ehours and throttled(ekey, ehours):
        say(f"calendar escalation for '{ekey}' suppressed — within {ehours}h")
        return out

    res = calendar_escalate(cfg, subject=title, body=text, log=say)
    out["calendar"] = bool(res.get("created"))
    if out["calendar"]:
        STATE.mkdir(parents=True, exist_ok=True)
        _stamp(ekey).touch()
    return out


def notify_with_actions(cfg, text: str, *, title: str, tags: str,
                        priority: str, actions: str = "", log=None) -> bool:
    """A push carrying ntfy action buttons — the approval ask (#83).

    Separate from `notify()` rather than another keyword on it, because the
    `Actions` header is the one thing here that is genuinely ntfy-specific.
    Every other header degrades harmlessly on a generic endpoint; an action
    button is a contract with one service, and hiding that inside the generic
    sender would make `notify()` quietly less portable than it claims to be.
    """
    say = log or (lambda m: None)
    url = getattr(cfg, "notify_url", None)
    if not url:
        return False
    if not str(url).lower().startswith(("http://", "https://")):
        say(f"refusing to notify: {str(url)[:32]!r} is not http(s)")
        return False
    headers = {"Title": _ascii(title), "Priority": priority, "Tags": _ascii(tags)}
    if actions:
        headers["Actions"] = _ascii(actions)
    try:
        import urllib.request
        req = urllib.request.Request(  # noqa: S310 — scheme checked above
            url, data=text.encode(), method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310
            pass
    except Exception as e:                                       # noqa: BLE001
        say(f"notification failed: {type(e).__name__}: {e}")
        return False
    return True


def calendar_escalate(cfg, *, subject: str, body: str, log=None) -> dict:
    """Indirection so tests can replace the channel without a Graph mock, and
    so notify.py does not import the handler graph at module scope."""
    try:
        import calendar_alert
    except ImportError:                                          # pragma: no cover
        return {"created": False, "reason": "calendar_alert unavailable"}
    return calendar_alert.create(cfg, subject=subject, body=body,
                                 log=log or (lambda m: None))


