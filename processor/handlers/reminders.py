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
        # "already set" and "set" are different facts about the pass.
        log(f"    reminder {rec['id']} already set for {local} ({label})")
        return {"id": rec["id"], "at": rec.get("at"), "at_local": local,
                "tz": label, "already_set": True}
    log(f"    reminder set for {local} ({label}) — {text}")
    return {"id": rec["id"], "at": rec["at"], "at_local": local, "tz": label}
