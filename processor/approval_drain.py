"""Collect approval decisions from the phone, and perform what was approved.

Issue #83. The queue itself is `approvals.py`; this is the part that closes the
loop. It runs inside the processor's existing pass — **no new timer** — because
an approval is not time-critical to the minute and a fifth unit is a fifth thing
that can silently stop.

## The reply path

The push that announces a held action carries ntfy action buttons, and those
buttons **publish to a second ntfy topic** rather than calling back into this
host. That is the whole security design (see `approvals.py`): approving must not
be reachable from the sandbox, and opening an inbound HTTP endpoint on the
machine that runs an autonomous agent is precisely what this project has refused
to do everywhere else.

So the flow is:

    held action → push with Approve/Deny buttons
                → operator taps → ntfy publishes to the decision topic
                → this module polls that topic → performs it → receipt + push

Polling uses ntfy's own `?poll=1&since=` JSON endpoint, so there is no
subscription to keep alive across passes and nothing to reconnect after a
reboot. `since` is a local watermark; re-reading a message twice is harmless
because `decide()` refuses anything already decided.

## Why the handler is called the same way an `auto` action calls it

`perform()` runs `outbox.validate()` and then the handler function — the exact
path an unattended action takes. One implementation of every verb, one set of
refusals, one receipt shape. An approval must not become a second, laxer way to
reach a credential.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import approvals
import notify as nf
import outbox

# The watermark lives beside the alarm stamps rather than in the vault: it is
# local operational state, and losing it on a reboot costs one duplicate poll
# window, which `decide()` already refuses.
WATERMARK = "approval-since"

# A decision message is tiny. Anything larger is not one of ours.
MAX_BODY = 4096


def _watermark_path() -> Path:
    return nf.STATE / WATERMARK


def _read_watermark() -> str:
    try:
        v = _watermark_path().read_text().strip()
    except OSError:
        return ""
    return v if v.isdigit() else ""


def _write_watermark(value: str):
    try:
        nf.STATE.mkdir(parents=True, exist_ok=True)
        _watermark_path().write_text(str(value))
    except OSError:
        pass


def poll_url(topic_url: str, since: str) -> str:
    base = topic_url.rstrip("/")
    # ntfy serves the JSON feed at <topic>/json. Accept a URL given either way.
    if not base.endswith("/json"):
        base += "/json"
    q = urllib.parse.urlencode({"poll": "1", "since": since or "12h"})
    return f"{base}?{q}"


def fetch_decisions(cfg, *, log=print) -> list[dict]:
    """Every decision message on the topic since the watermark.

    Never raises: the approval channel being unreachable must not fail a
    processor pass that has real work in it.
    """
    url = str(getattr(cfg, "approval_topic_url", "") or "").strip()
    if not url:
        return []
    if not url.lower().startswith(("http://", "https://")):
        log(f"    ! approval topic is not http(s): {url[:32]!r}")
        return []
    since = _read_watermark()
    out, newest = [], since
    try:
        req = urllib.request.Request(poll_url(url, since),  # noqa: S310 — checked
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            raw = resp.read(256 * 1024).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"    ! could not poll the approval topic: {type(e).__name__}")
        return []

    for line in raw.splitlines():                 # ntfy streams one JSON per line
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict) or msg.get("event") != "message":
            continue
        stamp = str(msg.get("time") or "")
        if stamp.isdigit() and (not newest or int(stamp) > int(newest or 0)):
            newest = stamp
        body = str(msg.get("message") or "")[:MAX_BODY]
        try:
            payload = json.loads(body)
        except ValueError:
            continue                             # somebody typing on the topic
        if isinstance(payload, dict) and payload.get("id"):
            out.append(payload)
    if newest:
        # +1 so the same message is not re-read every pass. Re-reading is safe
        # but noisy, and a decided item logs a refusal each time.
        _write_watermark(str(int(newest) + 1) if newest.isdigit() else newest)
    return out


def perform(cfg, item: dict, *, log=print) -> dict:
    """Run one approved action through the ordinary handler path."""
    req = dict(item.get("request") or {})
    req["_file"] = f"approval:{item['id']}"
    req["_stem"] = item.get("stem") or ""
    try:
        h = outbox.validate(req)
    except outbox.OutboxError as e:
        approvals.append(cfg.vault, item["id"], approvals.FAILED, reason=str(e))
        log(f"    ✗ approved action refused: {e}")
        return {"ok": False, "reason": str(e)}
    try:
        result = h["fn"](req, cfg, log=log) or {}
    except outbox.OutboxError as e:
        approvals.append(cfg.vault, item["id"], approvals.FAILED, reason=str(e))
        log(f"    ✗ approved action failed: {e}")
        return {"ok": False, "reason": str(e)}
    except Exception as e:                                       # noqa: BLE001
        approvals.append(cfg.vault, item["id"], approvals.FAILED,
                         reason=f"{type(e).__name__}: {e}")
        log(f"    ✗ approved action failed: {type(e).__name__}: {e}")
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    approvals.append(cfg.vault, item["id"], approvals.PERFORMED, result=result)
    log(f"    ✓ performed: {item.get('summary')}")
    return {"ok": True, "result": result}


def announce(cfg, item: dict, *, log=print) -> bool:
    """The push that asks. ALERT severity: it needs a decision, but nothing is
    being lost while it waits — and it must not book a calendar event."""
    topic = str(getattr(cfg, "approval_topic_url", "") or "").strip()
    body = (f"{item.get('summary')}\n\n"
            f"Risk: {item.get('risk')}. Expires {item.get('expires_at')}.\n"
            f"Reply on the approval topic, or run:\n"
            f"  atticus approvals --approve {item['id']}")
    actions = ""
    if topic:
        payload_ok = json.dumps({"id": item["id"], "decision": "approve",
                                 "nonce": item.get("nonce", "")})
        payload_no = json.dumps({"id": item["id"], "decision": "deny",
                                 "nonce": item.get("nonce", "")})
        # ntfy's Actions header: comma-separated fields, semicolon-separated
        # actions. The body is quoted because it is JSON with commas in it.
        actions = (f"http, Approve, {topic}, method=POST, body='{payload_ok}'; "
                   f"http, Deny, {topic}, method=POST, body='{payload_no}'")
    return nf.notify_with_actions(
        cfg, body, title="Atticus — approval needed", tags="lock",
        priority="high", actions=actions, log=log)


def run(cfg, *, log=print) -> dict:
    """One pass: expire, collect decisions, perform. Never raises."""
    if not getattr(cfg, "vault", None):
        return {"decided": 0, "performed": 0, "expired": 0}
    summary = {"decided": 0, "performed": 0, "expired": 0, "failed": 0}

    stale = approvals.expire_stale(cfg.vault)
    if stale:
        summary["expired"] = len(stale)
        lines = "\n".join(f"· {s.get('summary')}" for s in stale[:5])
        nf.alarm(cfg, f"{len(stale)} held action(s) expired undecided:\n\n{lines}",
                 severity=nf.ALERT, title="Atticus — approvals expired", log=log)

    for msg in fetch_decisions(cfg, log=log):
        try:
            approvals.decide(cfg.vault, str(msg.get("id")),
                             str(msg.get("decision") or ""),
                             nonce=str(msg.get("nonce") or ""), by="push")
            summary["decided"] += 1
        except approvals.ApprovalError as e:
            log(f"    ! approval decision refused: {e}")

    for item in approvals.approved_ready(cfg.vault):
        res = perform(cfg, item, log=log)
        summary["performed" if res["ok"] else "failed"] += 1
        nf.alarm(cfg,
                 ("Performed: " if res["ok"] else "Could not perform: ")
                 + str(item.get("summary"))
                 + ("" if res["ok"] else f"\n\n{res['reason']}"),
                 severity=nf.ROUTINE if res["ok"] else nf.ALERT,
                 title="Atticus — approved action", log=log)
    return summary
