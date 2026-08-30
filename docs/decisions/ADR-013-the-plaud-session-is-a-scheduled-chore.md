# ADR-013 — The Plaud session is a scheduled chore, not a repair

**Status:** Accepted
**Date:** 2026-08-30
**Related:** ADR-002 (the web fetcher, and why no password is stored), ADR-003 (the credential lives on the agent host), ADR-010 (severity decides the channel), `ingest/plaud_web.py`, `ops/reseed-plaud.sh`

## Context

On 2026-08-27 at 15:17 the Plaud session expired. Ingest crash-looped every 15
minutes until 2026-08-30 — three days, roughly 290 failed passes at ~9.4s CPU
and ~600 MB peak RSS each — and nobody noticed until someone asked how the
timers were doing.

The detection worked. `_alarm_dead_session` fired, the heartbeat's crash-loop
check reported `BAD atticus-ingest.service last run exited non-zero`, and the
escalation gate was counting correctly. What failed is that all of it fired
*after* the session was already dead, and the fix takes a person at a browser.
Every alarm here was a notification about something that was already too late
to prevent.

Three facts settle the shape of the response:

1. **The 30-day window is not extended by use.** The dead session had been
   exercised every 15 minutes for its entire life and expired on schedule
   anyway. Plaud's refresh token dates from the interactive login, full stop.
   There is no "keep it warm" trick.
2. **Re-authentication cannot be automated.** ADR-002 chose a 1Password-backed
   browser session precisely so no password lives on disk, and explicitly
   rejected the community toolkits that want one. Automating the login means
   storing a credential we decided not to store.
3. **The expiry date is knowable in advance.** It is the login date plus ~30
   days. Nothing was reading that clock.

So the failure is not that the session died. It is that a **predictable,
dated, recurring event was being handled as an unexpected outage**.

## Decision

Treat the Plaud session the way you treat any credential with a known expiry:
put its renewal on the calendar and make the renewal cheap.

**Date the login ourselves.** `plaud_web.py login` writes `.seeded` into the
profile directory; `plaud_web.py session-age` reports the age and what remains
of the window. We do not try to read Plaud's own token expiry — the refresh
token is not present in the profile in any decodable form (the single JWT there
is Firebase's installation token), and reverse engineering their storage layout
is exactly the fragile coupling ADR-002 avoids. A profile seeded before the
stamp existed falls back to Chromium's creation-time files, taking the *oldest*
candidate: over-estimating the age warns early, which costs nothing, while
under-estimating it warns after the session is already dead, which is the
failure this ADR exists to prevent.

**Warn before it dies, at ALERT, at most once a day.** A heartbeat check nudges
for the last `PLAUD_SESSION_WARN_DAYS` (default 5) of each window. It is
deliberately **a note, never a `problems` entry**:

- Folding it into `problems` would make the heartbeat exit 1 and fire a
  CRITICAL every hour for five days out of every thirty. `ops/heartbeat.py`
  already warns twice, in its own comments, that an alarm which always fires is
  one the operator learns to ignore — and #77 is the case study for what that
  costs when a real alarm finally arrives.
- CRITICAL is the class reserved for a pipeline that is already broken
  (ADR-010). A chore with five days of slack has not earned the channel that
  books a calendar event. The shared 6h throttle is also too loose here: four
  pushes a day for five days is twenty messages to convey one task, so the
  check gates itself to 24h.
- Once the session actually expires, this check goes quiet and says so.
  Ingest's dead-session alarm owns that case at CRITICAL, and saying it twice
  in two severities helps nobody.

**Back off while it is dead.** After three consecutive auth failures ingest
attempts hourly rather than every 15 minutes (`PLAUD_AUTH_RETRY_MINUTES`). The
pass still exits 3, so the unit stays `failed`, the heartbeat keeps reporting
it, and the alarms keep their own schedule — the only thing that stops is a
browser launch that cannot possibly succeed. Detection of a *recovery* slows
from 15 minutes to at most an hour, which is nothing against a fix that waits
on a human anyway, and the backoff clears itself the moment a pass succeeds.

**Make the chore one command.** `ops/reseed-plaud.sh` logs in, verifies the new
session *before* shipping it, rsyncs the profile to the ingest host, verifies it
there, and restarts the unit.

## Alternatives rejected

**Store the password and automate the login.** Reverses ADR-002 for a
convenience. The whole reason we are not using the community toolkits is that
they want a plaintext password, and this credential now shares a host with an
agent that executes text derived from ambient audio (ADR-003).

**Xvfb + x11vnc on Forge, so re-seeding happens where the session lives.** This
is genuinely tempting: it removes the second machine and the copy. But Forge
runs the autonomous agent, and the sandbox shares its network namespace by
default — SECURITY.md already names loopback services as inside the blast
radius, which is why approval arrives out of band (ADR-009). A VNC server
displaying a live, logged-in Plaud session is a much richer target than an
approve button. Seed on a desktop and copy; the copy is cheap and the trade
is not.

**Alarm harder when it dies.** This was the instinct, and it is the wrong one.
The alarms already worked. Adding volume to a message that arrives after the
deadline does not move the deadline.

**Read Plaud's token expiry out of the profile.** Would give an exact date
instead of an estimate. It is not there to read, and if it were, it would
couple us to their storage layout — the R1 risk ADR-002 spends a paragraph on.
Our own stamp is a narrower and more stable target, which is the same reasoning
that made us call the JSON API instead of scraping the DOM.

## Consequences

The session still expires every ~30 days and still needs a human. What changes
is that the human is told five days early, told once a day rather than four
times, and can discharge it with one command instead of four steps out of
`docs/SPEC.md`. A missed window degrades to exactly today's behaviour, minus
~290 pointless Chromium launches.

The warning is only as good as the stamp. A profile copied between machines by
hand, rather than by `reseed-plaud.sh`, carries its stamp with it — which is
correct, since the login date travels with the session — but a profile seeded
by some future path that forgets to stamp falls back to the estimate, and the
heartbeat says so in the message rather than quietly guessing.
