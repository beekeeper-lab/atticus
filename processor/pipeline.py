#!/usr/bin/env python3
"""Atticus processor — the Forge half.

    git pull → scan inbox → transcribe → route → execute → commit

Each stage advances the record's status and commits, so a crash resumes
rather than redoing work, and a failure in one stage never costs the others.

    pipeline.py                run one pass over the vault
    pipeline.py --once ID      process a single recording
    pipeline.py --retry ID     re-arm a failed record and run it now
    pipeline.py --retry-all    re-arm everything failed or waiting
    pipeline.py --status       show the queue, change nothing
    pipeline.py --dry-run      everything except the agent call

Exit: 0 clean · 1 some records failed · 2 usage/config error · 3 vault unreachable
"""
import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config                                    # noqa: E402
from lock import AlreadyRunning, single_instance             # noqa: E402
import execute as ex                                         # noqa: E402
import transcribe as stt                                     # noqa: E402
import wake                                                  # noqa: E402
from notify import notify as _notify                          # noqa: E402
from vault import (                                          # noqa: E402
    EXECUTED, FAILED, PUBLISHED, RAW, RETRY_WAIT, ROUTED, TRANSCRIBED,
    Git, VaultSyncError, load_records, write_atomic,
)

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class Log:
    def __init__(self, level="INFO"):
        self.min = LEVELS.get(level.upper(), 20)

    def _e(self, lvl, msg):
        if LEVELS[lvl] >= self.min:
            print(f"[{lvl[0]}] {msg}", flush=True)

    def debug(self, m): self._e("DEBUG", m)
    def info(self, m):  self._e("INFO", m)
    def warn(self, m):  self._e("WARNING", m)
    def error(self, m): self._e("ERROR", m)


def notify(cfg, text, log):
    _notify(cfg, text, log=log.warn, title="Atticus processor")


def primary_doc(outdir: Path) -> Path | None:
    """The deliverable a human should be pointed at.

    Same rule the vault browser uses to pick a recording's page — index.html if
    present, otherwise the largest HTML file. Deliberately scans the vault
    directory rather than trusting the execute stage's file list, because the
    agent currently writes straight into the vault (deploy report defect #2) and
    that list can be just the salvaged response.md stub.
    """
    if not outdir.is_dir():
        return None
    htmls = sorted(outdir.glob("*.html"))
    if not htmls:
        return None
    return next((h for h in htmls if h.name == "index.html"),
                max(htmls, key=lambda p: p.stat().st_size))


def doc_url(cfg, rec, doc: Path | None) -> str:
    """Public URL for a recording's page, or "" when no site is configured.

    Mirrors the browser's published layout: docs/<stem>/<filename>.
    """
    if not (cfg.site_base_url and doc):
        return ""
    return f"{cfg.site_base_url}/docs/{rec.stem}/{doc.name}"


def notify_result(cfg, rec, log):
    """Push the outcome of a recording, with a link to what it produced.

    This is the payoff of the whole pipeline, so it is worth getting right: a
    lock-screen message should say what came back and be tappable.
    """
    url = getattr(cfg, "result_notify_url", None)
    if not url:
        return
    executed = bool(rec.data.get("executed", True))
    if not executed and not cfg.notify_notes:
        return

    # What was asked for, in the operator's own words — far more recognisable on
    # a lock screen than a timestamp stem.
    said = ""
    try:
        said = rec.transcript_path(cfg.vault).read_text().strip()
    except OSError:
        pass
    detail = getattr(cfg, "notification_detail", "full")
    if detail == "title":
        said = ""                                   # link only; nothing spoken
    elif detail == "summary":
        said = (said[:60] + "…") if len(said) > 60 else said
    else:
        said = (said[:180] + "…") if len(said) > 180 else said

    if executed:
        doc = primary_doc(rec.outdir(cfg.vault))
        link = doc_url(cfg, rec, doc)
        title, tags, priority = "Atticus finished", "white_check_mark", "high"
        # High, not default. Results were sent at default priority and went
        # unnoticed for hours — iOS delivers those quietly. The entire premise
        # is that you walked away, so the result has to reach you.
        body = said or rec.stem
        if link:
            body += f"\n\n{link}"
        elif doc:
            # No site configured; name the file so it is still findable.
            body += f"\n\n{doc.name}"
    else:
        title, tags, priority = "Atticus filed a note", "memo", "low"
        body = f"{said or rec.stem}\n\nNot executed — {rec.data.get('gate_reason', 'gated')}"

    if _notify(_ResultTarget(cfg), body, log=log.warn, title=title,
               tags=tags, priority=priority):
        log.info("  → notified" + (" with link" if executed and link else ""))


class _ResultTarget:
    """Lets notify() post to the result topic without mutating cfg."""

    def __init__(self, cfg):
        self.notify_url = cfg.result_notify_url
        self.alarm_throttle_hours = getattr(cfg, "alarm_throttle_hours", 6)


# ---------------------------------------------------------------------------
#  stages
# ---------------------------------------------------------------------------

def stage_transcribe(rec, cfg, log):
    log.info(f"  transcribe: {rec.audio.name}")
    # A recording left running is normal operator error, and only its opening
    # seconds can contain a command — the wake phrase has to come first. So an
    # over-long recording is truncated, never rejected: rejecting would discard
    # a real instruction silently.
    with stt.bounded_audio(rec.audio, cfg, rec.data.get("duration_seconds"),
                           log=log.info) as (upload, trunc):
        text = stt.transcribe(upload, cfg)
    write_atomic(rec.transcript_path(cfg.vault), text + "\n")
    words = len(text.split())
    log.info(f"    {words} words: {text[:90]}{'…' if len(text) > 90 else ''}")
    rec.advance(TRANSCRIBED, word_count=words, **trunc,
                transcript_path=str(rec.transcript_path(cfg.vault).relative_to(cfg.vault)))
    if trunc:
        notify(cfg, f"Truncated a {trunc['truncated_from_seconds']:.0f}s recording "
                    f"to its first {trunc['transcribed_seconds']}s — "
                    f"was the device left running?\n\n{text[:150]}", log)


def stage_route(rec, cfg, log):
    text = rec.transcript_path(cfg.vault).read_text().strip()
    ok, reason = stt.sanity_check(text, cfg)

    # The strict gate failed. Before filing this as a note — which silently
    # discards a real command when the wake word was merely misheard — ask
    # whether the first word was PHONETICALLY a mishearing. Only reachable
    # after an exact-match failure, so this can widen the gate, never narrow it.
    if not ok and "no wake phrase" in reason:
        heard = wake.first_token(text)
        recovered, why = wake.adjudicate(
            heard, cfg, log=log.info,
            following=" ".join(text.split()[1:1 + wake.CONTEXT_WORDS]))
        log.info(f"    wake check: {why}")
        rec.data["wake_heard"] = heard
        rec.data["wake_adjudicated"] = recovered
        if recovered:
            log.warn(f"    RECOVERED: {heard!r} judged a mishearing of "
                     f"{cfg.wake_phrase!r} — executing")
            # The transcript on disk is NEVER rewritten. It records what was
            # actually heard, which is the evidence; the substitution is
            # metadata, so a fuzzy admission stays visible in git history.
            ok, reason = True, f"wake phrase recovered from {heard!r}"
            rec.data["wake_recovery_reason"] = why

    if not ok:
        # Not an error — a deliberate refusal to act on doubtful input.
        log.warn(f"    gate: {reason}")
        note = rec.outdir(cfg.vault) / "note.md"
        write_atomic(note, f"# Unexecuted recording\n\n**Reason:** {reason}\n\n"
                           f"## Transcript\n\n{text}\n")
        rec.advance(PUBLISHED, executed=False, gate_reason=reason,
                    wake_heard=rec.data.get("wake_heard"),
                    wake_adjudicated=rec.data.get("wake_adjudicated"))
        return False

    # When the wake word was recovered, strip_wake_phrase cannot find it — it is
    # looking for "atticus" in a transcript that says "Artemis". Hand it the word
    # that was actually heard so the instruction starts in the right place.
    if rec.data.get("wake_adjudicated"):
        cfg = _with_wake(cfg, rec.data.get("wake_heard", ""))
    instruction, clip = stt.extract_command(text, cfg)
    if clip:
        log.warn(f"    command bounded: {clip['transcript_chars']} chars of "
                 f"transcript → {clip['command_chars']} chars of prompt")
    task = ex.build_task(instruction)
    write_atomic(rec.task_path(cfg.vault), task)
    rec.advance(ROUTED, **clip,
                task_path=str(rec.task_path(cfg.vault).relative_to(cfg.vault)))
    return True


def _with_wake(cfg, heard: str):
    """A shallow view of cfg whose wake phrase is the word actually heard."""
    import copy
    c = copy.copy(cfg)
    c.wake_phrase = heard.lower()
    return c


def stage_execute(rec, cfg, log, dry_run=False):
    task = rec.task_path(cfg.vault).read_text()
    outdir = rec.outdir(cfg.vault)
    if dry_run:
        log.info("    [dry-run] skipping agent")
        rec.advance(EXECUTED, dry_run=True)
        return
    res = ex.run(task, outdir, cfg, log=log.info)
    log.info(f"    produced {res['files']} file(s), {res['bytes']:,} bytes")
    rec.advance(EXECUTED, output_files=res["files"], output_bytes=res["bytes"],
                budget_usd=res.get("budget_usd"))


def process(rec, cfg, git, log, dry_run=False) -> bool:
    """Drive one record as far as it will go. True if it ends published."""
    log.info(f"▶ {rec.stem}  [{rec.status}]")
    try:
        if rec.status == RAW:
            stage_transcribe(rec, cfg, log)
            git.commit_push(f"transcribe {rec.stem}")

        if rec.status == TRANSCRIBED:
            if not stage_route(rec, cfg, log):
                git.commit_push(f"note (ungated) {rec.stem}")
                notify_result(cfg, rec, log)
                return True
            git.commit_push(f"route {rec.stem}")

        if rec.status == ROUTED:
            stage_execute(rec, cfg, log, dry_run)

        if rec.status == EXECUTED:
            # Explicit, rather than letting absence imply it. The gated path
            # writes executed=False, so "no key" used to mean "executed" — a
            # default that is easy to read the wrong way round.
            rec.advance(PUBLISHED, executed=True)
            git.commit_push(f"publish {rec.stem}")
            log.info(f"  ✓ published → {rec.outdir(cfg.vault).relative_to(cfg.vault)}")
            notify_result(cfg, rec, log)
            return True

    except VaultSyncError:
        # Deliberately NOT caught as a record failure. The transcript or artifact
        # is correct; what failed is durability. Marking the record failed would
        # quarantine good work and hide a vault problem behind a per-record
        # error. Let it abort the pass so the exit code and the alarm are about
        # the real fault.
        raise
    except (stt.TranscriptionError, ex.ExecutionError) as e:
        kind = getattr(e, "kind", "execution")
        state = rec.fail(cfg.vault, rec.status, str(e), getattr(e, "retryable", False))
        if state == RETRY_WAIT:
            log.warn(f"  ↻ {kind}: {e}")
            log.warn(f"    attempt {rec.data['attempts']} — retrying after "
                     f"{rec.data['next_attempt_at']}")
            return False
        log.error(f"  ✗ {kind}: {e}")
        git.commit_push(f"fail {rec.stem} ({kind})")
        notify(cfg, f"Atticus {kind} failure on {rec.stem}: {e}", log)
    except Exception as e:
        rec.fail(cfg.vault, rec.status, f"{type(e).__name__}: {e}", False)
        log.error(f"  ✗ unexpected: {type(e).__name__}: {e}")
        log.debug(traceback.format_exc())
        git.commit_push(f"fail {rec.stem} (unexpected)")
        notify(cfg, f"Atticus unexpected failure on {rec.stem}: {e}", log)
    return False


# ---------------------------------------------------------------------------

def cmd_status(cfg, log):
    bad = []
    recs = load_records(cfg.vault, on_bad=lambda p, e: bad.append((p, e)))
    if bad:
        print(f"  ** {len(bad)} MALFORMED record(s) — not processable **")
        for p, e in bad:
            print(f"     {p.name}: {e}")
    if not recs:
        print(f"vault {cfg.vault}: empty")
        return 0
    counts = {}
    for r in recs:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"vault {cfg.vault}  —  {len(recs)} record(s)")
    for s in (RAW, TRANSCRIBED, ROUTED, EXECUTED, PUBLISHED, RETRY_WAIT, FAILED):
        if counts.get(s):
            print(f"  {s:<12} {counts[s]}")
    pending = [r for r in recs if r.status not in (PUBLISHED, FAILED)]
    for r in pending:
        when = r.data.get("next_attempt_at")
        extra = f"  retry at {when}" if when else ""
        print(f"    · {r.stem}  [{r.status}]{extra}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", metavar="ID", help="process a single recording by id or stem")
    ap.add_argument("--retry", metavar="ID", help="re-arm one failed record and run it now")
    ap.add_argument("--retry-all", action="store_true",
                    help="re-arm every failed and waiting record")
    ap.add_argument("--status", action="store_true", help="show the queue and exit")
    ap.add_argument("--dry-run", action="store_true", help="skip the agent call")
    ap.add_argument("--env", type=Path, help="alternate ops/.env")
    ap.add_argument("--no-pull", action="store_true", help="skip git pull")
    args = ap.parse_args()

    try:
        cfg = Config(args.env)
    except Exception as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    log = Log(cfg.log_level)
    if not cfg.vault.is_dir():
        print(f"vault not found: {cfg.vault}", file=sys.stderr)
        return 2

    if args.status:
        return cmd_status(cfg, log)

    log.debug(f"config: {json.dumps(cfg.redacted(), indent=2)}")
    git = Git(cfg.vault, cfg.git_name, cfg.git_email, cfg.push_retries,
              log=log.warn)

    if not args.no_pull:
        git.pull()

    bad = []

    def on_bad(path, err):
        # Loud, quarantined, and fatal to the exit code. A record we cannot
        # parse is exactly the case S5 exists to prevent.
        log.error(f"  ✗ MALFORMED RECORD {path.name}: {err}")
        bad.append((path, err))
        try:
            write_atomic(cfg.vault / "failures" / f"{path.stem}.malformed.json",
                         json.dumps({"path": str(path), "error": str(err),
                                     "at": __import__("vault").utcnow()}, indent=2) + "\n")
        except OSError:
            pass

    records = load_records(cfg.vault, on_bad=on_bad)
    if args.retry_all:
        for r in records:
            if r.status in (FAILED, RETRY_WAIT):
                log.info(f"re-arming {r.stem} (was {r.status})")
                r.rearm()
        records = load_records(cfg.vault, on_bad=on_bad)

    todo = [r for r in records
            if r.status not in (PUBLISHED, FAILED) and r.due()]
    waiting = [r for r in records if r.status == RETRY_WAIT and not r.due()]
    if waiting:
        log.info(f"{len(waiting)} record(s) waiting to retry; soonest "
                 f"{min(r.data.get('next_attempt_at', '') for r in waiting)}")
    if bad:
        notify(cfg, f"Atticus: {len(bad)} unreadable recording metadata file(s) "
                    f"quarantined — they are NOT being processed.\n\n"
                    + "\n".join(str(p.name) for p, _ in bad[:5]), log)
    if args.once or args.retry:
        want = args.once or args.retry
        todo = [r for r in load_records(cfg.vault, on_bad=on_bad)
                if want in (r.id, r.stem)]
        if args.retry:
            for r in todo:
                log.info(f"re-arming {r.stem} (was {r.status})")
                r.rearm()
            todo = [r for r in load_records(cfg.vault, on_bad=on_bad)
                    if want in (r.id, r.stem)]
        if not todo:
            print(f"no record matching {(args.once or args.retry)!r}", file=sys.stderr)
            return 2

    if not todo:
        log.info("nothing to do")
        return 0

    log.info(f"{len(todo)} record(s) to process")
    failed = 0
    for r in todo:
        try:
            if not process(r, cfg, git, log, args.dry_run):
                failed += 1
        except VaultSyncError as e:
            log.error(f"VAULT SYNC FAILED — stopping the pass: {e}")
            notify(cfg, f"Atticus cannot reach the vault remote. Work is "
                        f"committed locally and invisible downstream.\n\n{e}", log)
            return 3
    if failed:
        log.warn(f"{failed} record(s) failed")
    return 1 if (failed or bad) else 0


def _guarded():
    try:
        with single_instance("processor"):
            return main()
    except AlreadyRunning as e:
        print(f"skipped: {e}", file=sys.stderr)
        return 0          # not a failure — the other pass is doing the work


if __name__ == "__main__":
    sys.exit(_guarded())
