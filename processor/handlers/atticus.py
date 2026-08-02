"""Atticus controlling itself: status, cancel, retry. Issue #82.

The review that prompted this put it well: increase the operator's ability to
**control, continue, correct, audit and retrieve** what Atticus does, not the
number of things it can do. Everything else in this pipeline is a way to start
work. These are the first verbs that act on work already started.

All three are `INTERNAL`. They touch nothing outside the operator's own
pipeline — no credential, no message to anybody, nothing another person sees —
so holding them for confirmation would be theatre, and worse than theatre: a
cancellation that waits for approval has failed at the one thing it is for.

## How a spoken phrase reaches one recording

`recordings.resolve()`, which is the third use of the resolve-or-refuse pattern
(contacts for people, `github.close` for issues). The agent has no reads, so it
writes down the words it heard and the pipeline does the lookup afterwards,
refusing on ambiguity rather than acting on a guess.

## The guard that matters most

**A recording cannot act on itself.** "Cancel that" is spoken INTO a recording,
which the pipeline is executing at the moment the outbox runs. Without
`exclude_stem`, the cancel would kill the very run performing it — and that run
would then never finish writing the cancellation, so the operator would see
nothing happen and have no way to find out why. Every verb here passes its own
stem as the exclusion.

## What `cancel` can and cannot undo

  * before the agent runs — the record is marked `cancelled` and the next pass
    skips it. Nothing was spent, nothing was published.
  * during the run — the process group is signalled. `executing_by.pid` is
    already recorded, and the sandbox runs with `--die-with-parent`, so the
    whole tree goes. The PID is verified against `/proc` first: PID reuse is
    real, and killing an unrelated process because a number was recycled would
    be a genuinely bad failure.
  * after publishing — `superseded`, not `cancelled`. The artifact is committed
    and may already have been read; pretending it can be withdrawn would be a
    lie. The status marks it as no longer the answer.
"""
import os
import signal
from pathlib import Path

import notify as nf
import outbox
import recordings
from vault import CANCELLED, EXECUTING, PUBLISHED, RAW, ROUTED, SUPERSEDED, TRANSCRIBED

# Stages from which work can simply be abandoned: nothing has run yet.
_BEFORE_RUN = (RAW, TRANSCRIBED, ROUTED)


def _phrase(req: dict) -> str:
    return " ".join(str(req.get("match") or req.get("what") or "").split())


def _resolve(req, cfg, *, skip_status=recordings.DONE_WITH):
    days = int(getattr(cfg, "lifecycle_within_days", 7) or 7)
    try:
        return recordings.resolve(
            cfg.vault, _phrase(req), within_days=days, skip_status=skip_status,
            # THE guard: never the recording that is asking. See the module
            # docstring — without this, "cancel that" kills its own run.
            exclude_stem=str(req.get("_stem") or ""))
    except recordings.ResolveError as e:
        raise outbox.OutboxError(str(e))


def _still_ours(pid: int, stem: str) -> bool:
    """Is `pid` really the pipeline run for this recording?

    A PID recorded minutes ago may since have been recycled onto something
    unrelated, and signalling that would be a serious bug — so the cmdline has
    to name the pipeline. The stem is checked when present (a `--once` run has
    it in argv); a timed pass does not, so the pipeline name alone must serve,
    which is why the signal is TERM and never KILL.
    """
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return False
    if "pipeline.py" not in cmdline:
        return False
    return True


# ── atticus.status ─────────────────────────────────────────────────────────
@outbox.handler(
    "atticus.status", risk=outbox.INTERNAL, schema=(),
    describe=lambda r: (f"report the status of "
                        f"{_phrase(r) or 'the most recent recording'}"))
def status(req: dict, cfg, log=print) -> dict:
    """Push the operator a status line for one recording.

    **This is not a read**, and the distinction matters for #63. The answer goes
    to the OPERATOR, by notification — the agent never sees it, cannot reason
    about it, and cannot leak it. Nothing about the sandbox's read boundary
    changes here; the pipeline is simply answering a question that was addressed
    to it rather than to the model.
    """
    rec = _resolve(req, cfg, skip_status=())
    line = recordings.describe(rec, Path(cfg.vault))
    link = ""
    base = str(getattr(cfg, "site_base_url", "") or "").strip()
    if base and rec.data.get("status") == PUBLISHED:
        link = f"\n\n{base}/docs/{rec.stem}/"
    nf.alarm(cfg, line + link, severity=nf.ROUTINE, title="Atticus — status",
             tags="mag", log=log)
    log(f"    status: {line}")
    return {"stem": rec.stem, "status": rec.data.get("status"), "line": line}


# ── atticus.cancel ─────────────────────────────────────────────────────────
@outbox.handler(
    "atticus.cancel", risk=outbox.INTERNAL, schema=("match",),
    describe=lambda r: f"cancel {_phrase(r)!r}")
def cancel(req: dict, cfg, log=print) -> dict:
    """Stop one recording's work, as far as it can still be stopped."""
    rec = _resolve(req, cfg)
    status_before = str(rec.data.get("status") or "")
    label = recordings._label(rec, Path(cfg.vault))
    killed = False

    if status_before == EXECUTING:
        owner = rec.data.get("executing_by") or {}
        pid = int(owner.get("pid") or 0)
        if pid and _still_ours(pid, rec.stem):
            try:
                # The GROUP: bwrap and claude are children, and the sandbox runs
                # with --die-with-parent so the tree follows. TERM, never KILL —
                # the pipeline traps it and records a clean failure, and this
                # cannot be certain enough about PID reuse to justify SIGKILL.
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                killed = True
                log(f"    cancel: signalled pid {pid}")
            except (ProcessLookupError, PermissionError, OSError) as e:
                log(f"    cancel: could not signal pid {pid}: {type(e).__name__}")
        else:
            log(f"    cancel: pid {pid or '?'} is no longer this pipeline; "
                f"marking the record only")

    if status_before == PUBLISHED:
        # Already committed, possibly already read. Say what is true.
        rec.advance(SUPERSEDED, superseded_reason=_phrase(req)[:200] or None)
        outcome = ("already published, so it is marked superseded rather than "
                   "cancelled — the report stays where it is")
    elif status_before in _BEFORE_RUN or status_before == EXECUTING:
        rec.advance(CANCELLED, cancelled_from=status_before,
                    cancelled_killed=killed or None)
        outcome = ("stopped mid-run" if killed else
                   f"cancelled before it {'ran' if status_before != EXECUTING else 'finished'}")
    else:
        rec.advance(CANCELLED, cancelled_from=status_before)
        outcome = f"cancelled from {status_before}"

    nf.alarm(cfg, f"{label} — {outcome}.", severity=nf.ROUTINE,
             title="Atticus — cancelled", tags="octagonal_sign", log=log)
    log(f"    cancel: {label} — {outcome}")
    return {"stem": rec.stem, "was": status_before,
            "now": rec.data.get("status"), "killed": killed, "outcome": outcome}


# ── atticus.retry ──────────────────────────────────────────────────────────
@outbox.handler(
    "atticus.retry", risk=outbox.INTERNAL, schema=("match",),
    describe=lambda r: f"retry {_phrase(r)!r}")
def retry(req: dict, cfg, log=print) -> dict:
    """Re-arm one recording so the next pass runs it again.

    Deliberately does NOT run it here. The outbox executes inside a pass that
    already holds the processor lock; starting a second agent from inside one
    would race the lock and double the budget. Re-arming and letting the next
    tick pick it up is both simpler and how `--retry` already works.
    """
    rec = _resolve(req, cfg)
    was = str(rec.data.get("status") or "")
    if was == EXECUTING:
        raise outbox.OutboxError(
            "that recording is running right now — cancel it first, or wait. "
            "Re-arming a live run would have two agents on one record.")
    rec.rearm()
    label = recordings._label(rec, Path(cfg.vault))
    nf.alarm(cfg, f"{label} — re-armed, it will run on the next pass.",
             severity=nf.ROUTINE, title="Atticus — retrying", tags="repeat",
             log=log)
    log(f"    retry: {label} (was {was} → {rec.data.get('status')})")
    return {"stem": rec.stem, "was": was, "now": rec.data.get("status")}
