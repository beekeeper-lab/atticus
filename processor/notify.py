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
