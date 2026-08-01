"""The `reminders.set` verb. Issue #52.

`INTERNAL` risk, and that classification is the honest one rather than the
convenient one: a reminder is visible only to the operator, on the operator's own
push topic, and undoing it is one line in an append-only ledger. Nothing leaves
the operator's own infrastructure — `outbox.py`'s own comment names a reminder as
the example of the class. So it runs unattended, which it has to: holding a
reminder for confirmation until a human is present defeats the entire point of
setting one by voice and walking away.

Unlike every other handler this one needs **no credential at all**, so it is the
one that cannot fail with "SOMETHING_TOKEN is not configured". What it can fail
with is a time it cannot read, and it says so in the receipt — that receipt is
committed beside the report, so a reminder the operator never received can be
traced to the sentence that asked for it.

Storing is all that happens here. Delivery is `processor/reminders.py`'s drain on
its own timer, because a pass that spends up to ATTICUS_EXEC_TIMEOUT on an agent
run is the wrong thing to hang a stopwatch off.
"""
import outbox
import reminders as store          # processor/reminders.py — the ledger and drain


def _calendar_companion(when, text: str, cfg, log) -> dict | None:
    """Also put the reminder on the operator's own calendar. Issue #66.

    The operator's verdict on ntfy delivery was "too soft — it drowns among the
    other notifications", and on iOS the only free notification class that
    breaks through Focus is Time Sensitive, which calendar alerts get. Pushcut
    (a literal Clock alarm) was rejected on cost. So: the push stays, and a
    15-minute event with an alert at start doubles it on the strong channel.

    This calls the outlook.event handler FUNCTION, deliberately not the outbox
    gate in front of it. outlook.event is TRACKED because an event can carry
    attendees — visible to other people the moment it lands. This request is
    synthesized by the pipeline with a fixed shape: the operator's own calendar,
    NO attendees ever, subject bounded by the reminder's own MAX_TEXT. With
    nothing outward it is internal-shaped, same class as the reminder itself.
    Do not add an attendees field here; that would cross the gate this comment
    exists to mark.

    Best-effort by design: Calendars.ReadWrite is not consented until the
    operator runs `m365-auth` with the wider scope, and a reminder that pushes
    but has no calendar event is degraded, not failed. The receipt says which
    of the two channels are armed.
    """
    if str(getattr(cfg, "reminder_calendar", "on") or "on").strip().lower() == "off":
        return None
    from handlers import outlook
    minutes = max(1, int(getattr(cfg, "reminder_event_minutes", 15) or 15))
    req = {"subject": f"⏰ {text}",
           "start": store.iso_z(when),        # tz-aware Z form → UTC in Graph
           "minutes": minutes,
           "alert_minutes_before": 0,
           "body": "Set by Atticus reminders (#66). The ntfy push fires at the "
                   "same moment; this event exists because a calendar alert "
                   "breaks through Focus and a push does not."}
    try:
        ev = outlook.event(req, cfg, log=log)
        return {"created": True, "id": ev.get("id"), "web_link": ev.get("web_link")}
    except outbox.OutboxError as e:
        # Expected until consent is granted; the message names the fix.
        log(f"    reminder: calendar event skipped — {e}")
        return {"created": False, "reason": str(e)[:300]}


def _describe(req: dict) -> str:
    """What the operator reads in the receipt.

    Deliberately echoes the request's OWN words for the time rather than the
    resolved UTC instant: if the reminder arrives at the wrong moment, the useful
    question is what was asked for, and the resolved value is in the ledger.
    """
    text = " ".join(str(req.get("text") or "").split())[:80] or "(no text)"
    if req.get("in_minutes") not in (None, ""):
        when = f"in {req['in_minutes']} min"
    else:
        when = str(req.get("at") or "?")
    said = " ".join(str(req.get("said") or "").split())
    return f'remind: "{text}" at {when}' + (f' (heard "{said}")' if said else "")


@outbox.handler("reminders.set", risk=outbox.INTERNAL, schema=("text",),
                describe=_describe)
def set_reminder(req: dict, cfg, log=print) -> dict:
    try:
        text = store.clean_text(req.get("text"))
        when, label = store.resolve_when(req, cfg)
    except store.ReminderError as e:
        # Translate to the outbox's own error so process() records it as a failed
        # request with a readable reason instead of a traceback.
        raise outbox.OutboxError(str(e))

    zone, _ = store.local_zone(cfg)
    rec = store.add(cfg.vault, when=when, text=text, zone_label=label,
                    said=str(req.get("said") or "").strip(),
                    source=str(req.get("_file") or ""))
    local = store.fmt_local(when, zone)
    if rec.get("duplicate"):
        # Same instant, same text — the id is derived from both, so a re-run of one
        # recording cannot double a reminder. Report it rather than staying quiet:
        # "already set" and "set" are different facts about the pass. The calendar
        # companion is skipped for the same reason the ledger append is: it was
        # made (or attempted) when the reminder was first set.
        log(f"    reminder {rec['id']} already set for {local} ({label})")
        return {"id": rec["id"], "at": rec.get("at"), "at_local": local,
                "tz": label, "already_set": True}

    cal = _calendar_companion(when, text, cfg, log)
    if cal and cal.get("created"):
        # Recorded on the ledger too, so "did that get a calendar event?" is
        # answerable from the vault long after the receipt scrolls away.
        store.append(cfg.vault, rec["id"], store.PENDING,
                     calendar_event_id=cal.get("id"))

    log(f"    reminder set for {local} ({label}) — {text}")
    out = {"id": rec["id"], "at": rec["at"], "at_local": local, "tz": label}
    if cal is not None:
        out["calendar"] = cal
    return out
