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
import html
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


def gate(cfg, risk: str, verb: str = "") -> str:
    """"auto" | "confirm" | "off" for this action.

    A per-VERB override wins over the risk class, because the classes alone are too
    coarse to express what an operator actually wants. Observed while building the
    Outlook handlers: setting ATTICUS_OUTBOX_TRACKED=auto so GitHub issues can flow
    unattended ALSO opens `outlook.event`, i.e. calendar invites to other people.
    Those do not belong in one bucket, and without an override the only way to open
    the verb you want is to open several you do not — which is an incentive to
    over-grant.

    So the classes stay as sane defaults and the override handles the exception:

        ATTICUS_OUTBOX_VERB_GITHUB_ISSUE=auto

    ATTICUS_OUTBOX=off still wins over everything, since its whole purpose is a
    global stop.
    """
    if (getattr(cfg, "outbox", "on") or "on").strip().lower() == "off":
        return "off"
    per_verb = getattr(cfg, "outbox_verbs", None) or {}
    if verb and verb.lower() in per_verb:
        return str(per_verb[verb.lower()]).strip().lower()
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


def _primary_html(outdir: Path) -> Path | None:
    """The report the receipt belongs in — the same choice the site build makes."""
    htmls = sorted(outdir.glob("*.html"))
    if not htmls:
        return None
    for h in htmls:
        if h.name == "index.html":
            return h
    return max(htmls, key=lambda p: p.stat().st_size)


def process(outdir: Path, cfg, *, log=print, stem: str = "",
            max_actions: int | None = None) -> dict:
    """Perform every request in an outbox. Never raises.

    Returns a summary and writes `outbox-receipt.json` beside the deliverable, so
    what was attempted and what came of it is committed with the record rather
    than living only in a log.
    """
    reqs = read_requests(outdir, log=log)
    if not reqs:
        return {"requests": 0, "done": 0, "refused": 0, "failed": 0, "receipts": []}
    for req in reqs:
        # Which recording asked. Underscored like _file: pipeline-supplied, so a
        # request cannot claim to be another recording — handlers that derive
        # idempotency keys from it (todo.add) rely on that.
        req["_stem"] = stem
        # Where that recording's deliverable lives. Same rule: pipeline-supplied,
        # so a request cannot name a directory of its own. image.generate writes
        # a file the report already references, and it must land beside that
        # report rather than anywhere the request asks for.
        req["_outdir"] = str(outdir)

    # The fan-out bound. Overridable per record because a meeting genuinely
    # produces more action items than a spoken command ever does (#86), and the
    # alternative — silently dropping the sixth — is the quiet failure this
    # project treats as the worst kind.
    cap = int(max_actions if max_actions is not None
              else (getattr(cfg, "outbox_max_actions", 5) or 0))
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
        decision = gate(cfg, h["risk"], str(req.get("verb") or ""))
        if decision != "auto":
            # "confirm" and "off" both mean *not now*, and they now diverge in
            # what happens next.
            #
            # OFF stays exactly as it was: intent recorded, nothing performed,
            # no queue. Off must mean off — turning it into a queue would make
            # the global stop into a global "later", which is not what anyone
            # reaching for it wants.
            #
            # CONFIRM enqueues for approval (#83). It used to mean *held
            # forever*: nothing could ever approve it, so the middle setting was
            # `off` with better paperwork, and the only way to make a verb work
            # was to open it to `auto`. That pushed every enabled verb to auto —
            # an incentive to over-grant, which per-verb gates existed to stop.
            queued = None
            if decision == "confirm" and getattr(cfg, "approvals_enabled", False):
                # Imported HERE, not at module scope: approval_drain imports
                # this module to reach validate() and the handler table, and a
                # cycle that happens to work today is a cycle that breaks the
                # first time somebody reorders an import.
                import approval_drain
                import approvals
                try:
                    queued = approvals.enqueue(
                        cfg.vault, req, risk=h["risk"], summary=rec["summary"],
                        stem=stem, outdir=outdir,
                        ttl_hours=float(getattr(cfg, "approval_ttl_hours", 24)))
                except Exception as e:                           # noqa: BLE001
                    # A queue that cannot be written must not lose the action's
                    # receipt — the operator still needs to know it was asked for.
                    log(f"    ! could not queue for approval: {type(e).__name__}: {e}")

            if queued and not queued.get("duplicate"):
                approval_drain.announce(cfg, queued, log=log)
            if queued:
                rec.update(status="held", approval_id=queued["id"],
                           expires_at=queued.get("expires_at"),
                           reason=(f"awaiting approval — decide from the push, or "
                                   f"`atticus approvals --approve {queued['id']}`"))
            else:
                rec.update(status="held", reason=(
                    "ATTICUS_OUTBOX=off — intent recorded, nothing performed"
                    if decision == "off" else
                    f"{h['risk']} actions need confirmation. Open this class with "
                    f"ATTICUS_OUTBOX_{h['risk'].upper()}=auto, or just this verb with "
                    f"ATTICUS_OUTBOX_VERB_"
                    f"{str(req.get('verb') or '').upper().replace('.', '_')}=auto"))
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

    # Put the outcome in the REPORT, not only in a sidecar file. The agent could
    # not know it — process() runs after it exits — so without this the operator
    # reads a document that says "pending" about something that already happened.
    injected = False
    try:
        report = _primary_html(outdir)
        if report is not None:
            injected = inject_receipt(report, receipt_html(receipts))
    except OSError as e:
        log(f"    ! could not add the receipt to the report: {e}")

    summary = {"requests": len(reqs), "done": done, "refused": refused,
               "failed": failed, "injected": injected, "receipts": receipts}
    try:
        (outdir / "outbox-receipt.json").write_text(
            json.dumps({"stem": stem, **summary}, indent=2) + "\n")
    except OSError as e:
        log(f"    ! could not write the outbox receipt: {e}")
    return summary


# The receipt block injected into the report, comment-fenced so a re-run replaces
# it rather than stacking. Same mechanism as the audio player (podcast.py), and for
# the same reason: comments survive the vault's sanitiser, which only rewrites
# active constructs, and a class name cannot tell "has a receipt" from "has a
# CURRENT receipt".
BLOCK_OPEN = "<!--atticus-outbox-->"
BLOCK_CLOSE = "<!--/atticus-outbox-->"
_BLOCK = re.compile(re.escape(BLOCK_OPEN) + r".*?" + re.escape(BLOCK_CLOSE), re.DOTALL)
_BODY_OPEN = re.compile(r"(?i)<body[^>]*>")

_STATUS_LABEL = {"done": "Done", "held": "Waiting for you",
                 "refused": "Refused", "failed": "Failed"}


def receipt_html(receipts: list[dict]) -> str:
    """What actually happened, for the report the operator reads.

    This exists because `process()` runs AFTER the agent exits, so the agent
    physically cannot put a filed ticket's id or a sent message's outcome in the
    HTML it wrote. Without this the skills had to tell the agent to write "pending"
    — and the report then said pending forever, including long after the action
    succeeded. Found while building the Azure DevOps handler.
    """
    if not receipts:
        return ""
    rows = []
    for r in receipts:
        st = r.get("status", "refused")
        label = _STATUS_LABEL.get(st, st)
        bits = [f'<strong>{html.escape(label)}</strong> &middot; '
                f'{html.escape(str(r.get("summary") or r.get("verb") or ""))}']
        if r.get("url"):
            u = html.escape(str(r["url"]), quote=True)
            bits.append(f'<a href="{u}">{u}</a>')
        elif r.get("id"):
            bits.append(f'<code>{html.escape(str(r["id"]))}</code>')
        if r.get("reason"):
            bits.append(f'<span class="ao-why">{html.escape(str(r["reason"]))}</span>')
        rows.append(f'<li class="ao-{html.escape(st)}">' + " &mdash; ".join(bits) + "</li>")
    return (
        BLOCK_OPEN + "\n"
        "<style>.atticus-outbox{margin:0 0 1.5rem;padding:.9rem 1rem;border-radius:8px;"
        "border:1px solid rgba(127,127,127,.35);background:rgba(127,127,127,.08);"
        "font:14px/1.55 system-ui,-apple-system,\"Segoe UI\",Roboto,Arial,sans-serif}"
        ".atticus-outbox .lab{display:block;margin-bottom:.5rem;opacity:.75;"
        "text-transform:uppercase;letter-spacing:.05em;font-size:11px;font-weight:600}"
        ".atticus-outbox ul{margin:0;padding-left:1.1rem}"
        ".atticus-outbox li{margin:.25rem 0}"
        ".atticus-outbox .ao-why{opacity:.75}"
        ".atticus-outbox .ao-held .ao-why,.atticus-outbox .ao-refused .ao-why,"
        ".atticus-outbox .ao-failed .ao-why{opacity:.9}</style>\n"
        '<div class="atticus-outbox"><span class="lab">Actions</span><ul>'
        + "".join(rows) + "</ul></div>\n" + BLOCK_CLOSE + "\n")


def inject_receipt(html_path: Path, block: str) -> bool:
    """Splice the receipt near the top of the report. Idempotent, and replaces."""
    text = html_path.read_text(errors="replace")
    existing = _BLOCK.search(text)
    if existing:
        if existing.group(0).strip() == block.strip():
            return False
        text = text[:existing.start()] + block + text[existing.end():]
        html_path.write_text(text)
        return True
    m = _BODY_OPEN.search(text)
    text = (text[:m.end()] + "\n" + block + text[m.end():]) if m else block + text
    html_path.write_text(text)
    return True


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
