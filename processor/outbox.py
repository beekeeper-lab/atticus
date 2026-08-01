"""The outbox: how a sandboxed agent causes something to happen outside itself.

Resolves issue #42. Every credentialed skill goes through here.

## Why this exists

The agent executes text derived from ambient audio, so it holds no credentials —
`wrap_sandbox()` binds the `claude` binary and two named skill directories and
nothing else. No `~/.secrets`, no `~/.config/ai/env`, no `~/.ssh`, not even the
rest of `~/.local/bin`. That is deliberate and it is the main control in the
system, so "a skill that calls an API" cannot be how any of this works.

So the work splits at the **intent boundary**, exactly as the audio overview does
(the agent writes a script; the pipeline voices it):

    agent, sandboxed        writes output/outbox/NNN-verb.json   — no credential
    pipeline, credentialed  validates it, does it, writes a receipt

The intent file is committed to the vault before anything happens, which makes git
history the audit trail for *what was asked* separately from *what was done*.

## Why sends are gated and reads are not here

An action like `signal.send` or `ado.workitem` is **outward-facing and hard to
reverse**, and the instruction originates in a microphone worn in public. So:

  * every handler declares a risk class, and `confirm` policy is per class;
  * a per-pass cap bounds fan-out, because one misheard sentence should not be
    able to send thirty messages;
  * `ATTICUS_OUTBOX=off` disables execution entirely while still recording intent,
    which is also how you test a new handler safely.

**Reads are NOT solved by this file, deliberately.** "What's on my calendar" needs
data *during* the agent's run, which an outbox cannot provide. The options are a
credential-holding loopback broker the agent can query — powerful, and a large new
prompt-injection surface — or pipeline-side pre-fetch, which is safe but cannot
answer an arbitrary question. That is a separate decision with a worse risk profile
and it gets its own issue rather than being smuggled in here. Skills that only need
to *do* things work today; skills that need to *look things up* wait for it.
"""
import json
import re
from pathlib import Path

OUTBOX_DIR = "outbox"

# Risk classes. What differs is the default gate, not the mechanism.
#
#   internal   visible only to the operator, trivially undone (a todo, a reminder)
#   tracked    visible to others but recoverable and expected (a GitHub issue, an
#              ADO work item — systems whose whole purpose is things people file)
#   outward    a message to a person, immediate, not recallable (Signal, mail, Slack)
INTERNAL, TRACKED, OUTWARD = "internal", "tracked", "outward"
RISK_ORDER = (INTERNAL, TRACKED, OUTWARD)

_VERB = re.compile(r"^[a-z][a-z0-9]*\.[a-z][a-z0-9_]*$")
_FILENAME = re.compile(r"^(\d{1,4})-([a-z0-9.\-_]+)\.json$")

# A handler registers itself here. `verb` is `<service>.<action>`.
_HANDLERS: dict[str, dict] = {}


class OutboxError(Exception):
    """A request could not be performed. Never fatal to the record."""


def handler(verb: str, *, risk: str, schema: tuple[str, ...] = (),
            describe=None):
    """Register a handler for one verb.

    `schema` is the required field names — enough to reject a malformed request
    before it reaches a credential, not a full validator. `describe(req)` returns
    the one-line human summary used in confirmations and receipts, and is what the
    operator actually reads, so handlers should make it specific.
    """
    if not _VERB.match(verb):
        raise ValueError(f"verb must look like 'service.action', got {verb!r}")
    if risk not in RISK_ORDER:
        raise ValueError(f"risk must be one of {RISK_ORDER}, got {risk!r}")

    def register(fn):
        _HANDLERS[verb] = {"fn": fn, "risk": risk, "schema": tuple(schema),
                           "describe": describe or (lambda req: verb)}
        return fn
    return register


def known_verbs() -> list[str]:
    return sorted(_HANDLERS)


def handler_for(verb: str) -> dict | None:
    return _HANDLERS.get(verb)


def outbox_path(outdir: Path) -> Path:
    return outdir / OUTBOX_DIR


def read_requests(outdir: Path, *, log=print) -> list[dict]:
    """Parse `output/outbox/*.json`, in filename order.

    Filenames are `NNN-verb.json` so the agent controls ordering — "file the ticket
    then tell Robbie about it" has a sequence, and lexical sort on a zero-padded
    prefix preserves it. A malformed file is reported and skipped rather than
    failing the batch: one bad request must not silence a good one beside it.
    """
    d = outbox_path(outdir)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"), key=lambda x: x.name):
        m = _FILENAME.match(p.name)
        if not m:
            log(f"    ! outbox: ignoring {p.name} — expected NNN-verb.json")
            continue
        try:
            req = json.loads(p.read_text(errors="replace"))
        except json.JSONDecodeError as e:
            log(f"    ! outbox: {p.name} is not valid JSON ({e})")
            continue
        if not isinstance(req, dict):
            log(f"    ! outbox: {p.name} is not a JSON object")
            continue
        req["_file"] = p.name
        req["_seq"] = int(m.group(1))
        out.append(req)
    return out


def validate(req: dict) -> dict:
    """Return the handler for a request, or raise OutboxError saying why not.

    Refusing an unknown verb by name matters: a skill that ships a typo, or an
    agent that invents a plausible-looking action, must fail loudly here rather
    than be silently dropped and leave the operator believing something happened.
    """
    verb = str(req.get("verb") or "").strip().lower()
    if not verb:
        raise OutboxError("request has no 'verb'")
    h = _HANDLERS.get(verb)
    if h is None:
        raise OutboxError(f"unknown verb {verb!r}; known: {', '.join(known_verbs()) or 'none'}")
    missing = [f for f in h["schema"] if not str(req.get(f) or "").strip()]
    if missing:
        raise OutboxError(f"{verb} needs {', '.join(missing)}")
    return h


def gate(cfg, risk: str) -> str:
    """"auto" | "confirm" | "off" for this risk class.

    Read from ATTICUS_OUTBOX_<RISK>, so the three classes are configured
    independently and the strictest default applies to the one that cannot be
    undone. Defaults: internal auto, tracked confirm, outward confirm.
    """
    if (getattr(cfg, "outbox", "on") or "on").strip().lower() == "off":
        return "off"
    return {
        INTERNAL: getattr(cfg, "outbox_internal", "auto"),
        TRACKED: getattr(cfg, "outbox_tracked", "confirm"),
        OUTWARD: getattr(cfg, "outbox_outward", "confirm"),
    }.get(risk, "confirm")


def describe(req: dict) -> str:
    h = _HANDLERS.get(str(req.get("verb") or "").lower())
    if h is None:
        return str(req.get("verb") or "unknown action")
    try:
        return str(h["describe"](req))[:300]
    except Exception:                               # noqa: BLE001
        return str(req.get("verb"))


def process(outdir: Path, cfg, *, log=print, stem: str = "") -> dict:
    """Perform every request in an outbox. Never raises.

    Returns a summary and writes `outbox-receipt.json` beside the deliverable, so
    what was attempted and what came of it is committed with the record rather
    than living only in a log.
    """
    reqs = read_requests(outdir, log=log)
    if not reqs:
        return {"requests": 0, "done": 0, "refused": 0, "failed": 0, "receipts": []}

    cap = int(getattr(cfg, "outbox_max_actions", 5) or 0)
    receipts = []
    done = refused = failed = 0

    log(f"    outbox: {len(reqs)} request(s)")
    for i, req in enumerate(reqs):
        rec = {"file": req.get("_file"), "verb": req.get("verb"),
               "summary": describe(req)}
        if cap and i >= cap:
            rec.update(status="refused",
                       reason=f"per-pass cap of {cap} actions reached")
            log(f"    ✗ {rec['summary']}: {rec['reason']}")
            receipts.append(rec)
            refused += 1
            continue
        try:
            h = validate(req)
        except OutboxError as e:
            rec.update(status="refused", reason=str(e))
            log(f"    ✗ outbox: {e}")
            receipts.append(rec)
            refused += 1
            continue

        rec["risk"] = h["risk"]
        decision = gate(cfg, h["risk"])
        if decision != "auto":
            # "confirm" and "off" both mean *not now*. Nobody is present to
            # approve during an unattended pass, so this records the intent and
            # stops. It is not a failure and must not read as one.
            rec.update(status="held", reason=(
                "ATTICUS_OUTBOX=off — intent recorded, nothing performed"
                if decision == "off" else
                f"{h['risk']} actions need confirmation "
                f"(ATTICUS_OUTBOX_{h['risk'].upper()}=confirm)"))
            log(f"    ⧗ held: {rec['summary']} — {rec['reason']}")
            receipts.append(rec)
            refused += 1
            continue

        try:
            result = h["fn"](req, cfg, log=log) or {}
        except OutboxError as e:
            rec.update(status="failed", reason=str(e))
            log(f"    ✗ {rec['summary']}: {e}")
            receipts.append(rec)
            failed += 1
            continue
        except Exception as e:                      # noqa: BLE001
            # A handler bug must not cost the report the agent already wrote.
            rec.update(status="failed", reason=f"{type(e).__name__}: {e}")
            log(f"    ✗ {rec['summary']}: {type(e).__name__}: {e}")
            receipts.append(rec)
            failed += 1
            continue
        rec.update(status="done", **{k: v for k, v in result.items()
                                     if k not in ("status", "reason")})
        log(f"    ✓ {rec['summary']}")
        receipts.append(rec)
        done += 1

    summary = {"requests": len(reqs), "done": done, "refused": refused,
               "failed": failed, "receipts": receipts}
    try:
        (outdir / "outbox-receipt.json").write_text(
            json.dumps({"stem": stem, **summary}, indent=2) + "\n")
    except OSError as e:
        log(f"    ! could not write the outbox receipt: {e}")
    return summary


# The block a skill pastes into its own SKILL.md. One source of truth for the
# contract, so ten skills cannot drift into ten dialects of it.
CONTRACT = """## Causing something to happen outside this sandbox

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
"""
