---
name: reminders
description: |
  Sets a one-shot reminder that pushes to the operator's phone AT A PARTICULAR
  TIME. Use when the request names a moment — "remind me at four to call the
  bank", "in twenty minutes tell me to take the pizza out", "tomorrow morning
  at nine remind me to send the invoice", "give me a nudge at half past two",
  "wake me up in an hour". The words to look for are a CLOCK TIME or an
  ELAPSED INTERVAL. Writes an intent file; the pipeline stores it and a timer
  delivers the push after you exit — you cannot send it yourself.
  Do NOT use this for a task with no delivery moment ("add milk to my list",
  "remind me to renew the domain sometime", "don't let me forget about the
  prescription") — that is the `todo` skill, and a list item is what the
  operator wants there. Do NOT use it for a meeting, an appointment, or
  anything with a duration or other attendees — that is a calendar event, not
  a reminder. Do NOT use it for a recurring schedule ("every morning at
  seven"): only one-shot reminders exist, and inventing a repeat would silently
  deliver once. Do NOT use it to schedule work for Atticus itself to do later —
  nothing here re-runs the agent, it only pushes text at a person.
---

# reminders

"Remind me at four to call the bank." One sentence, spoken into a pin, and the
whole job is that a phone buzzes at four with something legible on it.

**This is not a todo.** A todo has content and no deadline behaviour. A reminder
is nothing *but* deadline behaviour — the text is almost incidental, and if it
arrives at the wrong moment it has failed completely, however well written it is.
That is the routing test, and it is the only one that matters here:

| the request | skill | why |
|---|---|---|
| "remind me **at four** to call the bank" | `reminders` | a moment |
| "remind me **in twenty minutes**" | `reminders` | a moment |
| "remind me to renew the domain" | `todo` | no moment; it belongs on a list |
| "put milk on the shopping list" | `todo` | no moment |
| "book me in with Sam at four on Thursday" | neither | that is a calendar event |

If a request has *both* — "add the prescription to my list and remind me at five
to pick it up" — that is two intents and gets two files, one per skill. Do not
collapse them; a list item and a push at five are different things the operator
asked for separately.

## The verb

One verb. There is no read, no list, no cancel, and no repeat.

| verb | does | required | optional |
|---|---|---|---|
| `reminders.set` | stores one push for one moment | `text`, and one of `at` / `in_minutes` | `said` |

- **`text`** — what the push says. Bounded to 200 characters, because it lands on
  a lock screen.
- **`in_minutes`** — a number of minutes from now. **Prefer this whenever the
  request was relative** ("in twenty minutes", "in an hour"). It needs no
  timezone at all, so it cannot be broken by a misconfigured one.
- **`at`** — a **local wall-clock** time, ISO-8601, **with no zone suffix**:
  `"2026-08-01T16:00"`. Read the next section before you write one.
- **`said`** — the words you heard for the time, verbatim: `"at four"`. Optional
  and worth including. It is stored beside the reminder, so when a push arrives at
  the wrong moment the operator can see what was actually heard instead of
  guessing.

```json
{"verb": "reminders.set", "text": "Call the bank", "at": "2026-08-01T16:00", "said": "at four"}
```

```json
{"verb": "reminders.set", "text": "Take the pizza out of the oven", "in_minutes": 20}
```

## Time, which is the only hard part of this skill

**Write the local wall-clock time. Do not convert anything to UTC.**

Everything else in this system is UTC by convention, so the instinct is to
convert. Resist it. You do not know the name of the operator's timezone and you
must not guess one — a reminder an hour out because you assumed the wrong offset
across a daylight-saving boundary is indistinguishable from a broken feature. The
pipeline knows the zone (`ATTICUS_LOCAL_TZ`) and attaches it. Your job is what the
speaker meant by "four"; the zone is not your half of the problem.

**Your clock is the operator's clock.** `/etc/localtime` is mounted into your
sandbox, so `date` and a naive `datetime.now()` both give local time on the
machine the operator is standing next to. Check it — do not assume today's date,
and do not assume what "morning" resolves to from context alone.

Then:

- **"at four" means the next four o'clock.** If it is 2pm, that is today; if it is
  6pm, that is tomorrow. Nobody sets a reminder for a time that has already gone.
- **Assume the plausible half of the day.** "At four" from someone awake and
  talking is 16:00, not 04:00. "Half seven" in the evening is 19:30. Only write an
  early-morning time if the request actually says so ("at four in the morning").
- **Round vague times to something defensible and say what you did in the
  report.** "Mid-afternoon" → 15:00. "First thing" → 08:00. "Tonight" → 20:00.
  Never refuse a reminder over vagueness; a nudge at a reasonable guess beats no
  nudge, and `said` records what you heard.
- **If you cannot pin a moment at all** — "remind me about this later", "sometime
  next week" — that is a `todo`, not a reminder. Do not invent a time to force it
  through here.
- **A bare `"16:00"` is accepted** as the next occurrence of that local time. Use
  it if you are confident of the clock time but not the date; a full date is still
  better.

Sanity bounds the pipeline enforces, so you see them as refusals rather than as
silence: further out than a year is rejected as a misparsed date, and so is
anything more than a day in the past.

## Writing the text

It is read on a lock screen, at the moment it matters, by someone who has
forgotten the conversation that produced it.

- **Imperative and complete.** "Call the bank about the transfer", not "bank" and
  not "reminder about the thing you mentioned".
- **Carry the detail that makes it actionable** — a name, a number, the reason —
  if it was in the request. One clause is usually enough; you have 200 characters
  and using half of them is fine.
- **No preamble.** Not "Reminder:", not "You asked me to remind you to…". The push
  already says it is a reminder and the time is appended for you.
- **Write it so it survives out of context.** "Ask him about it" is useless at
  four o'clock. "Ask Robbie about the deploy window" is not.

## What happens next, so your report is honest

Once stored, delivery is a one-minute timer, not your run — so the reminder is
**pending**, never sent, at the moment you write your report. Say so in those
terms.

- Delivery is a push to the operator's own ntfy topic. The **local time and the
  zone are appended to every reminder**, deliberately, so a timezone mistake is
  visible the first time rather than after weeks of vaguely mistimed pushes.
- If the machine is down when it comes due, it **fires late with a note** ("this
  was due at 4:00 PM — 3h 12m ago") for up to a day, then is reported as missed
  rather than dropped in silence.
- Setting the same reminder twice — same instant, same text — is one reminder, so
  re-running a recording cannot double a push.
- Nothing here can cancel or list reminders, and there is no repeat. If the
  request was "cancel the four o'clock one" or "every morning at seven", say
  plainly in your report that Atticus cannot do that yet.

Then write your usual short deliverable. A reminder is a one-line outcome, so it
is a one-paragraph `output/index.html` saying what was set, for when, and in whose
words — not a report about reminders.

## Causing something to happen outside this sandbox

You hold no credentials and you cannot reach any external service. To make
something happen, declare the intent and the pipeline performs it after you exit.

Write one JSON file per action into `./output/outbox/`, named `NNN-verb.json`
where `NNN` is a zero-padded sequence number that sets the order they run in:

```json
{"verb": "<service>.<action>", "...": "action-specific fields"}
```

Rules:

- **One action per file.** Never a list.
- **The verb must be one the pipeline knows.** An unknown verb is refused and
  reported, not silently dropped — so do not invent one.
- **Ordering is the filename.** `001-` runs before `002-`.
- Anything outward-facing may be **held for confirmation** rather than performed
  immediately. That is normal and not a failure. Write your report as though the
  action is pending, never as though it is done.
- Also write your usual HTML deliverable. The outbox is in addition to it, not
  instead of it: the report is what the operator reads to find out what you did.
