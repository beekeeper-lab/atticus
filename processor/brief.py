"""The daily AI briefing.

A scheduled sibling of the recording pipeline: same sandbox, same budget ceiling,
same usage ledger, same vault — but triggered by a clock rather than by something
you said. `ops/atticus-brief.timer` fires it at 07:00.

    python3 processor/brief.py                # today's briefing
    python3 processor/brief.py --dry-run      # build the prompt, run nothing
    python3 processor/brief.py --date 2026-08-01 --force

**Output goes to `reports/`, not `processed/`.** The vault browser already has a
first-class path for a document that is not a recording (`collect_reports`, an
HTML file plus `meta.json` carrying title/date/tags/summary), so this needs no
synthetic record and tells no lies about audio that never existed. It inherits
site indexing, tag filtering, read state, delete, and the PDF button for free.

**Deduplication is the whole feature, and it works by prompt injection.** The
agent runs sandboxed with no vault access, so it cannot read a ledger of what
previous briefings covered. The driver reads it here and writes it *into* the
task, and the agent hands back `covered.json` which the driver appends. That
round trip is why a briefing on day 30 is still worth opening.
"""
import argparse
import json
import os
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import execute as ex          # noqa: E402
import podcast as pod        # noqa: E402
import usage                  # noqa: E402
from config import Config     # noqa: E402
from notify import ResultTarget, notify   # noqa: E402
from vault import OWNED_BRIEF, Git   # noqa: E402

SLUG = "ai-brief"
LEDGER = ".state/brief-covered.jsonl"
TAG = "AI brief"

# How much history the agent is shown. Long enough that a story recurring after a
# week is recognised as an update; short enough that the prompt does not become
# the briefing. 14 days of items is a few hundred lines at most.
LOOKBACK_DAYS = 14

PREAMBLE = """# Daily AI briefing — {today}

Write today's AI briefing. Follow the **`ai-brief`** skill for method, source
discipline, and the output contract; everything below is today's specific input.

The window is the **last 24 hours** ({since} to {today}). Something older than
that is only news here if it genuinely surfaced or changed in the window, and you
should say which.

## Output contract

- Write exactly two files into `./output/` — the directory already exists and
  `$ATTICUS_OUTPUT_DIR` holds its absolute path.
- `index.html` — one self-contained HTML file, phone-first. Follow the
  `html-artifact-output` skill.
- `covered.json` — the machine-readable list of what you covered, in the shape the
  `ai-brief` skill specifies. Write `[]` on a quiet day. **Never omit it**: it is
  what stops tomorrow repeating today.
- Nothing else. Do not write scratch files, Markdown drafts, or notes.
- Do not run git. The pipeline commits your output.
- You have web access. Use it — a briefing written from memory is worthless, and
  your training data ends well before today.
{audio}
## Already covered — this is not news

{covered}
"""

NOTHING_COVERED = ("Nothing yet; this is the first briefing. Everything you find "
                   "is new.")

AUDIO_ASK = """- An audio version is wanted. Also write `podcast-script.md`, following the
  `ai-brief` skill's audio section and the `podcast-companion` format. Skip it
  entirely on a quiet day — nobody wants five minutes of "not much happened".
"""


def ledger_path(vault: Path) -> Path:
    return vault / LEDGER


def load_covered(vault: Path, *, days: int = LOOKBACK_DAYS,
                 today: date | None = None) -> list[dict]:
    """Items covered in the last `days` briefings, oldest first.

    Tolerates a torn line the same way the usage ledger does: this is an
    append-only log and a crash mid-write is a normal thing to read.
    """
    p = ledger_path(vault)
    if not p.is_file():
        return []
    cutoff = (today or datetime.now(UTC).date()) - timedelta(days=days)
    out = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict) or not d.get("key"):
            continue
        try:
            when = date.fromisoformat(str(d.get("date", ""))[:10])
        except ValueError:
            continue
        if when >= cutoff:
            out.append(d)
    return out


def format_covered(items: list[dict]) -> str:
    """The already-covered block, newest first so the most likely repeats lead.

    Deliberately compact and deliberately NOT the full briefing text — the agent
    needs enough to recognise a story, not enough to rewrite it.
    """
    if not items:
        return NOTHING_COVERED
    lines = []
    for d in sorted(items, key=lambda x: str(x.get("date", "")), reverse=True):
        kind = "update" if d.get("kind") == "update" else "new"
        title = str(d.get("title", "")).replace("\n", " ")[:140]
        lines.append(f"- `{d['key']}` ({d.get('date', '?')}, {kind}) — {title}")
    return ("These stories have appeared in previous briefings. Do not present any "
            "of them as new. Mention one only if it genuinely developed in the "
            "window, and then reuse its key with `\"kind\": \"update\"`.\n\n"
            + "\n".join(lines))


def build_task(covered: list[dict], today: date, audio: bool = False) -> str:
    return PREAMBLE.format(
        today=today.isoformat(),
        since=(today - timedelta(days=1)).isoformat(),
        covered=format_covered(covered),
        audio=AUDIO_ASK if audio else "",
    )


def read_covered_output(outdir: Path, today: date, log=print) -> list[dict]:
    """Parse and normalise the agent's covered.json.

    A malformed file is a warning, not a failure: the briefing itself is the
    deliverable and it is already written. But it IS loud, because a missing or
    broken ledger update means tomorrow silently repeats today — the one failure
    mode that makes the whole feature worthless while looking fine.
    """
    p = outdir / "covered.json"
    if not p.is_file():
        log("    ⚠ no covered.json — tomorrow's briefing may repeat today's items")
        return []
    try:
        raw = json.loads(p.read_text(errors="replace"))
    except json.JSONDecodeError as e:
        log(f"    ⚠ covered.json is not valid JSON ({e}) — dedup will not update")
        return []
    if not isinstance(raw, list):
        log("    ⚠ covered.json is not a list — dedup will not update")
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip().lower()
        if not key:
            continue
        out.append({
            "date": today.isoformat(),
            "key": key,
            "title": str(item.get("title") or "")[:200],
            "url": str(item.get("url") or "")[:500],
            "source": str(item.get("source") or "")[:120],
            "kind": "update" if item.get("kind") == "update" else "new",
        })
    return out


def append_covered(vault: Path, items: list[dict]) -> int:
    if not items:
        return 0
    p = ledger_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        for d in items:
            f.write(json.dumps(d, sort_keys=True) + "\n")
    return len(items)


def summarise(items: list[dict]) -> str:
    """meta.json's summary — what the browser shows on the card."""
    if not items:
        return "Quiet day — nothing substantial to report."
    new = [d for d in items if d["kind"] == "new"]
    upd = [d for d in items if d["kind"] == "update"]
    bits = []
    if new:
        bits.append(", ".join(d["title"] for d in new[:3]))
    parts = []
    if new:
        parts.append(f"{len(new)} new")
    if upd:
        parts.append(f"{len(upd)} update{'s' if len(upd) != 1 else ''}")
    head = " · ".join(parts)
    return f"{head}. {' · '.join(bits)}"[:600] if bits else f"{head}."


def write_meta(dest: Path, today: date, items: list[dict], extra_tags=()) -> None:
    tags = [TAG] + [t for t in extra_tags if t and t != TAG]
    (dest / "meta.json").write_text(json.dumps({
        "title": f"AI briefing — {today.isoformat()}",
        "date": today.isoformat(),
        "tags": tags,
        "summary": summarise(items),
    }, indent=2) + "\n")


def run(cfg, *, today: date | None = None, dry_run: bool = False,
        force: bool = False, log=print) -> dict:
    today = today or datetime.now(UTC).date()
    slug = f"{SLUG}-{today.isoformat()}"
    dest = cfg.vault / "reports" / slug

    if dest.is_dir() and (dest / "index.html").is_file() and not force:
        log(f"  {slug} already exists — nothing to do (use --force to rebuild)")
        return {"made": False, "reason": "already exists", "slug": slug}

    covered = load_covered(cfg.vault, today=today)
    want_audio = bool(getattr(cfg, 'brief_audio', False))
    task = build_task(covered, today, audio=want_audio)
    log(f"  briefing {today.isoformat()} — {len(covered)} prior item(s) in context")

    if dry_run:
        log("  [dry-run] task prompt follows\n")
        log(task)
        return {"made": False, "reason": "dry run", "slug": slug, "task": task}

    # Same real-money guard the audio stage respects. The agent itself is
    # subscription-billed, but a month that has blown its API budget is a month
    # with a problem, and spending more of it unattended is the wrong instinct.
    state = usage.budget_state(cfg.vault, cfg)
    if state.get("exhausted"):
        log(f"  ✗ monthly API budget exhausted (${state.get('spent', 0):.2f}) — skipping")
        return {"made": False, "reason": "monthly API budget exhausted", "slug": slug}

    # Stage into a scratch directory, not into reports/. A half-written briefing
    # in the vault is published by the next site build, and a partial AI briefing
    # is worse than none — it reads as a complete quiet day.
    staging = cfg.vault / "reports" / f".{slug}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        res = ex.run(task, staging, cfg, log=log)
    except ex.ExecutionError as e:
        shutil.rmtree(staging, ignore_errors=True)
        log(f"  ✗ briefing failed: {e}")
        return {"made": False, "reason": str(e), "slug": slug}

    au = res.get("usage") or {}
    if au:
        log(f"    agent: {au.get('turns', 0)} turn(s), "
            f"~${au.get('usd', 0):.4f} imputed (subscription, not billed)")
    usage.record(cfg.vault, kind="agent", billing=usage.SUBSCRIPTION,
                 stem=slug, log=log, **au)

    if not (staging / "index.html").is_file():
        # The agent produced something, but not the file the contract names, so
        # there is nothing to publish. Keep the scratch output for diagnosis
        # rather than deleting the evidence.
        keep = cfg.vault / "reports" / f".{slug}.failed"
        shutil.rmtree(keep, ignore_errors=True)
        staging.rename(keep)
        log(f"  ✗ no index.html produced ({res['files']} file(s)) — kept at {keep.name}")
        return {"made": False, "reason": "agent wrote no index.html", "slug": slug}

    items = read_covered_output(staging, today, log=log)
    write_meta(staging, today, items, extra_tags=getattr(cfg, "brief_tags", ()))

    # Voice it while still in staging, so the audio and the player land in the
    # same atomic move as the briefing. Best-effort for the same reason as the
    # recording pipeline: the written briefing is the deliverable and a TTS
    # outage must not cost it.
    audio = {}
    if want_audio:
        try:
            audio = pod.generate(staging, cfg, log=log)
        except Exception as e:                        # noqa: BLE001
            log(f"    ! audio failed unexpectedly: {type(e).__name__}: {e}")
            audio = {"made": False, "reason": f"unexpected {type(e).__name__}"}
        if audio.get("made"):
            log(f"    ♪ audio: {audio['seconds']:.0f}s, "
                f"{audio['bytes']:,} bytes, ${audio['usd']:.4f}")
            usage.record(cfg.vault, kind="tts", billing=usage.API, stem=slug,
                         model=getattr(cfg, "gemini_tts_model", ""),
                         usd=audio["usd"], log=log,
                         audio_seconds=audio["seconds"], turns=audio.get("turns"),
                         characters=audio.get("chars"))
        elif not str(audio.get("reason", "")).startswith("no script"):
            log(f"    ! no audio: {audio.get('reason')}")

    shutil.rmtree(dest, ignore_errors=True)
    staging.rename(dest)
    n = append_covered(cfg.vault, items)

    quiet = not items
    log(f"  ✓ {slug}: {len(items)} item(s) covered"
        + (" — quiet day" if quiet else "") + f", {res['bytes']:,} bytes")

    git = Git(cfg.vault, cfg.git_name, cfg.git_email, cfg.push_retries, log=log,
              paths=OWNED_BRIEF)
    git.commit_push(f"ai-brief {today.isoformat()} ({n} item(s))")
    pushed = _notify(cfg, today, items, slug, log=log)
    log("    → notified with link" if pushed else "    ! NOT notified")
    return {"made": True, "slug": slug, "items": len(items), "quiet": quiet,
            "bytes": res["bytes"], "usd": au.get("usd", 0), "notified": pushed,
            "audio": audio.get("made", False), "audio_usd": audio.get("usd", 0)}


def _notify(cfg, today: date, items: list[dict], slug: str, log=print) -> bool:
    """Push it, because a 7am briefing nobody is told about is a file on a disk.

    Aimed at the RESULT topic rather than the alarm topic — a briefing is an
    outcome, and filing outcomes as alarms trains the alarm channel to be
    ignorable. On a host that has not set ATTICUS_RESULT_NOTIFY_URL these are the
    same topic; that is config.py's documented fallback.

    **Returns whether it actually sent, and the caller logs that.** The first
    version discarded the result and logged nothing, so a briefing whose push
    failed — bad url, network down, ntfy unreachable — reported complete success
    and the operator simply never heard about that morning. Silent delivery
    failure on the one output that is supposed to reach a phone is the exact shape
    of failure this project treats as the worst kind.
    """
    if not getattr(cfg, "result_notify_url", None):
        log("    ! no ATTICUS_RESULT_NOTIFY_URL — the briefing alerts nobody")
        return False
    body = summarise(items)
    if cfg.site_base_url:
        body += f"\n\n{cfg.site_base_url}/docs/{slug}/index.html"
    return bool(notify(ResultTarget(cfg), body, log=log,
                       title=f"AI briefing — {today.isoformat()}",
                       tags="newspaper", priority="default"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="build and print the prompt; run no agent, write nothing")
    ap.add_argument("--date", help="build for this date (YYYY-MM-DD) instead of today")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if the briefing already exists")
    args = ap.parse_args()

    cfg = Config()
    when = date.fromisoformat(args.date) if args.date else None
    res = run(cfg, today=when, dry_run=args.dry_run, force=args.force)
    return 0 if (res.get("made") or res.get("reason") in
                 ("already exists", "dry run")) else 1


if __name__ == "__main__":
    sys.exit(main())
