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
"""
import os
import time
from pathlib import Path

STATE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "atticus"


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
    alarms immediately instead of waiting out the window."""
    _stamp(key).unlink(missing_ok=True)


def notify(cfg, text: str, log=None, key: str | None = None,
           title: str = "Atticus") -> bool:
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
    try:
        import urllib.request
        req = urllib.request.Request(
            cfg.notify_url, data=text.encode(), method="POST",
            headers={
                # HTTP headers are latin-1. A title with an em-dash in it
                # raises UnicodeEncodeError and takes the whole alarm down —
                # observed, and absurd: an alarm must not be defeated by
                # punctuation. Force ASCII here and let the body carry the
                # typography, since the body is sent as bytes.
                "Title": _ascii(title),
                # These alarms only fire when something is already broken and
                # unattended. There is no such thing as an FYI here.
                "Priority": "high",
                "Tags": "warning",
            })
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        say(f"notification failed: {type(e).__name__}: {e}")
        return False
    if key:
        STATE.mkdir(parents=True, exist_ok=True)
        _stamp(key).touch()
    return True
