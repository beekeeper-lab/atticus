"""`signal.send` — a private message to a real person. Issue #47.

This is the highest-consequence handler in the system and the only one whose
mistakes cannot be taken back. A bad research report can be ignored; a Signal
message sent to the wrong human is delivered, read, and permanent. The
instruction originates in a microphone worn in public and passes through a
speech-to-text model, so *the recipient name is the least reliable token in the
whole request* — proper nouns are exactly what transcription gets wrong.

Everything below follows from that one sentence.

## Recipient safety: an allowlist, exact matching, and no guessing

`ATTICUS_SIGNAL_RECIPIENTS` maps a spoken label to an E.164 number. It is
**mandatory**: an empty allowlist refuses everything. There is deliberately no
`ATTICUS_SIGNAL_ALLOW_ANY` escape hatch, because the only thing such a setting
could buy is the failure this handler exists to prevent.

Three rules, each of which is a refusal rather than a fallback:

1. **Matching is exact** (case- and whitespace-insensitive) and nothing else.
   No fuzzy match, no prefix match, no substring, no "closest entry". This is
   the same conclusion `processor/wake.py` reached empirically for the wake
   phrase: measured against real mishearings, similarity scoring ranks the
   wrong candidate above the right one often enough to be worse than useless.
   And the failure modes are not symmetric — refusing to send is a five-second
   annoyance, while sending "I'm leaving him" to the wrong Nadia is
   unrecoverable. When the cost of a false positive is unbounded and the cost of
   a false negative is a retry, you do not interpolate.

2. **A bare phone number is not a bypass.** `to: "+15551234567"` is accepted
   only if that exact number is already a value in the allowlist. Otherwise the
   agent (or anything that reached the agent through the transcript) could
   address an arbitrary handset by skipping name resolution entirely.

3. **One recipient per request.** A list, or a label that looks like several
   names joined together, is refused with instructions to write one file per
   message. The per-pass cap in `outbox.py` counts *files*, so silently
   splitting a comma-separated string here would let one misheard sentence
   broadcast past a bound that appears to hold.

Contact resolution (#43) does not exist yet, and when it does it plugs in at
`_resolve()` — as a *source of candidate labels*, never as an authority. Even a
perfect address book must still be intersected with this allowlist: knowing a
number is not permission to message it unattended.

## Failure is the normal state today

Nothing is installed on this host: no `signal-cli`, no REST wrapper, no linked
account. So the expected outcome of `signal.send` right now is a clean
`OutboxError` that names the missing piece and how to get it. A stack trace is
not a diagnosis.

Request validation runs **before** the environment check, on purpose. An
allowlist refusal is a durable fact about the request that the operator needs to
see in the receipt; "signal-cli is missing" is a fact about the host. Checking
the host first would mask every safety refusal behind an install error for as
long as the service is unconfigured — which is to say, exactly during the period
when the safety logic is least exercised.

## Setup (operator)

    # Fedora/generic: signal-cli needs a JVM
    sudo dnf install java-21-openjdk-headless
    curl -L -o /tmp/signal-cli.tar.gz \
      https://github.com/AsamK/signal-cli/releases/latest/download/signal-cli-<ver>.tar.gz
    sudo tar xf /tmp/signal-cli.tar.gz -C /opt && \
      sudo ln -sf /opt/signal-cli-<ver>/bin/signal-cli /usr/local/bin/signal-cli

Then choose an identity. **Device link is the recommended path:**

    signal-cli link -n atticus-forge      # prints an sgnl:// URI
    # render it as a QR (qrencode -t ANSI) and scan with Signal ▸ Linked devices
    # then: ATTICUS_SIGNAL_FROM=<your own number, E.164>

Messages arrive from *you*, so recipients recognise the sender and threads stay
continuous with your phone — which matters when the whole point is "tell Robbie
I'm late". The cost is that this host can send as you, and unlinking is the only
revocation.

A **dedicated number** (`signal-cli -a +1555… register`) is cleaner containment —
compromise cannot impersonate you, and it is trivially killable — but every
message arrives from a number nobody has saved, which is close to useless for
short logistical notes. It is the right choice only if this ever sends to people
outside a small circle.

Either way `signal-cli`'s state is a **directory** (`~/.local/share/signal-cli`
by default, or `ATTICUS_SIGNAL_CONFIG_DIR`), not an API key. Containment
therefore means paths, not variables: the pipeline needs it and the *agent* must
never see it. `bwrap` binds only the workspace, `/usr`, `/etc`, the CLI and the
skills directories, so the default location is already outside the sandbox — but
if you move the store, do not move it under anything the agent can read, and
consider `InaccessiblePaths=` on the processor unit the way the Plaud session is
handled.

## Reading is not implemented

Issue #47 also asks for reading recent messages. That needs data *during* the
agent's run, which an outbox structurally cannot provide (see the module
docstring in `outbox.py`), and it would commit other people's message bodies to
git forever. Out of scope here, deliberately.
"""
import re
import shutil
import subprocess

import outbox
from outbox import OutboxError
from redact import redact

VERB = "signal.send"

# E.164. Also the shape a config typo fails.
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
# Two or more names jammed into one label. Checked only after an exact allowlist
# match has already failed, so a legitimate label containing a comma still works.
_MULTI = re.compile(r",|;|&|\band\b|\+(?!\d)", re.IGNORECASE)
# signal-cli prints the sent message's timestamp; useful in the receipt.
_TIMESTAMP = re.compile(r"\b(\d{13})\b")

_INSTALL = (
    "install signal-cli (needs a JVM), link this host with "
    "`signal-cli link -n atticus`, then set ATTICUS_SIGNAL_FROM and "
    "ATTICUS_SIGNAL_RECIPIENTS — see the setup notes in "
    "processor/handlers/signal.py"
)


def _clean_number(raw: str) -> str:
    return re.sub(r"[\s\-().]", "", str(raw or "").strip())


def _label(raw) -> str:
    """Normalise a spoken label: collapse whitespace, drop case."""
    return " ".join(str(raw or "").split())


def allowlist(cfg) -> tuple[dict[str, str], set[str]]:
    """`{normalised label: E.164}` plus the labels that are ambiguous.

    Accepts either a mapping or the raw `name=+1555...,other=+1555...` string,
    so this handler does not depend on how config.py chooses to parse the
    setting. A malformed entry is dropped rather than passed to signal-cli: a
    number we cannot validate is a number we cannot claim was intended.
    """
    raw = getattr(cfg, "signal_recipients", None) or {}
    if isinstance(raw, str):
        pairs = [p.split("=", 1) for p in raw.split(",") if "=" in p]
    elif isinstance(raw, dict):
        pairs = list(raw.items())
    else:                                            # a list of "name=+1555…"
        pairs = [str(p).split("=", 1) for p in raw if "=" in str(p)]

    out: dict[str, str] = {}
    ambiguous: set[str] = set()
    for name, number in pairs:
        key = _label(name).casefold()
        num = _clean_number(number)
        if not key or not _E164.match(num):
            continue
        if key in out and out[key] != num:
            # Two different numbers under one spoken name. Guessing between them
            # is the exact failure this file is about, so the name is poisoned.
            ambiguous.add(key)
        out[key] = num
    return out, ambiguous


def _resolve(to, cfg) -> tuple[str, str]:
    """Return `(label, number)` or raise OutboxError. Never guesses.

    Future integration point for contact resolution (#43): it may propose
    candidate labels to look up here, but the allowlist stays the authority.
    """
    allow, ambiguous = allowlist(cfg)
    if not allow:
        raise OutboxError(
            "no Signal recipients are allowlisted, so nothing can be sent: set "
            "ATTICUS_SIGNAL_RECIPIENTS (e.g. \"robbie=+15551234567\"). This is "
            "mandatory by design — a misheard name must refuse, not guess.")

    if isinstance(to, (list, tuple, set)):
        raise OutboxError(
            "signal.send takes ONE recipient; write one outbox file per message "
            f"({len(to)} were listed in a single request)")

    want = _label(to)
    key = want.casefold()
    if key in ambiguous:
        raise OutboxError(
            f"{want!r} maps to more than one number in ATTICUS_SIGNAL_RECIPIENTS; "
            "refusing rather than picking one. Use distinct labels.")
    if key in allow:
        return want, allow[key]

    # Exact match failed. Say something more useful than "no" where we can.
    if _MULTI.search(want):
        raise OutboxError(
            f"{want!r} looks like several recipients; signal.send takes ONE, and "
            "splitting it here would let one request send several messages past "
            "the per-pass cap. Write one file per message.")
    num = _clean_number(want)
    if _E164.match(num):
        if num in allow.values():
            back = next(k for k, v in allow.items() if v == num)
            return back, num
        raise OutboxError(
            f"the number ending {num[-4:]} is not in ATTICUS_SIGNAL_RECIPIENTS; "
            "a raw number is not a way around the allowlist")
    raise OutboxError(
        f"{want!r} is not on the Signal allowlist, so nothing was sent. "
        f"Allowed: {', '.join(sorted(allow)) or 'none'}. Recipient names are "
        "matched exactly and never guessed — transcription mishears names, and a "
        "message to the wrong person cannot be recalled.")


def _describe(req: dict) -> str:
    """What the operator reads in the confirmation prompt and the receipt.

    It has to carry enough to approve or reject — which means the recipient as
    heard and the actual words — but it is committed to git, so it is bounded and
    passed through the credential redactor like every other logged string.
    """
    to = _label(req.get("to")) or "?"
    body = " ".join(str(req.get("body") or "").split())
    preview = body[:110] + ("…" if len(body) > 110 else "")
    return f"Signal to {to}: “{redact(preview)}”"


@outbox.handler(VERB, risk=outbox.OUTWARD, schema=("to", "body"),
                describe=_describe)
def send(req: dict, cfg, log=print) -> dict:
    """Send one Signal message to one allowlisted recipient."""
    # 1. The request. Refusals here are facts about what was asked and must not
    #    be hidden behind an unconfigured host (see the module docstring).
    label, number = _resolve(req.get("to"), cfg)

    body = str(req.get("body") or "").strip()
    limit = int(getattr(cfg, "signal_max_chars", 1000) or 0)
    if limit and len(body) > limit:
        # Refuse, never truncate. A message to a person cut at a character
        # boundary can invert its own meaning, and the operator would have no way
        # to know the recipient read half a sentence.
        raise OutboxError(
            f"the message is {len(body)} characters and ATTICUS_SIGNAL_MAX_CHARS "
            f"caps it at {limit}; nothing was sent. Shorten it — a truncated "
            "message to a person is worse than none.")

    # 2. The host. Absent is the normal state, so say what is missing and how.
    binary = str(getattr(cfg, "signal_cli", "signal-cli") or "signal-cli").strip()
    exe = shutil.which(binary)
    if not exe:
        raise OutboxError(f"{binary!r} is not installed or not on PATH: {_INSTALL}")

    account = _clean_number(getattr(cfg, "signal_from", ""))
    if not account:
        raise OutboxError(
            "ATTICUS_SIGNAL_FROM is not set — signal-cli needs the account it "
            f"sends as (the number you linked, in E.164 form): {_INSTALL}")
    if not _E164.match(account):
        raise OutboxError(
            f"ATTICUS_SIGNAL_FROM={account!r} is not an E.164 number "
            "(it must look like +15551234567)")

    cmd = [exe]
    store = str(getattr(cfg, "signal_config_dir", "") or "").strip()
    if store:
        cmd += ["--config", store]
    # `-m BODY` consumes the next argument as its value, so a body that begins
    # with "-" cannot become a flag. No shell is involved anywhere.
    cmd += ["-a", account, "send", "-m", body, number]

    masked = f"…{number[-4:]}"
    log(f"      signal: sending {len(body)} chars to {label} <{masked}>")
    timeout = float(getattr(cfg, "signal_timeout", 60) or 60)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        # Ambiguous on purpose: signal-cli may have delivered before we gave up,
        # so the receipt must not claim either outcome.
        raise OutboxError(
            f"signal-cli did not finish within {timeout:.0f}s; the message to "
            f"{label} may or may not have been delivered — check Signal before "
            "retrying (ATTICUS_SIGNAL_TIMEOUT)")
    except FileNotFoundError:
        raise OutboxError(f"{binary!r} disappeared between lookup and run: {_INSTALL}")

    if p.returncode != 0:
        tail = [ln for ln in redact(p.stderr or p.stdout).splitlines() if ln.strip()]
        raise OutboxError(
            f"signal-cli exited {p.returncode} and nothing was sent to {label}"
            + (f": {tail[-1][:300]}" if tail else ""))

    ts = _TIMESTAMP.search(p.stdout or "")
    return {"recipient": label, "to": masked, "chars": len(body),
            **({"message_timestamp": ts.group(1)} if ts else {})}
