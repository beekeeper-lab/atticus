"""Held actions, and the only safe way to say yes to one. Issue #83.

`outbox.gate()` returns `auto | confirm | off`, and until now **`confirm` meant
held forever**: the intent was recorded in the receipt, the report said pending,
and nothing could ever approve it. In an unattended pipeline that made the
middle setting identical to `off` with better paperwork — which is why every
verb the operator enabled is `auto`. The gate's safe middle was unusable, so the
only way to make a capability work was to open it fully. That is an incentive to
over-grant, and per-verb gates existed to prevent exactly that.

## Why approval does not live in the vault browser

That was the obvious build: the write API and the check-off UI already exist
from the todo work, and an approvals view would have been an afternoon. **It
would also have handed the agent the power to approve itself.**

The vault API answers on loopback. The sandbox deliberately keeps the host
network namespace, because research needs egress. The write token is embedded in
every published page (#69). So an agent acting on injected instructions can
fetch a page, scrape the token, and POST — and if approving lived there, the
control would be reachable by the thing it exists to control. A held action
would be one prompt injection away from performed.

## So approval arrives the way the operator already is

By notification, on a channel the sandbox cannot reach:

  * the agent's environment is a strict allowlist — `LANG, LC_ALL, TZ, TERM`,
    a synthetic `HOME`/`PATH`, its output dir, and its auth token. Crucially
    `ATTICUS_NOTIFY_URL` is **not** in it, so the agent cannot discover the
    topic it would need to publish to;
  * the reply comes back on a SECOND ntfy topic that the pipeline polls. No
    inbound endpoint is opened on the box that runs an autonomous agent — the
    same reasoning that made this project poll Plaud and poll git rather than
    accept webhooks.

Each pending action carries a **nonce** minted at enqueue. A decision whose
nonce does not match is refused, so a replayed or guessed message cannot
approve anything.

**The honest residual risk**, stated the way this project states them: the
approval topic is a bearer capability. Anyone who knows the URL can approve.
That is the same trust model as the existing alarm topic, it is why the URL is
unguessable and kept out of the repo, and it is why the ledger records who
decided and when — so a decision nobody remembers making is visible afterwards.

## Shape

An append-only JSONL ledger, `.state/approvals.jsonl` — the third use of the
pattern after reminders and todos, copied rather than abstracted because the
three differ in exactly the details an abstraction would hide.

    pending → approved → performed | failed
            → denied
            → expired
"""
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

LEDGER = ".state/approvals.jsonl"

PENDING, APPROVED, DENIED, EXPIRED, PERFORMED, FAILED = (
    "pending", "approved", "denied", "expired", "performed", "failed")

# Statuses that still owe the operator something.
OPEN = (PENDING,)

# Fields the pipeline adds to a request and must not be replayed as if the
# agent had supplied them.
_INTERNAL = ("_file", "_seq", "_stem")


class ApprovalError(Exception):
    """A decision this module refuses. The message is operator-readable."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_path(vault) -> Path:
    return Path(vault) / LEDGER


def approval_id(stem: str, file: str, verb: str) -> str:
    """Deterministic in (recording, request file, verb).

    `pipeline.py --retry` re-runs a whole outbox, and nothing may be enqueued
    twice — a duplicate approval request is indistinguishable from a bug, and
    approving one of two identical rows leaves the other pending forever.
    """
    return hashlib.sha256(f"{stem}|{file}|{verb}".encode()).hexdigest()[:12]


def _events(vault) -> list[dict]:
    p = ledger_path(vault)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue                      # torn concurrent append; skip the row
        if isinstance(d, dict) and d.get("id"):
            out.append(d)
    return out


def state(vault) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for ev in _events(vault):
        cur = out.setdefault(ev["id"], {})
        cur.update({k: v for k, v in ev.items() if v is not None})
    return out


def pending(vault) -> list[dict]:
    items = [a for a in state(vault).values() if a.get("status") in OPEN]
    return sorted(items, key=lambda a: str(a.get("created_at") or ""))


def append(vault, aid: str, status: str, **fields) -> dict:
    ev = {"id": aid, "status": status, "event_at": iso_z(_utcnow())}
    ev.update({k: v for k, v in fields.items() if v is not None})
    p = ledger_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Heal a torn previous write before appending, or this row glues onto it
    # and both become unreadable. Same rule as the todo and deferred ledgers.
    lead = ""
    try:
        with p.open("rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                lead = "\n"
    except OSError:
        pass
    with p.open("a") as f:
        f.write(lead + json.dumps(ev, sort_keys=True) + "\n")
    return ev


def enqueue(vault, req: dict, *, risk: str, summary: str, stem: str,
            outdir=None, ttl_hours: float = 24.0) -> dict:
    """Record one held action. Returns it, with `duplicate` if already queued."""
    aid = approval_id(stem, str(req.get("_file") or ""), str(req.get("verb") or ""))
    existing = state(vault).get(aid)
    if existing is not None:
        return {**existing, "id": aid, "duplicate": True}
    clean = {k: v for k, v in req.items() if k not in _INTERNAL}
    now = _utcnow()
    ev = append(vault, aid, PENDING,
                verb=str(req.get("verb") or ""), request=clean, risk=risk,
                summary=summary[:300], stem=stem or None,
                outdir=str(outdir) if outdir else None,
                nonce=secrets.token_urlsafe(9),
                created_at=iso_z(now),
                expires_at=iso_z(now + timedelta(hours=max(0.1, ttl_hours))))
    return {**ev, "duplicate": False}


def decide(vault, aid: str, decision: str, *, nonce: str = "",
           by: str = "push") -> dict:
    """Record approve/deny for a pending item. Refuses anything questionable.

    Every refusal here is a case where performing the action would be worse
    than doing nothing: an unknown id, a stale nonce, an item already decided,
    or one that has expired.
    """
    aid = str(aid or "").strip()
    cur = state(vault).get(aid)
    if cur is None:
        raise ApprovalError(f"no approval with id {aid!r}")
    if cur.get("status") != PENDING:
        raise ApprovalError(f"{aid} is already {cur.get('status')}")
    if cur.get("nonce") and nonce != cur.get("nonce"):
        # A replayed or guessed message. The topic is a bearer capability, so
        # this is the layer that stops an old push being re-tapped next week.
        raise ApprovalError(f"{aid}: nonce does not match")
    if _is_expired(cur):
        append(vault, aid, EXPIRED, reason="expired before the decision arrived")
        raise ApprovalError(f"{aid} expired at {cur.get('expires_at')}")
    if decision not in ("approve", "deny"):
        raise ApprovalError(f"unknown decision {decision!r}")
    status = APPROVED if decision == "approve" else DENIED
    return append(vault, aid, status, decided_at=iso_z(_utcnow()), decided_by=by)


def _is_expired(item: dict, *, now: datetime | None = None) -> bool:
    raw = str(item.get("expires_at") or "")
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now or _utcnow()) >= when


def expire_stale(vault, *, now: datetime | None = None) -> list[dict]:
    """Mark everything past its TTL. Returns what was expired, for one grouped
    notification — silently dropping a held action is the outcome with no
    recovery, since the operator believes it is still waiting."""
    out = []
    for item in pending(vault):
        if _is_expired(item, now=now):
            append(vault, item["id"], EXPIRED,
                   reason="not decided within the approval window")
            out.append(item)
    return out


def approved_ready(vault) -> list[dict]:
    """Approved but not yet performed, oldest first."""
    items = [a for a in state(vault).values() if a.get("status") == APPROVED]
    return sorted(items, key=lambda a: str(a.get("decided_at") or ""))


# ---------------------------------------------------------------------------
#  CLI — decide without a phone
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    """`approvals.py --list | --approve ID | --deny ID`.

    A terminal path matters for the same reason the push path does: the ntfy
    topic is a bearer capability and a channel that can be unavailable. This
    one requires filesystem access to the vault, which the sandboxed agent does
    not have — so it is not a way around the control, it is the same control
    reached from a place the agent cannot stand.
    """
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", type=Path, help="alternate ops/.env")
    ap.add_argument("--list", action="store_true", help="show pending approvals")
    ap.add_argument("--approve", metavar="ID")
    ap.add_argument("--deny", metavar="ID")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import Config
    try:
        cfg = Config(args.env)
    except Exception as e:                          # noqa: BLE001
        print(f"config error: {e}", file=sys.stderr)
        return 2
    vault = Path(cfg.vault)
    if not vault.is_dir():
        print(f"vault not found: {vault}", file=sys.stderr)
        return 2

    if args.approve or args.deny:
        aid = args.approve or args.deny
        want = "approve" if args.approve else "deny"
        cur = state(vault).get(aid.strip())
        try:
            # The nonce exists to stop a REPLAYED PUSH, not to stop the
            # operator at a keyboard — reaching this CLI already requires the
            # vault filesystem, which is a stronger proof than the topic.
            decide(vault, aid, want, nonce=(cur or {}).get("nonce", ""),
                   by="cli")
        except ApprovalError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"{want}d: {aid}  {(cur or {}).get('summary', '')}")
        print("It will be performed on the next processor pass."
              if want == "approve" else "Nothing will be performed.")
        return 0

    items = pending(vault)
    if not items:
        print("no approvals pending")
        return 0
    print(f"{len(items)} pending:")
    for a in items:
        print(f"  {a['id']}  [{a.get('risk')}]  {a.get('summary')}")
        print(f"            expires {a.get('expires_at')}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
