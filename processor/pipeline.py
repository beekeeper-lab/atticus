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
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config                                    # noqa: E402
from lock import AlreadyRunning, single_instance             # noqa: E402
import execute as ex                                         # noqa: E402
import audio_on_demand                                       # noqa: E402
import handlers            # noqa: E402,F401  (registers outbox handlers)
import approval_drain                                       # noqa: E402
import outbox                                                # noqa: E402
import podcast as pod                                        # noqa: E402
import projects                                              # noqa: E402
import transcribe as stt                                     # noqa: E402
import usage                                                 # noqa: E402
import wake                                                  # noqa: E402
from notify import (                                          # noqa: E402
    ROUTINE as _ROUTINE, ResultTarget, alarm as _alarm,
    clear as _notify_clear, notify as _notify,
)
from vault import (                                          # noqa: E402
    EXECUTED, EXECUTING, FAILED, OWNED_PROCESSOR, PUBLISHED, RAW, RETRY_WAIT,
    TERMINAL,
    ROUTED, TRANSCRIBED, Git, VaultSyncError, load_records, utcnow, write_atomic,
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


def notify(cfg, text, log, **kw):
    # Forward key= (and any other _notify kwarg) so a recurring condition can be
    # throttled to one alarm per window instead of firing every 5-minute pass.
    #
    # setdefault, not a literal: the title used to be hardcoded here, so any
    # caller passing its own title= raised "got multiple values for keyword
    # argument" — swallowed by the caller's own try/except and surfacing only as a
    # mysteriously undelivered alarm.
    kw.setdefault("title", "Atticus processor")
    return _notify(cfg, text, log=log.warn, **kw)


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

    # ROUTINE (#91): a finished report is good news. Inside quiet hours it is
    # parked for the morning brief rather than buzzing at 3am — never dropped.
    if _alarm(ResultTarget(cfg), body, severity=_ROUTINE, log=log.warn,
              title=title, tags=tags, priority=priority)["ntfy"]:
        log.info("  → notified" + (" with link" if executed and link else ""))


# ---------------------------------------------------------------------------
#  stages
# ---------------------------------------------------------------------------

def _alarm_budget_thresholds(cfg, log):
    """Announce each budget threshold the month's api spend has just passed.

    Called right after transcription is recorded, because transcription is where
    api spend actually moves — the adjudicator adds fractions of a cent, and its
    contribution is picked up by the next recording's check.

    Never lets an accounting problem fail a recording that already succeeded: the
    transcript is written and the money is spent either way.
    """
    try:
        for category in ("transcription", "tts"):
            _alarm_one_budget(cfg, log, category)
    except Exception as e:                          # noqa: BLE001
        log.warn(f"    ! budget alert check failed: {type(e).__name__}: {e}")


def _alarm_one_budget(cfg, log, category):
    try:
        crossed = usage.newly_crossed(cfg.vault, cfg, category)
        if not crossed:
            return
        state = usage.budget_state(cfg.vault, cfg, category)
        label = "transcription" if category == "transcription" else "TTS"
        for t in crossed:
            final = state["enabled"] and t >= state["budget_usd"]
            if final and category == "transcription":
                body = (f"Atticus has spent ${state['spent_usd']:.4f} of its "
                        f"${state['budget_usd']:.2f} transcription budget for "
                        f"{state['month']}.\n\n"
                        f"TRANSCRIPTION IS NOW STOPPED. Recordings keep arriving in "
                        f"the vault and will be processed once the budget is raised "
                        f"({state['env']}) or the month rolls over. Nothing is "
                        f"lost.\n\nAt about $0.003 a recording this should not "
                        f"happen in normal use — check for a runaway loop before "
                        f"raising it.")
                title, tags, priority = ("Atticus — transcription budget SPENT",
                                         "rotating_light", "high")
            elif final:
                body = (f"Atticus has spent ${state['spent_usd']:.2f} of its "
                        f"${state['budget_usd']:.2f} TTS budget for "
                        f"{state['month']}.\n\n"
                        f"AUDIO IS NOW SKIPPED. Everything else continues normally "
                        f"— recordings are still transcribed, the agent still runs, "
                        f"reports are still published. They just will not get an "
                        f"episode attached until {state['env']} is raised or the "
                        f"month rolls over.")
                title, tags, priority = ("Atticus — TTS budget spent",
                                         "mute", "default")
            else:
                remaining = state.get("remaining_usd")
                tail = (f" ${remaining:.2f} left of ${state['budget_usd']:.2f}."
                        if remaining is not None else "")
                body = (f"Atticus {label} spend for {state['month']} has passed "
                        f"${t:.2f} (now ${state['spent_usd']:.4f}).{tail}\n\n"
                        f"Real money, {label} only. The agent runs on your Claude "
                        f"subscription and is not counted in any money budget. Run "
                        f"`atticus usage` for the breakdown.")
                title, tags, priority = (f"Atticus — {label} budget warning",
                                         "warning", "default")
            log.warn(f"  ! {label} spend passed ${t:.2f} "
                     f"(${state['spent_usd']:.4f} of ${state['budget_usd']:.2f})")
            notify(cfg, body, log,
                   key=f"budget-{category}-{state['month']}-{t:.2f}",
                   title=title, tags=tags, priority=priority)
            usage.mark_alerted(cfg.vault, t, state["spent_usd"],
                               log=log.warn, category=category)
    except Exception as e:                          # noqa: BLE001
        log.warn(f"  ! budget threshold check failed: {type(e).__name__}: {e}")


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "on", "true", "yes")


def stage_transcribe(rec, cfg, log):
    log.info(f"  transcribe: {rec.audio.name}")

    # The budget gate. Checked HERE, before the only paid call in the pipeline,
    # and non-retryable: a month's budget does not refill on a 5-minute backoff,
    # so retrying would just re-check the same wall three times. Audio is already
    # durable in the vault, so nothing is lost — the recording waits for a human
    # to raise the ceiling or for the month to roll over.
    state = usage.budget_state(cfg.vault, cfg, "transcription")
    if state["exhausted"]:
        raise stt.TranscriptionError(
            f"the ${state['budget_usd']:.2f} transcription budget for "
            f"{state['month']} is spent (${state['spent_usd']:.4f}). Transcription "
            f"is STOPPED so it cannot keep charging. At roughly $0.003 a recording "
            f"this should not happen in normal use — check for a runaway loop "
            f"before raising {state['env']}.",
            retryable=False, kind="quota")

    # Two genuinely different jobs, and the record decides which one this is.
    #
    # A COMMAND is truncated: the wake phrase must come first, so everything
    # past the opening seconds is silence or ambient conversation. Transcribing
    # all of it would be wasteful and a privacy problem.
    #
    # A DOCUMENT — a meeting, a long dictation — is chunked, because the whole
    # recording is the point. Opt in globally with ATTICUS_CHUNK_LONG_AUDIO, or
    # per recording with "chunk_audio": true in its metadata.
    chunk_this = bool(rec.data.get("chunk_audio",
                                   getattr(cfg, "chunk_long_audio", False)))
    duration = rec.data.get("duration_seconds")
    if not isinstance(duration, (int, float)):
        duration = stt.probe_seconds(rec.audio)

    if chunk_this and not duration:
        # The operator explicitly asked for the document path. Silently falling
        # through to the command path would blind-cut a 40-minute meeting to its
        # first 180s and report success, which is the opposite of what was asked.
        raise stt.TranscriptionError(
            "chunking requested but the duration is unknown (metadata missing "
            "and ffprobe failed) — refusing to truncate a document",
            retryable=False)

    if chunk_this and duration > getattr(cfg, "max_command_seconds", 180):
        text, trunc = stt.transcribe_long(rec.audio, cfg, duration, log=log.info)
    else:
        with stt.bounded_audio(rec.audio, cfg, rec.data.get("duration_seconds"),
                               log=log.info) as (upload, trunc):
            text = stt.transcribe(upload, cfg)

        # MEETING MODE (#86, ADR-008). The trigger can only be found by
        # transcribing, and chunking has to be decided before transcribing —
        # so the cheap bounded pass runs first and, if it opened with "meeting
        # mode", the whole recording is transcribed properly. The first 180s
        # are paid for twice, which is about $0.018: cheaper than any scheme
        # that avoids it, and far cheaper than getting it wrong.
        if (stt.is_meeting(text, cfg) and duration
                and duration > getattr(cfg, "max_command_seconds", 180)):
            log.info(f"    meeting mode: re-transcribing all "
                     f"{duration:.0f}s (ADR-008)")
            text, trunc = stt.transcribe_long(rec.audio, cfg, duration,
                                              log=log.info)
            rec.data["meeting"] = True
        elif stt.is_meeting(text, cfg):
            # Short enough that the bounded pass already covered it.
            rec.data["meeting"] = True
    # Real money, so record it before anything else can fail. `transcribed` is
    # what we actually sent to the API — the truncated length, not the
    # recording's full length, which is the whole point of truncating.
    transcribed = float(trunc.get("transcribed_seconds")
                        or (duration if isinstance(duration, (int, float)) else 0))
    usage.record(cfg.vault, kind="transcription", billing=usage.API,
                 stem=rec.stem, model=cfg.stt_model,
                 usd=usage.transcription_usd(transcribed, cfg.stt_model),
                 audio_seconds=round(transcribed, 1),
                 chunks=trunc.get("chunks"), log=log.warn)

    _alarm_budget_thresholds(cfg, log)

    write_atomic(rec.transcript_path(cfg.vault), text + "\n")

    # ADR-008 §2: MEETING AUDIO IS NEVER COMMITTED. Deleted here, the moment the
    # transcript is durable — not expired after 30 days like the operator's own
    # audio, because `ops/retention.py` removes audio from the WORKING TREE and
    # git history keeps it forever. That story is already weaker than it sounds
    # for the operator's own voice; for a third party who never agreed to any of
    # this it would be indefensible: a permanent, searchable recording of
    # someone else's speech in a repository they cannot see and cannot ask to be
    # removed from without a history rewrite the README forbids.
    #
    # So meeting audio never enters the retention system at all. The transcript
    # stays — that is the deliberate line, and the reason the feature exists.
    if rec.data.get("meeting") and not _truthy(getattr(cfg, "meeting_keep_audio", False)):
        try:
            size = rec.audio.stat().st_size
            rec.audio.unlink()
            rec.data["meeting_audio_deleted"] = True
            log.info(f"    meeting: deleted {size:,} bytes of audio before commit "
                     f"(ADR-008 — the transcript is the record)")
        except OSError as e:
            # Loud, because the whole condition of the feature is that this
            # works. A meeting whose audio survived must be visible, not quiet.
            log.warn(f"    ! MEETING AUDIO NOT DELETED: {type(e).__name__}: {e}")
            rec.data["meeting_audio_deleted"] = False

    words = len(text.split())
    log.info(f"    {words} words: {text[:90]}{'…' if len(text) > 90 else ''}")
    rec.advance(TRANSCRIBED, word_count=words, **trunc,
                transcript_path=str(rec.transcript_path(cfg.vault).relative_to(cfg.vault)))
    if trunc.get("truncated_from_seconds"):
        # This alarm used to append text[:150] unconditionally — on the one code
        # path that is ambient-speech-heavy by definition, while every other
        # notification honours the privacy setting. Same rule here now.
        detail = getattr(cfg, "notification_detail", "full")
        excerpt = "" if detail == "title" else f"\n\n{text[:60 if detail == 'summary' else 150]}"
        notify(cfg, f"Truncated a {trunc['truncated_from_seconds']:.0f}s recording "
                    f"to its first {trunc['transcribed_seconds']}s — "
                    f"was the device left running?{excerpt}", log)
    elif trunc.get("chunks"):
        log.info(f"    joined {trunc['chunks']} chunk(s) → {words} words")


def stage_route(rec, cfg, log):
    text = rec.transcript_path(cfg.vault).read_text().strip()
    ok, reason = stt.sanity_check(text, cfg)

    # The strict gate failed. Before filing this as a note — which silently
    # discards a real command when the wake word was merely misheard — ask
    # whether the first word was PHONETICALLY a mishearing. Only reachable
    # after an exact-match failure, so this can widen the gate, never narrow it.
    if not ok and "no wake phrase" in reason:
        # Same filler-stripped view sanity_check matched against, so the word
        # judged is the word that failed the gate — not "Okay".
        opening = stt.leading_words(text, 1 + wake.CONTEXT_WORDS)
        heard = wake.first_token(" ".join(opening))
        recovered, why = wake.adjudicate(
            heard, cfg, log=log.info,
            following=" ".join(opening[1:]))
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
        # Reaching a gated note is also a terminal success — a record that failed
        # transcription, was retried, and then gated should not keep an error file.
        rec.clear_error(cfg.vault)
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
    # Does this recording belong to a named project (#84)? Resolved from the
    # FULL transcript rather than the bounded instruction, because "add this to
    # the consulting project" often names the project in the trailing half that
    # extract_command() clips away.
    ctx, proj = "", None
    try:
        proj = projects.resolve_from_text(cfg.vault, text)
    except projects.ProjectError as e:
        # Two projects named in one sentence. Refusing to assume is right, and
        # the run still happens — it simply gets no context.
        log.warn(f"    project: {e}")
    if proj:
        ctx = projects.context_block(
            proj, cap=int(getattr(cfg, "project_context_chars", 2000) or 2000))
        log.info(f"    project: {proj['name']} "
                 f"({len(ctx)} chars of context, {len(proj['artifacts'])} artifact(s))")

    task = ex.build_task(instruction, project_context=ctx)
    write_atomic(rec.task_path(cfg.vault), task)
    rec.advance(ROUTED, **clip,
                project=(proj["slug"] if proj else None),
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

    # SUBSCRIPTION, not api. `claude -p` authenticates with the operator's OAuth
    # credential, so this consumes rate-limit quota and bills nothing per token.
    # The CLI's total_cost_usd is an imputed figure for efficiency comparison —
    # recording it as money would resurrect exactly the mistake this split fixes.
    au = res.get("usage") or {}
    if au:
        log.info(f"    agent: {au.get('input_tokens', 0):,} in / "
                 f"{au.get('output_tokens', 0):,} out tokens, "
                 f"{au.get('cache_read_tokens', 0):,} cached, "
                 f"{au.get('turns', 0)} turn(s), "
                 f"~${au.get('usd', 0):.4f} imputed (subscription, not billed)")
    usage.record(cfg.vault, kind="agent", billing=usage.SUBSCRIPTION,
                 stem=rec.stem, log=log.warn, **au)
    rec.advance(EXECUTED, output_files=res["files"], output_bytes=res["bytes"],
                budget_usd=res.get("budget_usd"))


def stage_podcast(rec, cfg, log, dry_run=False):
    """Voice output/podcast-script.md, if the agent wrote one.

    Swallows everything. The only outcome that reaches the record is metadata
    saying what happened, because there is no failure here worth withholding a
    finished report over.
    """
    outdir = rec.outdir(cfg.vault)
    if pod.find_script(outdir) is None:
        return                                # audio was not asked for
    if dry_run:
        log.info("    [dry-run] skipping audio")
        return

    # TTS has its OWN budget, and exhausting it costs only the audio. The
    # transcript, the agent run and the published report have all already
    # happened; the report simply does not get an episode attached. Checked here
    # rather than inside podcast.py so that module stays free of budget policy.
    state = usage.budget_state(cfg.vault, cfg, "tts")
    if state["exhausted"]:
        log.warn(f"    audio skipped — the ${state['budget_usd']:.2f} TTS budget "
                 f"for {state['month']} is spent (${state['spent_usd']:.4f}). The "
                 f"report is published without an episode; raise {state['env']} "
                 f"to resume.")
        rec.data["podcast"] = {
            "made": False,
            "reason": f"TTS budget exhausted (${state['spent_usd']:.4f} of "
                      f"${state['budget_usd']:.2f})"}
        return

    try:
        res = pod.generate(outdir, cfg, log=log.info)
    except Exception as e:                    # noqa: BLE001 — see docstring
        # A bug in this module must not cost the report. Name it loudly instead.
        log.warn(f"    podcast failed unexpectedly: {type(e).__name__}: {e}")
        rec.data["podcast"] = {"made": False,
                               "reason": f"unexpected {type(e).__name__}: {e}"}
        return

    if res.get("made"):
        log.info(f"    ♪ audio: {res['audio']} ({res['bytes']:,} bytes, "
                 f"~{res['seconds']:.0f}s) → player in {res['report']}")
        # seconds/usd here are MEASURED off the finished file, not the pre-flight
        # estimate — see podcast.generate(). estimated_usd is kept alongside so
        # drift between the two stays visible in the ledger.
        usage.record(cfg.vault, kind="tts", billing=usage.API, stem=rec.stem,
                     model=cfg.tts_model, usd=res["usd"], log=log.warn,
                     audio_seconds=res["seconds"], turns=res["turns"],
                     characters=res["chars"], estimated_usd=res["estimated_usd"])
    elif res.get("reason", "").startswith("no script"):
        pass                                  # not requested; nothing to report
    else:
        log.warn(f"    podcast not made: {res.get('reason')}")
    rec.data["podcast"] = res


def _execution_is_live(rec, cfg, log) -> bool:
    """Is some process still working on this EXECUTING record?

    EXECUTING alone cannot distinguish "abandoned mid-run" from "in progress
    right now", and treating the second as the first is destructive: on
    2026-07-30 two timer passes walked into a record a manual pass was actively
    executing and failed it, writing a spurious failures/ entry for a run that
    then completed and published normally.

    The owning pass stamps host, pid and time when it enters EXECUTING. A run is
    live if that stamp is this host, the pid still exists, and it has not been
    going longer than the agent timeout allows. Anything else is abandoned.

    Deliberately conservative about the cross-host case: a stamp from ANOTHER
    host is treated as live until the timeout lapses, because declaring a remote
    peer's live run dead would double-execute it.
    """
    owner = rec.data.get("executing_by") or {}
    started = owner.get("at")
    if not started:
        return False                    # pre-stamp record, or none written
    try:
        age = (datetime.now(UTC)
               - datetime.fromisoformat(str(started).replace("Z", "+00:00"))
               ).total_seconds()
    except ValueError:
        return False
    # Generous margin over the agent timeout: the pipeline still has to collect
    # and commit after the agent returns.
    if age > getattr(cfg, "exec_timeout", 1800) + 600:
        log.warn(f"    execution stamp is {age / 60:.0f} min old — treating the "
                 f"run as abandoned")
        return False
    host = owner.get("host")
    if host and host != os.uname().nodename:
        return True                     # another host's run; let its timeout rule
    pid = owner.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)                 # signal 0 only tests existence
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                     # exists, owned by someone else
    return True


def _primary_output(rec, cfg):
    """The deliverable a project should hold: the same choice the site build
    makes — index.html if present, else the largest HTML file."""
    outdir = rec.outdir(cfg.vault)
    htmls = sorted(outdir.glob("*.html")) if outdir.is_dir() else []
    if not htmls:
        return None
    for h in htmls:
        if h.name == "index.html":
            return h
    return max(htmls, key=lambda p: p.stat().st_size)


def _doc_title(path) -> str:
    import re as _re
    try:
        head = path.read_text(errors="replace")[:4000]
    except OSError:
        return ""
    m = _re.search(r"(?is)<title[^>]*>(.*?)</title>", head)
    return _re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def process(rec, cfg, git, log, dry_run=False) -> bool:
    """Drive one record as far as it will go. True if it ends published."""
    log.info(f"▶ {rec.stem}  [{rec.status}]")
    # A due RETRY_WAIT record matches none of the stage branches below, so
    # without this it would fall straight through every pass and never retry —
    # rearm() only ran on a manual --retry. Re-arm it here so a retryable
    # failure actually gets its second attempt: rearm() restores failed_stage,
    # putting the record back at the stage that failed so it re-executes.
    if rec.status == RETRY_WAIT and rec.due():
        log.info(f"  ↻ retry due — re-arming to {rec.data.get('failed_stage') or RAW}")
        rec.rearm()
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

        if rec.status == EXECUTING and not _execution_is_live(rec, cfg, log):
            # A previous pass died mid-agent-run. The agent may already have had
            # side effects — it has Bash and network, and a skill that sends or
            # files something would have done so — so re-running is not free and
            # is NOT the safe default. Fail loudly, non-retryable, and let a
            # human decide with `--retry` once they know what the run did.
            raise ex.ExecutionError(
                "interrupted mid-execution (crash, reboot or kill during the "
                "agent run). Not auto-retried: the agent may have completed side "
                "effects. Inspect the run, then re-arm with --retry.",
                retryable=False)
        if rec.status == EXECUTING:
            log.info("  … another pass is executing this record; leaving it be")
            return False

        if rec.status == ROUTED:
            # Committed BEFORE the agent starts, so an interrupted run is
            # distinguishable from one that never began. The stamp is what lets a
            # later pass tell "abandoned" from "still running" — see
            # _execution_is_live().
            rec.advance(EXECUTING, executing_by={
                "host": os.uname().nodename,
                "pid": os.getpid(),
                "at": utcnow(),
            })
            git.commit_push(f"executing {rec.stem}")
            stage_execute(rec, cfg, log, dry_run)

        if rec.status == EXECUTED:
            # Best-effort, and deliberately BEFORE publish so the audio and the
            # player land in the same commit as the report. Cannot fail the
            # record: the HTML is the deliverable and audio is a companion, so a
            # TTS outage must not quarantine good research.
            # Outbox BEFORE publish, so intent, receipt and deliverable land in
            # one commit. Never fails the record: the report is what the operator
            # reads, and a refused or failed action is reported in it rather than
            # costing it.
            if not dry_run:
                try:
                    ob = outbox.process(
                        rec.outdir(cfg.vault), cfg, log=log.info, stem=rec.stem,
                        max_actions=(int(getattr(cfg, "meeting_max_actions", 20))
                                     if rec.data.get("meeting") else None))
                    if ob["requests"]:
                        rec.data["outbox"] = {k: v for k, v in ob.items()
                                              if k != "receipts"}
                except Exception as e:              # noqa: BLE001
                    log.warn(f"    ! outbox failed: {type(e).__name__}: {e}")
            # Link the deliverable into its project as the next version (#88).
            # AFTER the outbox, so a `revises:` the agent declared is visible,
            # and before publish so the copy lands in the same commit. A
            # recording stays immutable — the project is where versions
            # accumulate, because "revise that report" is a statement about an
            # artifact, not about a thing that was said at a time.
            if not dry_run and rec.data.get("project"):
                try:
                    doc = _primary_output(rec, cfg)
                    if doc:
                        linked = projects.link_artifact(
                            cfg.vault, rec.data["project"], source=doc,
                            title=_doc_title(doc) or rec.stem, stem=rec.stem,
                            revises=str(rec.data.get("revises") or ""))
                        rec.data["project_artifact"] = linked
                        log.info(f"    project: linked as {linked['artifact']} "
                                 f"v{linked['version']}")
                except projects.ProjectError as e:
                    log.warn(f"    ! project link refused: {e}")
                except Exception as e:              # noqa: BLE001
                    # Never costs the record: the deliverable is already in
                    # processed/ and that is the durable copy.
                    log.warn(f"    ! project link failed: {type(e).__name__}: {e}")

            stage_podcast(rec, cfg, log, dry_run)
            # Explicit, rather than letting absence imply it. The gated path
            # writes executed=False, so "no key" used to mean "executed" — a
            # default that is easy to read the wrong way round.
            rec.advance(PUBLISHED, executed=True)
            # A record that failed and then succeeded kept its failures/ entry
            # forever, so the doctor and the failures/ count overreported
            # permanently. Clearing it here — at the one transition that means
            # "this worked" — is the only place it is unambiguously correct.
            if rec.clear_error(cfg.vault):
                log.info("    cleared a stale failures/ entry")
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
        # A failed run still SPENT. A run killed at the spend ceiling consumed the
        # whole ceiling and produced nothing, and until this existed the ledger
        # recorded $0.00 for it — so the most expensive events in the system were
        # the invisible ones, and the cost page understated exactly where it
        # mattered most. Record before failing the record, so the accounting
        # survives whatever the retry logic decides.
        spent = getattr(e, "usage", None) or {}
        if spent:
            usage.record(cfg.vault, kind="agent", billing=usage.SUBSCRIPTION,
                         stem=rec.stem, log=log.warn, failed=True, **spent)
        state = rec.fail(cfg.vault, rec.status, str(e), getattr(e, "retryable", False))
        if state == RETRY_WAIT:
            log.warn(f"  ↻ {kind}: {e}")
            log.warn(f"    attempt {rec.data['attempts']} — retrying after "
                     f"{rec.data['next_attempt_at']}")
            # Commit the RETRY_WAIT transition. Without this the backoff state
            # lived only in the local working tree: a later pass on another host
            # (or after a pull) would not see next_attempt_at and could retry
            # early or lose the attempt count. Git is the queue, so the wait has
            # to be in it.
            git.commit_push(f"retry-wait {rec.stem} (attempt {rec.data['attempts']})")
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


def drain_approvals(cfg, git, log) -> dict:
    """Collect decisions, perform what was approved, and COMMIT the result.

    The commit is the part worth naming. `approval_drain.run()` writes the
    approvals ledger, and a performed action may write the vault itself —
    `image.generate` puts a PNG beside the report it illustrates, and that file
    is the deliverable, not a side effect.

    Nothing else in the pass will necessarily commit it. The caller's very next
    branch is `if not todo: return 0`, and the comment above the call says why
    that is the *common* case: an approval is usually tapped when no new
    recording has arrived. Without a commit here the work sits in the working
    tree, unpushed and unpublished, until some later pass with an unrelated
    reason to commit sweeps it up — and in the meantime the other host's
    `pull --rebase` has a dirty tree to contend with.

    Never raises: an approval that cannot be drained must not cost the pass the
    records it was going to process.
    """
    try:
        res = approval_drain.run(cfg, log=log.info)
    except Exception as e:                          # noqa: BLE001
        log.warn(f"approval drain failed: {type(e).__name__}: {e}")
        return {"decided": 0, "performed": 0, "expired": 0, "failed": 0}
    if any(res.values()):
        log.info(f"approvals: {res['decided']} decided, "
                 f"{res['performed']} performed, {res['expired']} expired"
                 + (f", {res['failed']} failed" if res.get("failed") else ""))
        try:
            git.commit_push(f"approvals: {res['performed']} performed, "
                            f"{res['decided']} decided")
        except VaultSyncError:
            # Same contract as every other commit in this file: a vault that
            # cannot be reached is fatal to the pass, not silently swallowed.
            raise
        except Exception as e:                      # noqa: BLE001
            log.warn(f"could not commit approvals: {type(e).__name__}: {e}")
    return res


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
    for s in (RAW, TRANSCRIBED, ROUTED, EXECUTING, EXECUTED, PUBLISHED,
              RETRY_WAIT, FAILED):
        if counts.get(s):
            print(f"  {s:<12} {counts[s]}")
    # TERMINAL covers published, cancelled and superseded (#82): an
    # operator who said stop must not see the work resume next tick.
    pending = [r for r in recs if r.status not in (*TERMINAL, FAILED)]
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

    if not cfg.wake_phrase:
        # The gate is the only thing between ambient speech and an autonomous
        # agent, and an empty phrase makes sanity_check pass EVERYTHING. The
        # config dump below is DEBUG-only, so a deployment configured purely by
        # environment — or with a hand-written ops/.env missing the line — used
        # to execute every recording with nothing in the journal saying so.
        log.warn("ATTICUS_WAKE_PHRASE is empty — the wake gate is OFF and every "
                 "transcribed recording will be executed")

    log.debug(f"config: {json.dumps(cfg.redacted(), indent=2)}")
    git = Git(cfg.vault, cfg.git_name, cfg.git_email, cfg.push_retries,
              log=log.warn, paths=OWNED_PROCESSOR)

    if not args.no_pull:
        # A silent failure is the worst failure. An unreachable remote makes
        # pull() return False; ignoring it means the pass runs on a stale tree,
        # exits 0, and looks exactly like a quiet day — while work piles up
        # invisibly. Mirror the VaultSyncError handling below: alarm (throttled)
        # and exit 3.
        if not git.pull():
            # Do NOT assert a cause. This said "the vault remote is unreachable"
            # unconditionally, and the real error in production was
            # "fatal: Cannot rebase onto multiple branches" — a local git state
            # problem caused by ingest and the processor fetching at the same
            # instant, with a perfectly healthy remote. Naming the wrong cause
            # sends the operator to check the network for a concurrency bug.
            why = git.last_error or "(no output from git)"
            log.error(f"git pull failed: {why}")
            notify(cfg, "Atticus cannot pull the vault. The processor is running "
                        "on a stale tree and cannot see new recordings.\n\n"
                        f"git said: {why}",
                   log, key="pull")
            return 3
        # Clear on recovery, or a pull that breaks again inside the throttle
        # window opened by an earlier one stays silent for hours.
        _notify_clear("pull")

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
            if r.status not in (*TERMINAL, FAILED) and r.due()]
    waiting = [r for r in records if r.status == RETRY_WAIT and not r.due()]
    if waiting:
        log.info(f"{len(waiting)} record(s) waiting to retry; soonest "
                 f"{min(r.data.get('next_attempt_at', '') for r in waiting)}")
    if bad:
        # Throttled: a malformed file stays malformed, so an unkeyed alarm fired
        # every 5-minute pass and trained the operator to ignore it.
        notify(cfg, f"Atticus: {len(bad)} unreadable recording metadata file(s) "
                    f"quarantined — they are NOT being processed.\n\n"
                    + "\n".join(str(p.name) for p, _ in bad[:5]), log, key="malformed")
    else:
        # No bad records this pass — clear the throttle so a NEW malformed file
        # alarms immediately rather than waiting out a window opened earlier.
        _notify_clear("malformed")
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

    # On-demand audio requested from the vault browser's "+ Audio" button. Run
    # BEFORE the "nothing to do" exit, or a pass with no new recordings — which is
    # most passes — would never fulfil one. Skipped entirely for --once/--retry,
    # which target a named record.
    if not (args.once or args.retry) and not args.dry_run:
        try:
            aod = audio_on_demand.run(cfg, log=log.info)
            if aod["done"] or aod["failed"]:
                git.commit_push(f"on-demand audio: {aod['done']} done, "
                                f"{aod['failed']} failed")
        except VaultSyncError:
            raise
        except Exception as e:                      # noqa: BLE001
            log.warn(f"audio request queue failed: {type(e).__name__}: {e}")

    # Approvals run on EVERY pass, before the "nothing to do" exit (#83). A
    # decision tapped on a phone while the queue was empty must still be
    # collected and performed — the common case is precisely that no new
    # recording has arrived since the action was held.
    if not args.dry_run and getattr(cfg, "approvals_enabled", False):
        drain_approvals(cfg, git, log)

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
    # Resolve the vault BEFORE locking, so the lock can live in it. The lock has
    # to cover a manual pass racing a timed one, and only a vault-relative path
    # is visible identically to both — see lock.py. A config error here is not
    # fatal to locking; main() reports it properly a moment later.
    vault = None
    try:
        vault = Config().vault
    except Exception as e:                          # noqa: BLE001
        # Not fatal to locking: fall back to the runtime-dir lock and let main()
        # report the config problem properly a moment later.
        print(f"lock: cannot resolve the vault ({type(e).__name__}); "
              f"using a fallback lock location", file=sys.stderr)
    try:
        with single_instance("processor", vault=vault):
            return main()
    except AlreadyRunning as e:
        print(f"skipped: {e}", file=sys.stderr)
        return 0          # not a failure — the other pass is doing the work


if __name__ == "__main__":
    sys.exit(_guarded())
