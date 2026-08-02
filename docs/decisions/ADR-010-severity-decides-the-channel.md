# ADR-010 — Severity decides the channel, because ntfy alone was not heard

**Status:** Accepted
**Date:** 2026-08-02
**Issue:** [#91](https://github.com/beekeeper-lab/atticus/issues/91), closing item 1 of [#77](https://github.com/beekeeper-lab/atticus/issues/77)
**Related:** [#66](https://github.com/beekeeper-lab/atticus/issues/66) (reminders needed the same thing), `processor/notify.py`, `processor/calendar_alert.py`

## Context

This project's stated thesis is that **the failures that matter are the quiet
ones**, and its engineering is largely organised around that: per-condition
alarms, clear-on-recovery, an explicit warning when no notification URL is set,
a heartbeat that watches the watchers.

Then two multi-day outages happened in one week, and both were **delivery
failures rather than detection failures**:

- **Ingest was dead for 2 days 6 hours** (#77). Plaud changed their auth on
  2026-07-29 and every 15-minute poll failed — roughly 215 of them. The
  `F_CHANGED` branch in `ingest/poller.py` printed to stderr and returned with
  **no alarm at all**. The heartbeat *did* notice the crash-looping unit and
  *did* alarm, into ntfy, throttled to one message per six hours, where it
  drowned among routine pushes. It surfaced only because a spoken command never
  arrived while somebody happened to be watching.
- **`atticus-vault-site.path` sat failed for 1.5 days.** Same shape. Discovered
  when the operator tapped a fresh result notification and got a 404.

Separately and earlier, #66 had already established the underlying fact: **the
ntfy iOS app cannot bypass Focus or Do Not Disturb at all.** The entitlements
are not wired up; it is the most-upvoted open issue on their repository. A
"high priority" push into that app is an ordinary notification wearing a
costume. Reminders solved it by also booking a calendar event whose alert fires
at the moment — Time Sensitive, which does break through — and the operator
confirmed both channels arriving on 2026-08-01.

So the machinery to shout existed. What was missing was a rule about when to use
it.

## Decision

**Three severities, chosen on consequence rather than feeling, and the severity
picks the channel.**

| severity | meaning | ntfy | calendar alert | quiet hours |
|---|---|---|---|---|
| `critical` | broken or silently losing work; only a human can fix it | urgent | **yes** | ignored |
| `alert` | needs a decision, nothing is being lost | high | no | deferred |
| `routine` | a result | default | no | deferred |

Two guards keep the strong channel meaningful, because a channel that fires too
often stops being strong:

- **Escalate on persistence, not on one bad pass.** `ATTICUS_ESCALATE_AFTER_FAILURES`
  (default 3). A single failed poll is usually transient, and a calendar event
  for it would teach the operator to ignore calendar events.
- **The calendar channel has its own, longer throttle.**
  `ATTICUS_ESCALATE_THROTTLE_HOURS` (default 12), against ntfy's 6. A
  15-minute timer failing all night must book one event, not ninety-six.
  Recovery clears both windows, so a condition that breaks again after
  recovering escalates immediately rather than sitting silent for the rest of
  the day.

**Quiet hours never drop anything.** `ATTICUS_QUIET_HOURS` parks `routine` and
`alert` in `.state/deferred-notifications.jsonl`, and the 07:00 briefing opens
with what arrived overnight. `critical` ignores the window entirely — that is
the point of the class.

## Consequences

- `ingest/poller.py`'s `F_CHANGED` branch now alarms as `critical`. That is item
  1 of #77 and the specific silence that cost 2d6h.
- The heartbeat escalates on a repeated identical fingerprint — hourly, so a
  third identical report is roughly three hours of a broken system.
- `calendar_alert.py` is extracted from the reminders handler; reminders behave
  exactly as before and now share the mechanism.
- **A channel strong enough to wake someone must not be used for routine
  completions**, which is why quiet hours exist in the same change rather than
  later. Escalation and restraint are one decision.

## What this does not fix

The calendar channel depends on `Calendars.ReadWrite` consent (granted
2026-08-01) and on the Microsoft account's calendar being on the operator's
phone with alerts enabled. If either lapses, escalation degrades to ntfy and
says so in the log — best-effort by design, because an alarm that cannot
escalate must still have been an alarm.

Nothing here improves *detection*. Both incidents were detected correctly. If a
future outage is silent, the fault will be somewhere else.
