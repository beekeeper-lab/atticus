"""Azure DevOps work items — the first credentialed write (issue #49, via #42).

"Atticus, file a ticket on the DDI project that does X." The agent cannot do it:
it holds no credentials and has no network path to `dev.azure.com`. It writes
`output/outbox/001-ado.workitem.json` and this module performs it afterwards.

## Why this one goes first

A work item is **visible but reversible** — it can be edited, reassigned or
closed, and it lands in a system whose entire purpose is tracking things people
file. A wrong ticket is embarrassing, not damaging. So it exercises the whole #42
gate (intent file → validate → risk class → receipt) on something recoverable,
which is exactly what you want from the first handler that touches a credential.
Hence `TRACKED`, not `OUTWARD`.

## Why the project, area path and iteration are NOT taken from the request

The instruction originates in ambient audio. "File a ticket on the DDI project"
gives the agent one noun and no basis whatsoever for guessing an area path or an
iteration, and a misheard project name would file into some *other* team's
backlog — visible to people who did not ask for it, which is the one failure mode
that turns "embarrassing" into "damaging". So the target is configuration:
`ATTICUS_ADO_ORG`, `ATTICUS_ADO_PROJECT`, `ATTICUS_ADO_AREA_PATH`,
`ATTICUS_ADO_ITERATION_PATH`. The agent supplies only the things a sentence
actually contains — a title, a description, optionally a type.

The PAT is scoped to **work-item read/write on that one project**, not
organisation-wide, for the same reason: the blast radius of the credential should
match the blast radius of the feature.

## The work-item type

"File a ticket" is genuinely ambiguous between Bug, Task, User Story and Issue,
and the honest response is to pick a default, say which one was used, and let the
operator configure it — not to infer severity or customer value from a sentence
that stated neither. The default is **Task**; see `DEFAULT_TYPE` for why. The
result dict (and therefore the receipt, and therefore the HTML record) always
names the type that was actually used and whether it came from the request or
from configuration.
"""
import html
import re
from urllib.parse import quote

import requests
from outbox import TRACKED, OutboxError, handler

# 7.1 is the current GA REST version. Pinned deliberately: the *preview* comment
# endpoints move between `-preview.3` and `-preview.4`, which is why comments here
# go through the GA `System.History` field instead (see `add_comment`).
API_VERSION = "7.1"

DEFAULT_BASE_URL = "https://dev.azure.com"

# Task, and the reason is availability rather than taste: Task is the only
# work-item type present in *every* default ADO process (Basic, Agile, Scrum,
# CMMI). Bug is absent from Basic, User Story exists only in Agile, Issue only in
# Agile and Basic, Product Backlog Item only in Scrum. A default that can 404 on
# a project we were pointed at is a bad default.
#
# It is also the least presumptuous of the four. Bug asserts something is broken
# and drags severity/triage obligations with it; User Story claims deliverable
# customer value and enters a backlog to be estimated. "Something to do" is what
# a spoken request actually established.
DEFAULT_TYPE = "Task"

# The types the agent is permitted to ask for. An allowlist rather than a
# pass-through: an invented type name would fail at the API with a confusing 404,
# and it keeps a misheard word from creating something with an unexpected workflow.
DEFAULT_ALLOWED_TYPES = ("Task", "Bug", "Issue", "User Story",
                         "Product Backlog Item", "Feature", "Epic")

# Appended to every description. Anyone finding this item later should be able to
# tell it was filed by a voice agent without asking, and the tag makes a batch of
# them findable (and bulk-closable) if a mishearing ever files something wrong.
PROVENANCE = "<p><em>Filed by Atticus from a spoken request.</em></p>"

TITLE_MAX = 255                                 # System.Title's own limit


def _get(cfg, name: str, default=""):
    """Read an `ado_*` setting, tolerating both a plain attribute and a lazy
    credential property that raises when nothing is configured.

    `getattr(cfg, "ado_pat", "")` is not sufficient on its own: config.py reads
    secrets through properties that raise `RuntimeError` when the credential file
    has no entry, and that exception would escape as a stack trace instead of the
    named, actionable refusal this module promises.
    """
    try:
        v = getattr(cfg, name, default)
    except Exception:                           # noqa: BLE001 — see docstring
        return default
    if v is None:
        return default
    return v


def _required(cfg, name: str, env: str) -> str:
    v = str(_get(cfg, name) or "").strip()
    if not v:
        raise OutboxError(f"{env} is not configured")
    return v


def _target(cfg) -> tuple[str, str, str]:
    """(base_url, organisation, project) — all from config, never from the request."""
    base = str(_get(cfg, "ado_base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
    return base, _required(cfg, "ado_org", "ATTICUS_ADO_ORG"), \
        _required(cfg, "ado_project", "ATTICUS_ADO_PROJECT")


def _pat(cfg) -> str:
    return _required(cfg, "ado_pat", "ATTICUS_ADO_PAT")


def _timeout(cfg) -> int:
    try:
        return max(1, int(_get(cfg, "ado_timeout", 30) or 30))
    except (TypeError, ValueError):
        return 30


def _work_item_type(req: dict, cfg) -> tuple[str, str, str]:
    """(type, where it came from, note).

    An unrecognised request type is not an error — it falls back to the configured
    default and says so, because refusing the whole ticket over a mishearing of one
    word loses the thing the person asked for.
    """
    default = str(_get(cfg, "ado_workitem_type", DEFAULT_TYPE) or DEFAULT_TYPE).strip()
    allowed = _get(cfg, "ado_workitem_types", DEFAULT_ALLOWED_TYPES) or DEFAULT_ALLOWED_TYPES
    if isinstance(allowed, str):
        allowed = [t.strip() for t in allowed.split(",") if t.strip()]
    asked = str(req.get("type") or req.get("work_item_type") or "").strip()
    if not asked:
        return default, "config default", ""
    for a in allowed:
        if a.lower() == asked.lower():
            return a, "request", ""
    return (default, "config default",
            f"requested type {asked!r} is not in ATTICUS_ADO_WORKITEM_TYPES; "
            f"used {default} instead")


def _html_body(text: str) -> str:
    """Plain text → the HTML that ADO's rich-text fields expect.

    Escaped, always. The text derives from ambient audio and renders in other
    people's browsers, and an agent that emitted markup — deliberately or because
    it wrote markdown out of habit — would otherwise silently change what the
    field displays. Blank lines become paragraphs, single newlines become breaks.
    """
    paras = [p for p in (p.strip() for p in re.split(r"\n\s*\n", text.strip())) if p]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)


def _add(path: str, value) -> dict:
    return {"op": "add", "path": path, "value": value}


def _send(method, url: str, ops: list[dict], cfg, *, what: str) -> dict:
    """One JSON-Patch call. Every failure comes back as a named OutboxError.

    Bodies are truncated and never echoed wholesale: this string reaches the
    receipt, the receipt is committed to the vault, and git is forever.
    """
    try:
        resp = method(
            url,
            json=ops,
            headers={"Content-Type": "application/json-patch+json",
                     "Accept": "application/json"},
            # PAT over basic auth with an empty username is the documented scheme.
            auth=("", _pat(cfg)),
            timeout=_timeout(cfg),
        )
    except requests.Timeout:
        raise OutboxError(f"ADO {what} timed out after {_timeout(cfg)}s")
    except requests.RequestException as e:
        raise OutboxError(f"ADO {what} network error: {type(e).__name__}")

    if resp.status_code in (401, 403):
        raise OutboxError(
            f"ADO rejected the credential ({resp.status_code}) — check ATTICUS_ADO_PAT "
            f"is current and scoped to work-item read/write on this project")
    if resp.status_code == 404:
        raise OutboxError(
            f"ADO {what} returned 404 — check ATTICUS_ADO_ORG, ATTICUS_ADO_PROJECT "
            f"and the work-item type exist: {resp.text[:120]}")
    if resp.status_code not in (200, 201):
        raise OutboxError(f"ADO {what} returned {resp.status_code}: {resp.text[:160]}")
    try:
        data = resp.json()
    except ValueError:
        raise OutboxError(f"ADO {what} returned a non-JSON body: {resp.text[:120]}")
    if not isinstance(data, dict) or not data.get("id"):
        raise OutboxError(f"ADO {what} returned no work-item id: {str(data)[:120]}")
    return data


def _browser_url(data: dict, base: str, org: str, project: str, item_id) -> str:
    """The link a human can click.

    `data["url"]` is the REST resource, which is useless in a report. ADO returns
    the web URL under `_links.html.href`; construct it if it did not.
    """
    link = ((data.get("_links") or {}).get("html") or {}).get("href")
    if link:
        return str(link)
    return f"{base}/{quote(org)}/{quote(project)}/_workitems/edit/{item_id}"


@handler("ado.workitem", risk=TRACKED, schema=("title",),
         describe=lambda r: f"file an ADO work item: {str(r.get('title') or '')[:120]!r}")
def create_workitem(req: dict, cfg, log=print) -> dict:
    """Create a work item. Returns id + clickable URL so the record can link to it."""
    base, org, project = _target(cfg)
    wtype, type_source, type_note = _work_item_type(req, cfg)

    title = " ".join(str(req.get("title") or "").split())
    truncated = len(title) > TITLE_MAX
    if truncated:
        title = title[:TITLE_MAX - 1] + "…"

    description = str(req.get("description") or req.get("body") or "").strip()
    if truncated:
        # Nothing spoken should be lost just because the title ran long.
        description = f"Full title: {req.get('title')}\n\n{description}".strip()

    ops = [_add("/fields/System.Title", title),
           _add("/fields/System.Description", _html_body(description) + PROVENANCE)]

    # Area and iteration are omitted when unset, which lets ADO apply the
    # project's own defaults rather than us inventing a path that may not exist.
    area = str(_get(cfg, "ado_area_path") or "").strip()
    iteration = str(_get(cfg, "ado_iteration_path") or "").strip()
    assignee = str(_get(cfg, "ado_assigned_to") or "").strip()
    tags = _get(cfg, "ado_tags", ("atticus",)) or ()
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    if area:
        ops.append(_add("/fields/System.AreaPath", area))
    if iteration:
        ops.append(_add("/fields/System.IterationPath", iteration))
    if assignee:
        ops.append(_add("/fields/System.AssignedTo", assignee))
    if tags:
        ops.append(_add("/fields/System.Tags", "; ".join(tags)))

    url = f"{base}/{quote(org)}/{quote(project)}/_apis/wit/workitems/${quote(wtype)}" \
          f"?api-version={API_VERSION}"
    data = _send(requests.post, url, ops, cfg, what=f"create {wtype}")

    item_id = data["id"]
    web = _browser_url(data, base, org, project, item_id)
    log(f"      ADO {wtype} #{item_id} in {project} — {web}")
    if type_note:
        log(f"      ! {type_note}")
    result = {
        "id": item_id,
        "url": web,
        "work_item_type": wtype,          # the record must say WHICH type it filed
        "type_source": type_source,
        "project": project,
        "organisation": org,
        "title": title,
        "area_path": area or "(project default)",
        "iteration_path": iteration or "(project default)",
        "tags": list(tags),
    }
    if type_note:
        result["type_note"] = type_note
    return result


@handler("ado.comment", risk=TRACKED, schema=("id", "body"),
         describe=lambda r: f"comment on ADO work item #{r.get('id')}")
def add_comment(req: dict, cfg, log=print) -> dict:
    """Append a comment to an existing work item.

    Written through the GA `System.History` field rather than the `/comments`
    endpoint: History is what the Discussion tab renders, and it is stable API,
    while the comments endpoint is still version-suffixed `-preview` and has moved.
    """
    base, org, project = _target(cfg)
    item_id = str(req.get("id") or "").strip()
    if not item_id.isdigit():
        raise OutboxError(f"ado.comment needs a numeric work-item id, got {item_id!r}")
    body = str(req.get("body") or req.get("comment") or "").strip()

    url = f"{base}/{quote(org)}/{quote(project)}/_apis/wit/workitems/{item_id}" \
          f"?api-version={API_VERSION}"
    ops = [_add("/fields/System.History", _html_body(body) + PROVENANCE)]
    data = _send(requests.patch, url, ops, cfg, what=f"comment on #{item_id}")

    web = _browser_url(data, base, org, project, data["id"])
    log(f"      ADO comment on #{data['id']} — {web}")
    return {"id": data["id"], "url": web, "project": project, "organisation": org,
            "comment_chars": len(body)}
