"""A calendar event used as a notification channel — the one that gets through.

Issue #91, built on what #66 established: **ntfy alone is not enough.** The
ntfy iOS app has never wired up the entitlements for priority 4/5 to bypass
Focus or Do Not Disturb (their most-upvoted open iOS issue), so a high-priority
push into that app is a normal notification wearing a costume. A calendar event
with an alert at its start is Time Sensitive, which does break through Focus,
needs no new app, and costs nothing.

Reminders proved it on 2026-08-01 and the operator confirmed both channels
arrived. This module is that mechanism extracted so alarms can use it too,
because the same week produced two failures that argue for it:

  * ingest died for 2 days 6 hours (#77) — the heartbeat DID detect it and DID
    alarm, into ntfy, throttled to one per 6h, where it drowned among routine
    pushes;
  * `atticus-vault-site.path` sat failed for 1.5 days for the same reason, and
    surfaced only when the operator tapped a notification and got a 404.

Both were delivery failures, not detection failures. The alarm net works; it
was shouting into the wrong channel.

## What this deliberately is not

Not a general calendar API — `outlook.event` is that, and this calls it. Not a
new credential: it reuses the m365 store and the same `Calendars.ReadWrite`
consent reminders already needs. And **never fatal**: every path returns rather
than raising, because an alarm that fails to escalate must still have been an
alarm. The caller has already sent the ntfy copy by the time this runs.
"""
from datetime import UTC, datetime, timedelta

# Emoji rather than the word "ALERT": this lands on a lock screen among ordinary
# meetings, and the glyph is what makes it scannable at a glance.
SUBJECT_PREFIX = "🚨 Atticus"

# The event starts a couple of minutes out, not now. An event whose start has
# already passed does not reliably produce an alert on any client — it is a
# thing that already happened. Two minutes is soon enough to read as immediate
# and far enough to survive clock skew between Forge and Microsoft.
LEAD_MINUTES = 2


def create(cfg, *, subject: str, body: str = "", minutes: int = 15,
           lead_minutes: int = LEAD_MINUTES, log=print) -> dict:
    """Book a short event whose alert fires at its start. Never raises.

    Returns {"created": bool, ...} — a reason when it could not, so the caller
    can say so in a receipt rather than guessing.
    """
    # Imported here, not at module scope: this module is imported by notify.py,
    # which ingest also imports, and ingest must not require the outbox handler
    # graph (or `requests`) merely to send a push.
    #
    # Inside the try for the same reason. Deferring the import kept it off
    # ingest's import path but not out of ingest's *call* path: on an alarm,
    # ingest reached here, `handlers/__init__` imported `ado`, `ado` imported
    # `requests` — absent from the fetchers venv — and the ModuleNotFoundError
    # escaped a function documented above as never raising, killing the poller
    # mid-alarm. Every escalation this module ever attempted from ingest died
    # here (`calendar=False` on all of them). An import failure is one more way
    # the calendar channel can be unavailable, so it returns like the rest.
    try:
        from handlers import outlook
        from outbox import OutboxError
    except ImportError as e:
        log(f"    escalation: no calendar alert — {type(e).__name__}: {e}")
        return {"created": False, "reason": f"{type(e).__name__}: {e}"}

    when = datetime.now(UTC) + timedelta(minutes=max(0, int(lead_minutes)))
    req = {
        "subject": f"{SUBJECT_PREFIX} — {subject}"[:250],
        "start": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "minutes": max(1, int(minutes or 15)),
        # AT the start. The start is the moment the operator should look, and
        # Graph's client-side default (usually 15 minutes before) would fire
        # the alert before the event this module just created.
        "alert_minutes_before": 0,
        "body": (body or "").strip() or
                "Raised by Atticus. This is a calendar event because a push "
                "notification does not break through Focus and this does.",
    }
    try:
        ev = outlook.event(req, cfg, log=lambda m: None)
    except OutboxError as e:
        # The expected state until Calendars.ReadWrite is consented. Say it once,
        # quietly: the ntfy copy has already gone out.
        log(f"    escalation: no calendar alert — {str(e)[:160]}")
        return {"created": False, "reason": str(e)[:300]}
    except Exception as e:                                      # noqa: BLE001
        log(f"    escalation: calendar alert failed — {type(e).__name__}: {e}")
        return {"created": False, "reason": f"{type(e).__name__}: {e}"}
    log(f"    escalated to a calendar alert at {req['start']}")
    return {"created": True, "id": ev.get("id"), "web_link": ev.get("web_link"),
            "start": req["start"]}
