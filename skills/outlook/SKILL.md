---
name: outlook
description: |
  Drafts an Outlook email or creates an Outlook calendar event from a spoken
  request — e.g. "draft a reply to finance saying I'll have it Thursday",
  "email Robbie at robbie@example.com asking for the migration date", "put an
  hour on Tuesday at 2 called Migration review". Creates a DRAFT for a human to
  send; it cannot send mail. It also CANNOT READ anything: this skill cannot
  search, read or summarise mail, cannot tell you what is on your calendar, and
  cannot look up a contact — that half is not built yet (see "What you cannot do"
  below). If the request only asks a question about existing mail, a schedule or
  a person, use this skill anyway but only to explain, in the report, that the
  answer is unavailable and why — never invent one.
verbs: [outlook.draft, outlook.event]
risk: tracked
outputs: [html]
cost: low
---

# outlook

Microsoft 365 mail, calendar and contacts. **One skill for all three** because they
are one credential and one CLI — a single app registration, one delegated token per
account. Splitting them would triple the plumbing and change nothing you can do.

Issues #44 (mail), #45 (calendar), #46 (contacts).

## What you cannot do, and why it is not a bug you should work around

**You cannot read anything from Microsoft 365. Not mail, not the calendar, not
contacts.** This is a property of the architecture, not a missing switch:

You hold no credentials. Everything credentialed happens in the pipeline *after you
exit*, through the outbox. That works for causing something — a draft, an event — and
it cannot work for a read, because a read has to give you data *during* your run, and
by then nothing has run the request. `processor/outbox.py` sets this out in full: the
options are a credential-holding broker you could query (powerful, and a large new
prompt-injection surface) or pipeline-side pre-fetch (safe, but cannot answer an
arbitrary question). Neither is built. It is a deliberate open decision.

A read tool called `m365` does exist on the operator's machine and reads all three
today — but it is not bound into your sandbox and there is no verb for it. There is no
`outlook.search`, no `outlook.calendar`, no `outlook.lookup`. **An unknown verb is
refused and reported, so do not invent one.**

So when the request is "what did Robbie say about the migration?", "what does tomorrow
look like?", or "who is Robbie Page?":

- Say plainly in your report that Atticus cannot read mail, calendar or contacts yet,
  and that the read half of #44/#45/#46 is unbuilt pending the decision in
  `outbox.py`.
- **Do not guess, and do not write a plausible-sounding summary of mail you have not
  seen.** A confident invention here is the worst possible output — it will be read as
  if someone checked.
- Do what you *can* do, if the request also contains one. "What did Robbie say, and
  draft him a reply" — you cannot do the first half; you can draft something the
  operator will edit, as long as you have an actual address. Say which half you did.

## What you can do

Two verbs, written into `./output/outbox/` as intent files.

### `outlook.draft` — create a draft. It does not send.

```json
{"verb": "outlook.draft",
 "to": "robbie@example.com",
 "cc": ["finance@example.com"],
 "subject": "Migration timing",
 "body": "Thursday works for the cutover. — sent from Atticus, drafted from a voice note."}
```

`to`, `subject` and `body` are required. `to` and `cc` take one address or a list.

**This creates a message in Drafts and stops.** There is no send verb, and the
pipeline never requests the `Mail.Send` permission. That is the point: the instruction
came from a microphone worn in public, and a draft the operator presses send on gets
almost all the value with none of the irreversibility. Passing `"send": true` is
refused by name, not quietly ignored — do not try it.

Write the body as a message a human will read and probably edit. Plain text, no
markup; it is inserted as text. Keep it short and specific, and do not sign it as
though the operator wrote it by hand.

### `outlook.event` — create a calendar event

```json
{"verb": "outlook.event",
 "subject": "Migration review",
 "start": "2026-08-04T14:00",
 "minutes": 60,
 "attendees": ["robbie@example.com"],
 "location": "Teams",
 "body": "Agenda: cutover date, rollback."}
```

`subject` and `start` are required. `start` is ISO-8601: `2026-08-04T14:00` is the
operator's configured local time; add `Z` or an offset to mean an absolute instant.
Give `end` **or** `minutes` — with neither you get the configured default (30
minutes). `attendees` is optional; with none it is a private hold on the calendar,
which is often exactly what was asked for.

**Anything with attendees is visible to them the moment it lands**, and an invite is
an email that cannot be recalled. So prefer a hold with no attendees unless the
request clearly asked to invite someone.

**Work out the actual date.** "Tuesday at 2" is not a date. Use the recording
timestamp in your prompt as today, resolve the weekday to a real calendar date, and
say in your report which date you resolved it to so a wrong guess is visible. If you
genuinely cannot tell — "next Tuesday" recorded on a Tuesday — pick the reading you
state in the report rather than stalling, and say what the alternative was.

## Addresses: an email address, or nothing

The pipeline refuses any recipient it cannot verify, so **put a literal email address
in the request**. A bare name is refused with an error, not resolved to whoever seems
closest — resolving "Robbie" is issue #43 and does not exist yet.

If the transcript gives you no address, do not invent one and do not guess at a
company's format. Write the draft into the report as text for the operator to copy,
and say the address was missing. That is a useful result; a draft addressed to the
wrong person is not.

`Name <address@example.com>` is fine — the pipeline takes the address out of it.

## Expect the first attempt to be refused, and write your report that way

Two things routinely stop these verbs, and neither is a failure you can fix:

1. **Consent.** The Microsoft token is currently read-only. Drafting needs
   `Mail.ReadWrite` and an event needs `Calendars.ReadWrite`, and until the operator
   adds those permissions and re-consents, every request is refused with a receipt
   naming the exact scope. That is the expected first-run state.
2. **The gate.** Both verbs are risk class `tracked`, which defaults to needing
   confirmation. In an unattended pass nobody is there to confirm, so the intent is
   recorded and held.

Either way the request is committed and visible to the operator. **Write your report
as though the action is pending — never as though the mail is drafted or the meeting
is booked.** Include the full text of what you asked for, so the operator can act on
it directly if the outbox held it.

## Which account

The operator has two Microsoft 365 accounts with different licensing, and the
pipeline picks between them by configuration (`ATTICUS_OUTLOOK_ACCOUNT`). You do not
choose, and an `account` field in your request does nothing. If the request names a
specific account, say in your report which one you assumed was used and that the
choice is the operator's setting.

## Output

Write the outbox file(s) **and** a self-contained HTML file in the output directory,
named for the task — `draft-migration-timing.html`, not `report.html`. The report is
what the operator reads to find out what happened, so it should contain:

- What was asked for, in one or two sentences.
- The exact draft or event you requested — recipients, subject, body, date and time
  spelled out in words as well as ISO form.
- What is pending, and what would make it complete (a confirmation, or consent).
- Anything you could not do — especially any part of the request that needed reading
  mail, a schedule or a contact — stated plainly.

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
