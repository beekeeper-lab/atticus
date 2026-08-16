"""Severity routing, escalation and quiet hours (#91).

This exists because of two incidents in one week, both of which were DELIVERY
failures rather than detection failures:

  * ingest died for 2 days 6 hours (#77). The `F_CHANGED` branch alarmed on
    nothing at all, and the heartbeat's own alarm went to ntfy, throttled to one
    per 6h, where it drowned among routine pushes.
  * `atticus-vault-site.path` sat failed for 1.5 days and surfaced only when the
    operator tapped a notification and got a 404.

So the properties worth pinning are about *where* a message goes and *when it
is allowed not to go*:

  * critical escalates to the calendar channel, which breaks through Focus;
  * it escalates only after persistence, so one bad pass stays quiet;
  * the strong channel has its own longer throttle, so a crash-loop books ONE
    event rather than ninety-six;
  * quiet hours never DROP anything — parked is not lost;
  * critical ignores quiet hours entirely.

No network: `notify()` is monkeypatched at the module boundary and the calendar
channel is replaced with a recorder, so nothing here can reach Graph or ntfy.
"""
import json
from datetime import datetime

import notify as nf
import pytest
from zoneinfo import ZoneInfo

ZONE = "America/New_York"


@pytest.fixture
def ncfg(cfg, tmp_path):
    cfg.notify_url = "https://ntfy.example/atticus"
    cfg.local_tz = ZONE
    cfg.quiet_hours = ""
    cfg.notify_escalate = "on"
    cfg.escalate_after_failures = 3
    cfg.escalate_throttle_hours = 12
    cfg.alarm_throttle_hours = 6
    cfg.vault = tmp_path / "vault"
    cfg.vault.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Records pushes and calendar escalations; isolates the stamp directory so
    a real ~/.cache/atticus cannot make a test pass or fail."""
    sent, cal = [], []
    monkeypatch.setattr(nf, "STATE", tmp_path / "cache")

    def fake_notify(cfg, text, log=None, key=None, title="Atticus",
                    tags="warning", priority="high"):
        hours = getattr(cfg, "alarm_throttle_hours", 6)
        if key and nf.throttled(key, hours):
            return False
        sent.append({"text": text, "key": key, "title": title,
                     "priority": priority, "tags": tags})
        if key:
            nf.STATE.mkdir(parents=True, exist_ok=True)
            nf._stamp(key).touch()
        return True

    def fake_cal(cfg, *, subject, body, log=None):
        cal.append({"subject": subject, "body": body})
        return {"created": True, "id": f"EV{len(cal)}"}

    monkeypatch.setattr(nf, "notify", fake_notify)
    monkeypatch.setattr(nf, "calendar_escalate", fake_cal)
    return {"sent": sent, "cal": cal}


# ── severity → channel ──────────────────────────────────────────────────────
def test_routine_is_one_push_at_default_priority(ncfg, wire):
    out = nf.alarm(ncfg, "your report is ready", severity=nf.ROUTINE)
    assert out == {"ntfy": True, "calendar": False, "deferred": False}
    assert wire["sent"][0]["priority"] == "default"
    assert wire["cal"] == []


def test_alert_pushes_but_never_escalates(ncfg, wire):
    out = nf.alarm(ncfg, "an action is awaiting approval", severity=nf.ALERT,
                   streak=99)
    assert out["ntfy"] and not out["calendar"]
    assert wire["sent"][0]["priority"] == "high"


def test_critical_escalates_once_persistence_is_reached(ncfg, wire):
    """The whole point. #77 ran for 2d6h on ntfy alone."""
    first = nf.alarm(ncfg, "ingest is dead", severity=nf.CRITICAL,
                     key="plaud-auth", streak=1, title="ingest")
    assert first["ntfy"] and not first["calendar"], "one bad pass is usually transient"

    nf.clear("plaud-auth")            # so the ntfy throttle is not what we measure
    third = nf.alarm(ncfg, "ingest is dead", severity=nf.CRITICAL,
                     key="plaud-auth", streak=3, title="ingest")
    assert third["calendar"] is True
    assert "ingest" in wire["cal"][0]["subject"]
    assert wire["sent"][-1]["priority"] == "urgent"


def test_an_overnight_crash_loop_books_one_event_not_ninety_six(ncfg, wire):
    for i in range(20):
        # Simulate the 6h NTFY window expiring — by removing only that stamp.
        # Not clear(), which means "the condition recovered" and deliberately
        # resets the escalation window too.
        nf._stamp("plaud-auth").unlink(missing_ok=True)
        nf.alarm(ncfg, "still dead", severity=nf.CRITICAL, key="plaud-auth",
                 streak=3 + i, title="ingest")
    assert len(wire["cal"]) == 1, "the calendar channel has its own longer throttle"


def test_recovery_clears_the_escalation_window_too(ncfg, wire):
    nf.alarm(ncfg, "dead", severity=nf.CRITICAL, key="k", streak=3, title="t")
    assert len(wire["cal"]) == 1
    # Broke, recovered, broke again: it must escalate again rather than sit
    # inside a 12-hour window in silence.
    nf.clear("k")
    nf.alarm(ncfg, "dead again", severity=nf.CRITICAL, key="k", streak=3, title="t")
    assert len(wire["cal"]) == 2


def test_escalation_can_be_switched_off(ncfg, wire):
    ncfg.notify_escalate = "off"
    out = nf.alarm(ncfg, "dead", severity=nf.CRITICAL, key="k", streak=9, title="t")
    assert out["ntfy"] and not out["calendar"]


def test_a_failing_calendar_channel_does_not_lose_the_push(ncfg, wire, monkeypatch):
    """Escalation is best-effort. The ntfy copy has already gone out by then,
    and an alarm that cannot escalate must still have been an alarm."""
    monkeypatch.setattr(nf, "calendar_escalate",
                        lambda *a, **k: {"created": False, "reason": "no consent"})
    out = nf.alarm(ncfg, "dead", severity=nf.CRITICAL, key="k", streak=5, title="t")
    assert out["ntfy"] is True and out["calendar"] is False


# ── quiet hours ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("hhmm,expected", [
    ("23:30", True), ("03:00", True), ("06:59", True),
    ("07:00", False), ("12:00", False), ("21:59", False), ("22:00", True),
])
def test_quiet_hours_window_crosses_midnight(ncfg, hhmm, expected):
    ncfg.quiet_hours = "22:00-07:00"
    h, m = (int(x) for x in hhmm.split(":"))
    now = datetime(2026, 8, 2, h, m, tzinfo=ZoneInfo(ZONE))
    assert nf.in_quiet_hours(ncfg, now=now) is expected


def test_a_same_day_window_also_works(ncfg):
    ncfg.quiet_hours = "01:00-06:00"
    assert nf.in_quiet_hours(ncfg, now=datetime(2026, 8, 2, 3, 0, tzinfo=ZoneInfo(ZONE)))
    assert not nf.in_quiet_hours(ncfg, now=datetime(2026, 8, 2, 9, 0, tzinfo=ZoneInfo(ZONE)))


@pytest.mark.parametrize("bad", ["", "nonsense", "22:00", "aa:bb-cc:dd", "22:00-22:00"])
def test_a_malformed_window_disables_quiet_hours_rather_than_silencing(ncfg, bad):
    """The failure mode of a typo here must be too many notifications, never
    too few."""
    ncfg.quiet_hours = bad
    assert nf.in_quiet_hours(ncfg) is False


def test_routine_inside_quiet_hours_is_parked_not_dropped(ncfg, wire, monkeypatch):
    ncfg.quiet_hours = "22:00-07:00"
    monkeypatch.setattr(nf, "in_quiet_hours", lambda *a, **k: True)
    out = nf.alarm(ncfg, "your report is ready", severity=nf.ROUTINE,
                   title="Atticus finished")
    assert out == {"ntfy": False, "calendar": False, "deferred": True}
    assert wire["sent"] == []
    rows = [json.loads(ln) for ln in
            (ncfg.vault / nf.DEFERRED).read_text().splitlines() if ln.strip()]
    assert rows[0]["title"] == "Atticus finished"


def test_critical_ignores_quiet_hours(ncfg, wire, monkeypatch):
    """That is the entire point of the class."""
    monkeypatch.setattr(nf, "in_quiet_hours", lambda *a, **k: True)
    out = nf.alarm(ncfg, "ingest is dead", severity=nf.CRITICAL, streak=5,
                   key="k", title="ingest")
    assert out["ntfy"] and out["calendar"] and not out["deferred"]


def test_take_deferred_returns_and_clears(ncfg, wire, monkeypatch):
    monkeypatch.setattr(nf, "in_quiet_hours", lambda *a, **k: True)
    nf.alarm(ncfg, "one", severity=nf.ROUTINE, title="a")
    nf.alarm(ncfg, "two", severity=nf.ALERT, title="b")
    rows = nf.take_deferred(ncfg)
    assert [r["text"] for r in rows] == ["one", "two"]
    assert nf.take_deferred(ncfg) == [], "draining twice must not repeat them"
    assert not (ncfg.vault / nf.DEFERRED).exists()


def test_take_deferred_on_a_host_with_nothing_parked(ncfg):
    assert nf.take_deferred(ncfg) == []


def test_a_torn_deferred_line_does_not_lose_the_rest(ncfg, wire, monkeypatch):
    monkeypatch.setattr(nf, "in_quiet_hours", lambda *a, **k: True)
    nf.alarm(ncfg, "one", severity=nf.ROUTINE, title="a")
    with (ncfg.vault / nf.DEFERRED).open("a") as f:
        f.write('{"at": "broken"')
    nf.alarm(ncfg, "two", severity=nf.ROUTINE, title="b")
    assert [r["text"] for r in nf.take_deferred(ncfg)] == ["one", "two"]


def test_an_unknown_severity_falls_back_to_alert_rather_than_silence(ncfg, wire):
    out = nf.alarm(ncfg, "?", severity="apocalyptic")
    assert out["ntfy"] is True and out["calendar"] is False


# --- the escalation channel's own failure modes ---------------------------
#
# Everything above replaces `calendar_escalate` with a recorder, which is right
# for testing routing but meant the real `calendar_alert.create` was never once
# executed by the suite. It shipped documented as "never fatal" and was not:
# its `from handlers import outlook` sat OUTSIDE the try, and on the ingest host
# that import chain reaches `handlers/ado.py` → `import requests`, absent from
# the fetchers venv. So every alarm ingest raised died mid-escalation with a
# ModuleNotFoundError, taking the poller down with it and leaving `calendar=False`
# on all three alarms it managed to send between 2026-08-06 and 2026-08-16.
#
# An alarm that fails to escalate must still have been an alarm.

import builtins

import calendar_alert

# Captured at import, before any fixture swaps it for a recorder.
_REAL_ESCALATE = nf.calendar_escalate


def test_create_returns_rather_than_raises_when_the_handler_graph_is_absent(
        monkeypatch):
    """The ingest host's exact shape: `requests` is not installed."""
    real_import = builtins.__import__

    def no_requests(name, *a, **k):
        if name == "requests" or name.split(".")[0] == "handlers":
            raise ModuleNotFoundError("No module named 'requests'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_requests)
    out = calendar_alert.create(None, subject="ingest is down", log=lambda m: None)
    assert out["created"] is False
    assert "requests" in out["reason"]


def test_a_failed_escalation_does_not_take_the_alarm_down(ncfg, wire, monkeypatch):
    """The property the outage actually violated, end to end.

    `wire` is deliberately un-stubbed on the calendar side here: this drives the
    REAL calendar_escalate → calendar_alert.create against the ingest host's
    import environment. ntfy still goes out, alarm() returns, and the caller
    lives to keep polling.
    """
    real_import = builtins.__import__

    def no_requests(name, *a, **k):
        if name == "requests" or name.split(".")[0] == "handlers":
            raise ModuleNotFoundError("No module named 'requests'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(nf, "calendar_escalate", _REAL_ESCALATE)
    monkeypatch.setattr(builtins, "__import__", no_requests)
    out = nf.alarm(ncfg, "upstream changed", severity=nf.CRITICAL,
                   key="plaud-upstream", streak=9, title="ingest")
    assert out["ntfy"] is True
    assert out["calendar"] is False
