"""`todo.add` — put a task on the operator's Microsoft To Do list. Issue #51.

## Why Microsoft To Do, and not a new app

Issue #51 opened as "pick a todo app" and answered itself: **one already exists.**
`m365 tasks` has worked on both configured accounts since before this handler was
written, which means the backend is Microsoft To Do over the Graph `/me/todo`
endpoint — a real task store with a first-class phone client, sync, and a token
already sitting on this host. Adopting anything else would mean re-solving auth,
sync and a UI to gain nothing.

The fallback the issue records is a plain Markdown checklist in the vault, rendered
by the site build. It is genuinely cheaper — no credential at all, and it inherits
git history and the browser for free — and it was still rejected, for one reason
that is decisive: **a list you cannot see on your phone is worse than paper.** The
whole point of speaking "add picking up the prescription to my list" into a pin is
that the item is waiting on the phone in your pocket at the pharmacy. A vault file
is also a list that only ever contains what Atticus put in it, whereas To Do
already holds the items added from Outlook and the phone. Keep the Markdown option
in mind only if Graph write consent turns into a fight.

## Why Graph directly rather than shelling out to `m365`

`m365` is on the pipeline host's PATH and this handler runs pipeline-side, so
shelling out was available. It was rejected on three counts:

1. **The CLI is read-only by design and by advertisement.** Its own skill
   description says it can "never send, reply, delete, or modify", and several
   sessions rely on that sentence being true. Teaching it to write would silently
   invalidate a security claim made in a place this repo does not own.
2. **The subcommand does not exist.** There is no `m365 tasks add`, and `m365
   tasks` has no `--json`, so even resolving a list id would mean scraping its
   printed output. Shelling out would mean first editing
   `~/.local/share/m365/m365.py` — a file outside this repository, unversioned
   with it, and shared with every other skill on the host.
3. **The write belongs where the gate is.** Everything that makes this action safe
   — the risk class, the confirm policy, the per-pass cap, the receipt, the tests —
   lives in `outbox.py`. A `subprocess` boundary would put the actual mutation on
   the far side of all of it.

So we mint our own access token from the refresh token the m365 CLI already
stores, and call Graph with `requests`. We read that file and never write it: two
processes racing to rotate one refresh token is how an account locks itself out,
and Azure keeps the existing refresh token valid after redemption, so there is
nothing to save. `ATTICUS_TODO_TOKEN_FILE` exists for an operator who would rather
give the write path its own consent than widen the read-only tool's token.

## This cannot work until someone re-consents

The stored token was issued for read-only scopes (`Tasks.Read` among them). A
refresh grant asking for `Tasks.ReadWrite` is refused by Azure until the operator
approves the wider scope interactively. That is the expected state on first
deploy, not a bug, so every path here fails with an `OutboxError` that names the
scope and the command — the intent is still recorded in the outbox and in git, and
the operator can grant consent and speak the request again.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from outbox import INTERNAL, OutboxError, handler

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# Only what this handler needs. Deliberately NOT the m365 CLI's full read scope
# list: a token minted here should not be able to read mail.
SCOPE = "offline_access Tasks.ReadWrite"

# Graph truncates silently past this; better to say we did it.
TITLE_MAX = 255

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# One sentence the operator can act on, used by every authorisation failure. The
# whole point of the message is that a stack trace is not a diagnosis.
NEEDS_CONSENT = (
    "Microsoft To Do writes are not authorised yet: the stored token carries "
    "read-only scopes. Add Tasks.ReadWrite to the m365 app's scopes and re-consent "
    "interactively (`m365-auth`, then approve in the browser). Nothing was created."
)


def _s(req: dict, field: str) -> str:
    return str(req.get(field) or "").strip()


def _describe(req: dict) -> str:
    title = _s(req, "title") or "(untitled)"
    where = _s(req, "list") or "the default list"
    due = _s(req, "due")
    return f"add “{title}” to {where}" + (f", due {due}" if due else "")


def _token_file(cfg) -> Path:
    """The m365 CLI's token store, resolved the same way the CLI resolves it.

    Two settings rather than one because they answer different questions: which of
    the operator's accounts (`todo_account`, the vocabulary they already use with
    `m365 --account`), versus an outright path for a separately-consented write
    credential (`todo_token_file`, which wins when set).
    """
    explicit = str(getattr(cfg, "todo_token_file", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    account = str(getattr(cfg, "todo_account", "default") or "default").strip()
    safe = re.sub(r"[^A-Za-z0-9_-]", "", account)
    name = "m365.json" if not safe or safe == "default" else f"m365-{safe}.json"
    return Path.home() / ".secrets" / name


def _timeout(cfg) -> int:
    try:
        return max(1, int(getattr(cfg, "todo_timeout", 20) or 20))
    except (TypeError, ValueError):
        return 20


def _stored(cfg) -> dict:
    path = _token_file(cfg)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise OutboxError(
            f"no Microsoft 365 token at {path} — the operator must sign in with "
            f"`m365-auth` first. {NEEDS_CONSENT}")
    except (OSError, ValueError) as e:
        raise OutboxError(f"could not read {path}: {type(e).__name__}: {e}")
    if not isinstance(data, dict):
        raise OutboxError(f"{path} is not a JSON object")
    missing = [k for k in ("client_id", "tenant_id", "refresh_token")
               if not str(data.get(k) or "").strip()]
    if missing:
        raise OutboxError(
            f"{path} has no {', '.join(missing)} — the operator must run "
            f"`m365-auth`. {NEEDS_CONSENT}")
    return data


def _access_token(data: dict, cfg) -> str:
    """A Tasks.ReadWrite access token, or an OutboxError naming the scope.

    We ignore any cached `access_token` in the file: it was minted for the CLI's
    read-only scopes, so presenting it here would fail at the POST with a 403
    instead of failing now with a message the operator can act on.
    """
    try:
        r = requests.post(
            AUTHORITY.format(tenant=data["tenant_id"]),
            data={"grant_type": "refresh_token",
                  "client_id": data["client_id"],
                  "refresh_token": data["refresh_token"],
                  "scope": SCOPE},
            timeout=_timeout(cfg))
    except requests.RequestException as e:
        raise OutboxError(f"could not reach Microsoft to get a token: {type(e).__name__}: {e}")
    try:
        tok = r.json()
    except ValueError:
        raise OutboxError(f"token endpoint returned HTTP {r.status_code} and no JSON")
    if not isinstance(tok, dict):
        tok = {}

    if tok.get("error") or not tok.get("access_token"):
        detail = str(tok.get("error_description") or tok.get("error") or
                     f"HTTP {r.status_code}")[:300]
        raise OutboxError(f"{NEEDS_CONSENT} Microsoft said: {detail}")
    # Azure can hand back a token narrower than the one asked for, which would
    # otherwise surface as a puzzling 403 several calls later.
    granted = str(tok.get("scope") or "")
    if granted and "tasks.readwrite" not in granted.lower():
        raise OutboxError(f"{NEEDS_CONSENT} The token granted only: {granted[:200]}")
    return str(tok["access_token"])


def _graph(method: str, path: str, tok: str, cfg, **kw):
    """One Graph call, with every failure turned into a sentence.

    401/403 is the authorisation story again — consent can be revoked between
    passes — so it gets the same actionable message rather than a bare status code.
    """
    try:
        r = requests.request(method, GRAPH + path,
                             headers={"Authorization": f"Bearer {tok}"},
                             timeout=_timeout(cfg), **kw)
    except requests.RequestException as e:
        raise OutboxError(f"could not reach Microsoft Graph: {type(e).__name__}: {e}")
    if r.status_code in (401, 403):
        raise OutboxError(f"{NEEDS_CONSENT} Graph refused {method} {path} with "
                          f"HTTP {r.status_code}.")
    if r.status_code >= 400:
        body = (r.text or "")[:300]
        raise OutboxError(f"Graph {r.status_code} on {method} {path}: {body}")
    try:
        out = r.json()
    except ValueError:
        raise OutboxError(f"Graph returned no JSON for {method} {path}")
    return out if isinstance(out, dict) else {}


def _resolve_list(req: dict, cfg, tok: str) -> dict:
    """Which list. Never creates one.

    A misheard list name must not silently spawn "Groseries" beside "Groceries" —
    the operator would never find the task, and a wrong list is exactly the kind of
    quiet failure this project treats as the worst kind. So an unknown name is
    refused with the real names listed, which is a diagnosis.
    """
    want = _s(req, "list") or str(getattr(cfg, "todo_list", "") or "").strip()
    lists = _graph("GET", "/me/todo/lists?$top=50", tok, cfg).get("value") or []
    lists = [x for x in lists if isinstance(x, dict)]
    if not lists:
        raise OutboxError("this Microsoft 365 account has no To Do lists")
    if want:
        for x in lists:
            if str(x.get("displayName") or "").strip().lower() == want.lower():
                return x
        names = ", ".join(sorted(str(x.get("displayName") or "?") for x in lists))
        raise OutboxError(f"no To Do list named {want!r}; this account has: {names}")
    for x in lists:
        # Graph spells it lowercase-k; tolerate the other casing rather than
        # silently falling through to lists[0] if that ever changes.
        if (x.get("wellknownListName") or x.get("wellKnownListName")) == "defaultList":
            return x
    return lists[0]


def _due(req: dict, tz_name: str) -> dict | None:
    """A calendar date, or nothing. Never a guess.

    The handler accepts only `YYYY-MM-DD`. Resolving "by Friday" needs the day the
    words were spoken, which the agent has and the pipeline does not, so that job
    belongs upstream — and a phrase reaching here means the agent could not resolve
    it, in which case no due date is the honest answer.

    The time is **noon local, sent as UTC**. To Do treats a due date as date-only,
    but Graph insists on a datetime, and the two plausible client behaviours —
    render the date part as stored, or convert to the viewer's local time — disagree
    by a day if you pick midnight. Noon is correct under both.
    """
    raw = _s(req, "due")
    if not raw:
        return None
    if not _DATE.match(raw):
        raise OutboxError(
            f"due must be a calendar date as YYYY-MM-DD, got {raw!r} — resolve a "
            f"spoken phrase like 'by Friday' to a date before writing the request, "
            f"or leave due out and put the wording in the note")
    try:
        day = datetime.strptime(raw, "%Y-%m-%d")           # naive by design
    except ValueError:
        raise OutboxError(f"due {raw!r} is not a real date")
    local_noon = day + timedelta(hours=12)
    try:
        utc = local_noon.replace(tzinfo=ZoneInfo(tz_name)).astimezone(ZoneInfo("UTC"))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        utc = local_noon                                   # already treated as UTC
    return {"dateTime": utc.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"}


@handler("todo.add", risk=INTERNAL, schema=("title",), describe=_describe)
def add(req: dict, cfg, log=print) -> dict:
    """Create one task in Microsoft To Do.

    INTERNAL, so it runs unattended by default. That is the whole reason this verb
    went first: only the operator ever sees the result, undoing it is one tap in an
    app they already have, and holding it for confirmation would make the feature
    useless — you would read "a task is pending" in a report instead of finding the
    task on your phone. It is the mildest write on the roadmap and therefore the
    right place to prove the #42 gate end to end.
    """
    title = _s(req, "title")[:TITLE_MAX]
    stored = _stored(cfg)
    tok = _access_token(stored, cfg)
    lst = _resolve_list(req, cfg, tok)
    list_id, list_name = str(lst.get("id") or ""), str(lst.get("displayName") or "?")
    if not list_id:
        raise OutboxError(f"To Do list {list_name!r} has no id")

    tz = str(stored.get("timezone") or "UTC")
    body: dict = {"title": title}
    note = _s(req, "note")
    if note:
        body["body"] = {"content": note, "contentType": "text"}
    due = _due(req, tz)
    if due:
        body["dueDateTime"] = due

    created = _graph("POST", f"/me/todo/lists/{list_id}/tasks", tok, cfg, json=body)
    log(f"    todo: added to {list_name}")
    return {"id": str(created.get("id") or ""), "list": list_name,
            "title": title, "due": _s(req, "due") or None}
