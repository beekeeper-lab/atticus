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
                return True
            git.commit_push(f"route {rec.stem}")

        if rec.status == ROUTED:
            stage_execute(rec, cfg, log, dry_run)

        if rec.status == EXECUTED:
            rec.advance(PUBLISHED)
            git.commit_push(f"publish {rec.stem}")
            log.info(f"  ✓ published → {rec.outdir(cfg.vault).relative_to(cfg.vault)}")
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
