"""outlook.draft and outlook.event — the WRITE half of Microsoft 365. Issues #44, #45, #46.

Mail, calendar and contacts are one skill and one handler module because they are one
credential and one CLI: `m365` on PATH, delegated OAuth against a single app
registration, one rotating refresh token per account in `~/.secrets/m365*.json`.
Splitting them into three handlers would triple the token plumbing and change nothing
about the blast radius.

## What this module can and cannot do, and why the gap is not a bug

`m365` already reads mail, calendar, contacts and people today — and it is
deliberately read-only. **None of that reading can be delivered through here.**
`outbox.py` says why in full: a read needs data *during* the agent's run, and the
outbox performs actions only *after* the agent exits. The two candidate fixes — a
credential-holding loopback broker the agent can query, or pipeline-side pre-fetch —
are a separate decision with a worse risk profile (a broker is a large new
prompt-injection surface; pre-fetch cannot answer an arbitrary question).

So the read half of #44/#45/#46 is **unbuilt, not merely unwired**, and the SKILL.md
says so to the agent in as many words. A skill that implied "I can summarise your
mail" would produce a confident report about mail nobody read.

What is left is the half an outbox can actually do: create a draft, create an event.

## Draft, not send

`outlook.draft` creates a message in Drafts and stops. It never calls `/send` and
nothing here ever requests `Mail.Send`. Issue #44 asks for exactly this split, and the
reason is that the instruction originates in a microphone worn in public: a draft
sitting in Outlook for a human to press send on gets most of the value with none of
the irreversibility. A request with `"send": true` is refused by name rather than
quietly downgraded, so the receipt records the disagreement.

## Risk classes

Both verbs are `TRACKED`, whose default gate is `confirm`.

`outlook.event` is TRACKED by the letter of the taxonomy — an invite is visible to
every attendee the moment it lands, and recoverable but not silent.

`outlook.draft` is TRACKED by choice, one class above where the taxonomy's wording
would put it (a draft is visible only to the operator, which reads like `internal`).
It is classed up because a draft is a complete message addressed to a real person
sitting one click from delivery, and `internal` defaults to `auto` — ambient speech
must not be able to populate the mailbox with pre-addressed mail unattended.

**Consequence worth knowing:** an operator who sets `ATTICUS_OUTBOX_TRACKED=auto` to
let GitHub issues flow also lets calendar invites to other people flow. The classes
are configured per class, not per verb.

## Scopes: the existing token cannot do any of this

The `m365` token is read-only on purpose (`Mail.Read`, `Calendars.Read`, …). Drafting
needs `Mail.ReadWrite`; creating an event needs `Calendars.ReadWrite`. Neither is
consented today, so **both verbs fail until someone adds the delegated permission to
the app registration and re-consents** with `m365-auth`. That failure is the expected
first-run state, so it is reported as an `OutboxError` naming the exact scope, never a
traceback.

We check the *granted* scopes on the token response rather than trusting the request,
because Entra will happily hand back a token for the scopes it does have and let the
write fail later as a 403 with a less useful message.

## Recipients

Resolution ("Robbie" → an address) is issue #43 and lives in `processor/contacts.py`,
which is being built separately. This module does not resolve anything: it accepts a
literal address, uses the resolver if it happens to be importable, and otherwise
**refuses the recipient by name**. Guessing is the highest-consequence failure on the
roadmap and a draft addressed to the wrong Robbie is one keypress from being sent.
"""
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests
from outbox import TRACKED, OutboxError, handler

try:                          # issue #43 — the resolver, built alongside this
    import contacts           # type: ignore
except ImportError:           # today's normal state; every name is then refused
    contacts = None

# Wire endpoints. Overridable only so a test can point them at a closed port.
_GRAPH = "https://graph.microsoft.com/v1.0"
_LOGIN = "https://login.microsoftonline.com"

# The delegated permissions each verb needs. Named constants because they appear in
# operator-facing error text and must match what someone types into Entra exactly.
DRAFT_SCOPE = "Mail.ReadWrite"
EVENT_SCOPE = "Calendars.ReadWrite"
# Never requested anywhere in this module. It is written down so that a future
# `outlook.send` cannot be added without noticing that this is the line it crosses.
SEND_SCOPE = "Mail.Send"

_EMAIL = re.compile(r"^[^@\s,;<>]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_ANGLE = re.compile(r"<([^<>]+)>\s*$")          # "Robbie Page <robbie@x.com>"
_SUBJECT_MAX = 255                              # Graph's own limit


# ── the credential ─────────────────────────────────────────────────────────────
def _account(cfg) -> str:
    """Which Microsoft 365 account, sanitised the same way `m365` sanitises it.

    THIS IS A SETTING, not a constant, because the two accounts have different
    licensing and neither is right for everything: the Stonewaters default is
    email-only, `organservices` has SharePoint but its calendar is empty. Hard-coding
    either would make one of the two verbs write into the wrong mailbox with no
    error — an event created on an empty calendar looks exactly like success.
    """
    raw = str(getattr(cfg, "outlook_account", "default") or "default").strip()
    return re.sub(r"[^A-Za-z0-9_-]", "", raw)


def _store_path(cfg) -> Path:
    explicit = str(getattr(cfg, "outlook_secrets", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    account = _account(cfg)
    if account.lower() in ("", "default"):
        return Path("~/.secrets/m365.json").expanduser()
    return Path(f"~/.secrets/m365-{account}.json").expanduser()


def _load_store(path: Path) -> dict:
    """Read `m365`'s secret store. Absent is a configuration state, not a crash."""
    try:
        store = json.loads(path.read_text())
    except FileNotFoundError:
        raise OutboxError(
            f"no Microsoft 365 credential at {path} — sign in with "
            f"`m365-auth` first (see ATTICUS_OUTLOOK_ACCOUNT)")
    except (OSError, ValueError) as e:
        raise OutboxError(f"could not read the Microsoft 365 credential at {path}: {e}")
    if not isinstance(store, dict):
        raise OutboxError(f"{path} is not a JSON object")
    missing = [k for k in ("client_id", "tenant_id", "refresh_token")
               if not str(store.get(k) or "").strip()]
    if missing:
        raise OutboxError(
            f"{path} is missing {', '.join(missing)} — run "
            f"`m365-auth` to (re-)sign in")
    return store


def _scope_hint(cfg, scope: str, granted: str) -> str:
    """The message an operator reads on the very first attempt, so it must be a fix.

    Names the scope exactly as Entra spells it, because that string is what gets
    typed into the app registration.
    """
    account = _account(cfg)
    flag = "" if account.lower() in ("", "default") else f" --account {account}"
    return (f"the Microsoft 365 token does not grant {scope}. The m365 token is "
            f"deliberately read-only, so this is expected until someone adds {scope} "
            f"to the app registration's delegated permissions and re-consents with "
            f"`m365-auth{flag}`. Granted today: {granted.strip() or 'nothing'}")


def _access_token(cfg, store: dict, scope: str, *, log=print) -> str:
    """Exchange the refresh token for an access token that really carries `scope`.

    Two ways this fails and both must name the scope: Entra can refuse the exchange
    outright (unconsented permission — AADSTS65001), or it can succeed and hand back
    a token for the narrower set it does have. The second is the dangerous one: it
    would otherwise surface much later as a bare Graph 403.
    """
    timeout = int(getattr(cfg, "outlook_timeout", 30) or 30)
    url = (f"{str(getattr(cfg, 'outlook_login_url', '') or _LOGIN).rstrip('/')}"
           f"/{store['tenant_id']}/oauth2/v2.0/token")
    try:
        resp = requests.post(url, data={
            "grant_type": "refresh_token",
            "client_id": store["client_id"],
            "refresh_token": store["refresh_token"],
            "scope": f"offline_access {scope}",
        }, timeout=timeout)
    except requests.Timeout:
        raise OutboxError(f"Microsoft sign-in timed out after {timeout}s; nothing was written")
    except requests.RequestException as e:
        raise OutboxError(f"Microsoft sign-in network error: {type(e).__name__}")

    try:
        tok = resp.json()
    except ValueError:
        raise OutboxError(f"Microsoft sign-in returned a non-JSON body "
                          f"(HTTP {resp.status_code}): {resp.text[:160]}")

    if tok.get("error"):
        detail = str(tok.get("error_description") or "")[:200]
        if ("AADSTS65001" in detail or "consent" in detail.lower()
                or tok["error"] in ("invalid_scope", "invalid_grant")):
            raise OutboxError(_scope_hint(cfg, scope, "") + f" [{tok['error']}]")
        raise OutboxError(f"Microsoft refused the token refresh ({tok['error']}): {detail}")

    granted = str(tok.get("scope") or "")
    if scope.lower() not in {s.lower() for s in granted.split()}:
        raise OutboxError(_scope_hint(cfg, scope, granted))

    access = str(tok.get("access_token") or "").strip()
    if not access:
        raise OutboxError("Microsoft returned no access_token")
    _persist_refresh(_store_path(cfg), store, tok, log=log)
    return access


def _persist_refresh(path: Path, store: dict, tok: dict, *, log=print) -> None:
    """Write the rotated refresh token back, and nothing else.

    Entra rotates the refresh token on every exchange and retires the old one, so
    dropping the new one here would eventually break `m365`'s reads as a side effect
    of a draft. Only `refresh_token` is written: caching OUR access token in m365's
    store would hand a read-only tool a write-capable token it would then reuse.

    Best-effort — a draft that succeeded must not be reported as failed because a
    file was not writable.
    """
    fresh = str(tok.get("refresh_token") or "").strip()
    if not fresh or fresh == store.get("refresh_token"):
        return
    store["refresh_token"] = fresh
    tmp = path.with_suffix(path.suffix + ".atticus-tmp")
    try:
        tmp.write_text(json.dumps(store, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        log(f"    ! outlook: could not save the rotated refresh token to {path}: {e}")
        tmp.unlink(missing_ok=True)


# ── Graph ──────────────────────────────────────────────────────────────────────
def _graph_post(cfg, path: str, body: dict, token: str, *, what: str) -> dict:
    base = str(getattr(cfg, "outlook_graph_url", "") or _GRAPH).rstrip("/")
    timeout = int(getattr(cfg, "outlook_timeout", 30) or 30)
    try:
        resp = requests.post(f"{base}{path}", json=body, timeout=timeout, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"})
    except requests.Timeout:
        # Ambiguous on purpose: the POST may have landed. Do not imply otherwise.
        raise OutboxError(f"Graph timed out after {timeout}s — the {what} may or "
                          f"may not have been created")
    except requests.RequestException as e:
        raise OutboxError(f"Graph network error: {type(e).__name__}")

    if resp.status_code >= 400:
        code = msg = ""
        try:
            err = (resp.json() or {}).get("error") or {}
            code, msg = str(err.get("code") or ""), str(err.get("message") or "")
        except ValueError:
            msg = resp.text[:160]
        if resp.status_code == 401:
            hint = " — the token was rejected; re-run `m365-auth`"
        elif resp.status_code == 403:
            hint = (f" — this needs the delegated permission for the {what} "
                    f"({DRAFT_SCOPE if what == 'draft' else EVENT_SCOPE})")
        elif resp.status_code == 429:
            hint = " — rate limited by Graph; nothing was created"
        else:
            hint = ""
        raise OutboxError(f"Graph returned HTTP {resp.status_code}"
                          f"{f' {code}' if code else ''}: {msg[:200]}{hint}")
    try:
        return resp.json() or {}
    except ValueError:
        raise OutboxError(f"Graph returned a non-JSON body: {resp.text[:160]}")


# ── recipients (see issue #43) ─────────────────────────────────────────────────
def _resolve(name: str, cfg) -> str:
    """Ask the #43 resolver, or refuse. This module never guesses.

    The resolver's interface is not settled yet, so everything about the call is
    defensive: it may not exist, it may not expose `resolve`, and its signature may
    not be the one in #43. Every one of those is a refusal, not a fallback, because
    the fallback would be to send a private message to whoever seemed closest.
    """
    if contacts is None:
        raise OutboxError(
            f"cannot address {name!r} — recipient resolution (issue #43, "
            f"processor/contacts.py) is not installed, so only a literal email "
            f"address can be used here")
    resolve = getattr(contacts, "resolve", None)
    if not callable(resolve):
        raise OutboxError(f"cannot address {name!r} — processor/contacts.py exposes "
                          f"no resolve()")
    try:
        try:
            found = resolve(name, channel="email")
        except TypeError:
            found = resolve(name)
    except OutboxError:
        raise
    except Exception as e:                                          # noqa: BLE001
        raise OutboxError(f"resolving {name!r} failed: {type(e).__name__}: {e}")

    floor = float(getattr(cfg, "outlook_min_confidence", 0.9) or 0)
    good = []
    for c in (found or []):
        c = c if isinstance(c, dict) else {}
        addr = str(c.get("handle") or c.get("address") or c.get("email") or "").strip()
        if _EMAIL.match(addr) and float(c.get("confidence") or 0) >= floor:
            good.append((addr.lower(), str(c.get("name") or addr)))
    if len(good) != 1:
        # Ambiguity must refuse: nobody is present to disambiguate. #43 is explicit
        # that confidence high enough to draft is not confidence high enough to send.
        raise OutboxError(
            f"cannot address {name!r} — resolution returned "
            f"{len(good)} candidate(s) at confidence >= {floor}"
            + (f" ({', '.join(n for _, n in good)})" if good else "")
            + "; say the email address instead")
    return good[0][0]


def _one_recipient(value: str, cfg) -> str:
    text = str(value or "").strip()
    m = _ANGLE.search(text)
    raw = (m.group(1) if m else text).strip().strip("<>").strip()
    if _EMAIL.match(raw):
        return raw.lower()
    return _resolve(raw, cfg)


def _recipients(value, cfg, *, field: str, required: bool) -> list[str]:
    if isinstance(value, str):
        parts = [p for p in re.split(r"[,;]| and ", value) if p.strip()]
    elif isinstance(value, (list, tuple)):
        parts = [str(p) for p in value if str(p).strip()]
    elif value in (None, ""):
        parts = []
    else:
        raise OutboxError(f"{field} must be a string or a list")

    out: list[str] = []
    for p in parts:
        addr = _one_recipient(p, cfg)
        if addr not in out:
            out.append(addr)
    if required and not out:
        raise OutboxError(f"{field} is empty")
    cap = int(getattr(cfg, "outlook_max_recipients", 5) or 0)
    if cap and len(out) > cap:
        # One misheard sentence must not address a distribution list.
        raise OutboxError(f"{field} has {len(out)} recipients; the cap is {cap} "
                          f"(ATTICUS_OUTLOOK_MAX_RECIPIENTS)")
    return out


def _emails(addrs: list[str]) -> list[dict]:
    return [{"emailAddress": {"address": a}} for a in addrs]


def _subject(req: dict) -> str:
    s = " ".join(str(req.get("subject") or "").split())
    if len(s) > _SUBJECT_MAX:
        raise OutboxError(f"the subject is {len(s)} characters; Graph's limit is "
                          f"{_SUBJECT_MAX}")
    return s


# ── outlook.draft ──────────────────────────────────────────────────────────────
def _describe_draft(req: dict) -> str:
    to = req.get("to")
    who = ", ".join(str(t) for t in to) if isinstance(to, (list, tuple)) else str(to or "?")
    subject = " ".join(str(req.get("subject") or "").split())
    return f"draft an Outlook mail to {who}: {subject[:100]}"


@handler("outlook.draft", risk=TRACKED, schema=("to", "subject", "body"),
         describe=_describe_draft)
def draft(req: dict, cfg, log=print) -> dict:
    """Create a message in Drafts. Never sends it."""
    if str(req.get("send") or "").strip().lower() in ("1", "true", "yes", "on"):
        # Refused by name so the receipt records what was asked for. Sending is a
        # separate verb that does not exist, and Mail.Send is never requested here.
        raise OutboxError(
            "outlook.draft cannot send — it creates a draft for a human to send. "
            f"There is no send verb and no {SEND_SCOPE} scope (issue #44 lands "
            f"draft-only first, deliberately)")

    to = _recipients(req.get("to"), cfg, field="to", required=True)
    cc = _recipients(req.get("cc"), cfg, field="cc", required=False)
    subject = _subject(req)
    body = str(req.get("body") or "")

    store = _load_store(_store_path(cfg))
    token = _access_token(cfg, store, DRAFT_SCOPE, log=log)
    # contentType Text, not HTML: the body derives from a transcript and nothing
    # here should be able to inject markup into the operator's mail client.
    msg = _graph_post(cfg, "/me/messages", {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": _emails(to),
        **({"ccRecipients": _emails(cc)} if cc else {}),
    }, token, what="draft")

    log(f"    outlook: draft created for {', '.join(to)} (not sent)")
    return {"id": msg.get("id"), "web_link": msg.get("webLink"), "to": to,
            "cc": cc, "sent": False, "account": _account(cfg)}


# ── outlook.event ──────────────────────────────────────────────────────────────
def _timezone_name(cfg, store: dict) -> str:
    return (str(getattr(cfg, "outlook_timezone", "") or "").strip()
            or str(store.get("timezone") or "").strip() or "UTC")


def _when(value, *, field: str) -> tuple[datetime, bool]:
    """Parse one ISO-8601 instant. Returns (naive datetime, was_utc).

    An offset or a trailing Z is normalised to UTC and reported as such; a bare local
    time is left alone and gets the configured zone. Mixing those up silently moves a
    meeting by hours, which is the whole reason this is not a one-liner.
    """
    s = " ".join(str(value or "").split()).replace(" ", "T")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise OutboxError(f"{field} is not an ISO-8601 date-time: {value!r} "
                          f"(e.g. 2026-08-04T14:00)")
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None), True
    return dt, False


def _describe_event(req: dict) -> str:
    subject = " ".join(str(req.get("subject") or "").split())
    who = req.get("attendees") or []
    n = len(who) if isinstance(who, (list, tuple)) else 1
    return (f"create an Outlook event {subject[:80]!r} at {req.get('start')}"
            + (f" with {n} attendee(s)" if n else ""))


@handler("outlook.event", risk=TRACKED, schema=("subject", "start"),
         describe=_describe_event)
def event(req: dict, cfg, log=print) -> dict:
    """Create a calendar event. Visible to attendees immediately — hence TRACKED."""
    subject = _subject(req)
    attendees = _recipients(req.get("attendees"), cfg, field="attendees", required=False)

    start, start_utc = _when(req.get("start"), field="start")
    if str(req.get("end") or "").strip():
        end, end_utc = _when(req.get("end"), field="end")
        if end_utc != start_utc:
            raise OutboxError("start and end must both be local or both be UTC")
        if end <= start:
            raise OutboxError(f"end ({req.get('end')}) is not after start "
                              f"({req.get('start')})")
    else:
        minutes = int(req.get("minutes") or getattr(cfg, "outlook_event_minutes", 30) or 30)
        if minutes <= 0:
            raise OutboxError("minutes must be positive")
        end = start + timedelta(minutes=minutes)

    store = _load_store(_store_path(cfg))
    tz = "UTC" if start_utc else _timezone_name(cfg, store)
    token = _access_token(cfg, store, EVENT_SCOPE, log=log)
    body = {
        "subject": subject,
        "body": {"contentType": "Text", "content": str(req.get("body") or "")},
        "start": {"dateTime": start.isoformat(timespec="seconds"), "timeZone": tz},
        "end": {"dateTime": end.isoformat(timespec="seconds"), "timeZone": tz},
    }
    if attendees:
        body["attendees"] = [{"emailAddress": {"address": a}, "type": "required"}
                             for a in attendees]
    if str(req.get("location") or "").strip():
        body["location"] = {"displayName": str(req["location"]).strip()}
    # Optional explicit alert. Graph's default is client-defined (usually 15 min
    # before); the reminders companion event (issue #66) needs the alert AT the
    # start, because the start IS the reminder's moment. Only set when asked, so
    # ordinary spoken events keep whatever the operator's calendar does normally.
    if req.get("alert_minutes_before") not in (None, ""):
        try:
            # Via str, so a float like 3.7 is refused rather than silently
            # truncated to an alert three minutes earlier than written.
            alert = int(str(req["alert_minutes_before"]).strip())
        except (TypeError, ValueError):
            raise OutboxError("alert_minutes_before must be a whole number of minutes")
        if alert < 0:
            raise OutboxError("alert_minutes_before cannot be negative")
        body["isReminderOn"] = True
        body["reminderMinutesBeforeStart"] = alert

    ev = _graph_post(cfg, "/me/events", body, token, what="event")
    log(f"    outlook: event created {start.isoformat(timespec='minutes')} {tz}")
    return {"id": ev.get("id"), "web_link": ev.get("webLink"),
            "start": start.isoformat(timespec="seconds"), "time_zone": tz,
            "attendees": attendees, "account": _account(cfg)}
