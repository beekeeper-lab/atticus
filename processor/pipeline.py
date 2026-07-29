#!/usr/bin/env python3
"""Atticus processor — the Forge half.

    git pull → scan inbox → transcribe → route → execute → commit

Each stage advances the record's status and commits, so a crash resumes
rather than redoing work, and a failure in one stage never costs the others.

    pipeline.py                run one pass over the vault
    pipeline.py --once ID      process a single recording
    pipeline.py --status       show the queue, change nothing
    pipeline.py --dry-run      everything except the agent call

Exit: 0 clean · 1 some records failed · 2 usage/config error
"""
import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config                                    # noqa: E402
import execute as ex                                         # noqa: E402
import transcribe as stt                                     # noqa: E402
from notify import notify as _notify                          # noqa: E402
from vault import (                                          # noqa: E402
    EXECUTED, FAILED, PUBLISHED, RAW, ROUTED, TRANSCRIBED,
    Git, load_records, write_atomic,
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
    said = (said[:180] + "…") if len(said) > 180 else said

    if executed:
        doc = primary_doc(rec.outdir(cfg.vault))
        link = doc_url(cfg, rec, doc)
        title, tags, priority = "Atticus finished", "white_check_mark", "default"
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
    text = stt.transcribe(rec.audio, cfg)
    write_atomic(rec.transcript_path(cfg.vault), text + "\n")
    words = len(text.split())
    log.info(f"    {words} words: {text[:90]}{'…' if len(text) > 90 else ''}")
    rec.advance(TRANSCRIBED, word_count=words,
                transcript_path=str(rec.transcript_path(cfg.vault).relative_to(cfg.vault)))


def stage_route(rec, cfg, log):
    text = rec.transcript_path(cfg.vault).read_text().strip()
    ok, reason = stt.sanity_check(text, cfg)
    if not ok:
        # Not an error — a deliberate refusal to act on doubtful input.
        log.warn(f"    gate: {reason}")
        note = rec.outdir(cfg.vault) / "note.md"
        write_atomic(note, f"# Unexecuted recording\n\n**Reason:** {reason}\n\n"
                           f"## Transcript\n\n{text}\n")
        rec.advance(PUBLISHED, executed=False, gate_reason=reason)
        return False

    instruction = stt.strip_wake_phrase(text, cfg)
    task = ex.build_task(instruction, str(rec.outdir(cfg.vault)))
    write_atomic(rec.task_path(cfg.vault), task)
    rec.advance(ROUTED, task_path=str(rec.task_path(cfg.vault).relative_to(cfg.vault)))
    return True


def stage_execute(rec, cfg, log, dry_run=False):
    task = rec.task_path(cfg.vault).read_text()
    outdir = rec.outdir(cfg.vault)
    if dry_run:
        log.info("    [dry-run] skipping agent")
        rec.advance(EXECUTED, dry_run=True)
        return
    res = ex.run(task, outdir, cfg, log=log.info)
    log.info(f"    produced {res['files']} file(s), {res['bytes']:,} bytes")
    rec.advance(EXECUTED, output_files=res["files"], output_bytes=res["bytes"])


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

    except (stt.TranscriptionError, ex.ExecutionError) as e:
        kind = getattr(e, "kind", "execution")
        rec.fail(cfg.vault, rec.status, str(e), getattr(e, "retryable", False))
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
    recs = load_records(cfg.vault)
    if not recs:
        print(f"vault {cfg.vault}: empty")
        return 0
    counts = {}
    for r in recs:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"vault {cfg.vault}  —  {len(recs)} record(s)")
    for s in (RAW, TRANSCRIBED, ROUTED, EXECUTED, PUBLISHED, FAILED):
        if counts.get(s):
            print(f"  {s:<12} {counts[s]}")
    pending = [r for r in recs if r.status not in (PUBLISHED, FAILED)]
    for r in pending:
        print(f"    · {r.stem}  [{r.status}]")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", metavar="ID", help="process a single recording by id or stem")
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

    todo = [r for r in load_records(cfg.vault)
            if r.status not in (PUBLISHED, FAILED)]
    if args.once:
        todo = [r for r in load_records(cfg.vault)
                if args.once in (r.id, r.stem)]
        if not todo:
            print(f"no record matching {args.once!r}", file=sys.stderr)
            return 2

    if not todo:
        log.info("nothing to do")
        return 0

    log.info(f"{len(todo)} record(s) to process")
    failed = sum(0 if process(r, cfg, git, log, args.dry_run) else 1 for r in todo)
    if failed:
        log.warn(f"{failed} record(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
