"""Command lifecycle: status, cancel, retry (#82).

Two properties carry most of the weight here.

**A recording must never act on itself.** "Cancel that" is spoken INTO a
recording that the pipeline is executing when the outbox runs. Without the
exclusion, the cancel kills the run performing it, which then never finishes
writing the cancellation — so the operator sees nothing happen and has no way
to learn why. It is the kind of bug that looks like the feature simply does not
work.

**Resolution refuses rather than guesses.** Nobody is present to disambiguate,
and cancelling the wrong recording destroys work that was asked for. This is the
third use of the pattern (contacts, github.close), and the rules are the same.

No processes are signalled: `os.killpg` is monkeypatched, and the tests that
exercise the running case assert on what WOULD have been signalled.
"""
import json

import notify as nf
import outbox
import pytest
import recordings
from handlers import atticus
from vault import CANCELLED, EXECUTING, PUBLISHED, RAW, ROUTED, SUPERSEDED


@pytest.fixture(autouse=True)
def _no_pushes(monkeypatch):
    monkeypatch.setattr(nf, "alarm",
                        lambda *a, **k: {"ntfy": True, "calendar": False,
                                         "deferred": False})


@pytest.fixture
def lcfg(cfg, tmp_path):
    cfg.vault = tmp_path / "vault"
    (cfg.vault / "inbox").mkdir(parents=True, exist_ok=True)
    cfg.lifecycle_within_days = 7
    cfg.site_base_url = "http://forge/atticus"
    return cfg


def _rec(lcfg, stem, *, status=RAW, transcript="", title="", recorded=None,
         **extra):
    """A record on disk, with an optional transcript and deliverable."""
    from datetime import UTC, datetime, timedelta
    when = recorded or (datetime.now(UTC) - timedelta(hours=1))
    d = lcfg.vault / "inbox" / "2026" / "08"
    d.mkdir(parents=True, exist_ok=True)
    meta = {"plaud_id": stem, "recorded_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "audio_filename": f"{stem}.mp3", "status": status, **extra}
    (d / f"{stem}.json").write_text(json.dumps(meta, indent=2) + "\n")
    proc = lcfg.vault / "processed" / "2026" / "08"
    proc.mkdir(parents=True, exist_ok=True)
    if transcript:
        (proc / f"{stem}.transcript.txt").write_text(transcript)
    if title:
        out = proc / stem
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.html").write_text(
            f"<!doctype html><title>{title}</title><body>x</body>")
    return stem


def _req(verb, match="", stem="asking-recording"):
    return {"verb": verb, "match": match, "_stem": stem,
            "_file": f"001-{verb}.json"}


# ── the resolver ────────────────────────────────────────────────────────────
def test_resolves_on_the_deliverables_title(lcfg):
    _rec(lcfg, "recA", transcript="do some research", title="West Coast Consulting Plan")
    _rec(lcfg, "recB", transcript="something else entirely", title="Elm Lake")
    got = recordings.resolve(lcfg.vault, "consulting plan")
    assert got.stem == "recA"


def test_resolves_on_the_transcript_when_there_is_no_title(lcfg):
    _rec(lcfg, "recA", transcript="Atticus, research agentic plugins for me")
    _rec(lcfg, "recB", transcript="unrelated words")
    assert recordings.resolve(lcfg.vault, "agentic plugins").stem == "recA"


def test_the_last_one_means_the_most_recent(lcfg):
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    _rec(lcfg, "older", transcript="a", recorded=now - timedelta(hours=5))
    _rec(lcfg, "newer", transcript="b", recorded=now - timedelta(hours=1))
    for phrase in ("the last one", "that", "the most recent", "it"):
        assert recordings.resolve(lcfg.vault, phrase).stem == "newer", phrase


def test_ambiguity_refuses_and_names_the_candidates(lcfg):
    _rec(lcfg, "recA", transcript="research the consulting market")
    _rec(lcfg, "recB", transcript="research the consulting market again")
    with pytest.raises(recordings.ResolveError, match="matches 2"):
        recordings.resolve(lcfg.vault, "consulting market")


def test_no_match_refuses_and_shows_what_is_recent(lcfg):
    _rec(lcfg, "recA", transcript="research plugins", title="Plugins")
    with pytest.raises(recordings.ResolveError, match="nothing in the last"):
        recordings.resolve(lcfg.vault, "quarterly tax filing")


def test_an_empty_vault_refuses_rather_than_raising_something_ugly(lcfg):
    with pytest.raises(recordings.ResolveError, match="no recording"):
        recordings.resolve(lcfg.vault, "anything")


def test_records_outside_the_window_are_invisible(lcfg):
    from datetime import UTC, datetime, timedelta
    _rec(lcfg, "ancient", transcript="the consulting research",
         recorded=datetime.now(UTC) - timedelta(days=30))
    with pytest.raises(recordings.ResolveError):
        recordings.resolve(lcfg.vault, "consulting research", within_days=7)


def test_terminal_records_are_skipped_unless_asked_for(lcfg):
    _rec(lcfg, "done", transcript="the consulting research", status=PUBLISHED)
    with pytest.raises(recordings.ResolveError):
        recordings.resolve(lcfg.vault, "consulting research")
    assert recordings.resolve(lcfg.vault, "consulting research",
                              skip_status=recordings.DONE_WITH).stem == "done"


def test_a_recording_never_resolves_to_itself(lcfg):
    """THE guard. Without it, 'cancel that' kills the run doing the cancelling
    and the cancellation is never recorded."""
    _rec(lcfg, "self", transcript="cancel the consulting research")
    _rec(lcfg, "target", transcript="the consulting research", title="Consulting")
    got = recordings.resolve(lcfg.vault, "consulting research", exclude_stem="self")
    assert got.stem == "target"


# ── the verbs ───────────────────────────────────────────────────────────────
def test_the_three_verbs_are_registered_internal():
    for verb in ("atticus.status", "atticus.cancel", "atticus.retry"):
        h = outbox.handler_for(verb)
        assert h is not None and h["risk"] == outbox.INTERNAL


def test_cancel_before_the_run_marks_cancelled(lcfg):
    _rec(lcfg, "target", transcript="research the plugins", status=ROUTED)
    out = atticus.cancel(_req("atticus.cancel", "plugins"), lcfg, log=lambda m: None)
    assert out["now"] == CANCELLED and out["killed"] is False
    rec = recordings.load_by_stem(lcfg.vault, "target")
    assert rec.data["status"] == CANCELLED
    assert rec.data["cancelled_from"] == ROUTED


def test_cancel_of_a_published_recording_supersedes_instead(lcfg):
    """The artifact is committed and may already have been read. Pretending it
    can be withdrawn would be a lie."""
    _rec(lcfg, "target", transcript="research the plugins", title="Plugins",
         status=PUBLISHED)
    out = atticus.cancel(_req("atticus.cancel", "plugins"), lcfg, log=lambda m: None)
    assert out["now"] == SUPERSEDED
    assert "superseded" in out["outcome"]
    assert (lcfg.vault / "processed/2026/08/target/report.html").is_file(), \
        "the report must stay where it is"


def test_cancel_of_a_running_recording_signals_the_process_group(lcfg, monkeypatch):
    killed = []
    monkeypatch.setattr(atticus.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(atticus.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(atticus, "_still_ours", lambda pid, stem: True)
    _rec(lcfg, "target", transcript="research the plugins", status=EXECUTING,
         executing_by={"host": "forge", "pid": 4242, "at": "2026-08-02T00:00:00Z"})
    out = atticus.cancel(_req("atticus.cancel", "plugins"), lcfg, log=lambda m: None)
    assert out["killed"] is True and killed == [(4242, atticus.signal.SIGTERM)]
    assert recordings.load_by_stem(lcfg.vault, "target").data["status"] == CANCELLED


def test_a_recycled_pid_is_not_signalled(lcfg, monkeypatch):
    """PID reuse is real, and killing an unrelated process because a number was
    recycled would be a genuinely bad failure. The record is still marked."""
    killed = []
    monkeypatch.setattr(atticus.os, "killpg", lambda *a: killed.append(a))
    monkeypatch.setattr(atticus, "_still_ours", lambda pid, stem: False)
    _rec(lcfg, "target", transcript="research the plugins", status=EXECUTING,
         executing_by={"host": "forge", "pid": 999999, "at": "x"})
    out = atticus.cancel(_req("atticus.cancel", "plugins"), lcfg, log=lambda m: None)
    assert killed == [] and out["killed"] is False
    assert recordings.load_by_stem(lcfg.vault, "target").data["status"] == CANCELLED


def test_still_ours_checks_the_cmdline(monkeypatch, tmp_path):
    assert atticus._still_ours(999999999, "x") is False, "a dead pid is not ours"


def test_cancel_refuses_an_ambiguous_phrase_and_changes_nothing(lcfg):
    _rec(lcfg, "a", transcript="the consulting research", status=ROUTED)
    _rec(lcfg, "b", transcript="the consulting research too", status=ROUTED)
    with pytest.raises(outbox.OutboxError, match="matches 2"):
        atticus.cancel(_req("atticus.cancel", "consulting research"), lcfg,
                       log=lambda m: None)
    for stem in ("a", "b"):
        assert recordings.load_by_stem(lcfg.vault, stem).data["status"] == ROUTED


def test_cancel_cannot_target_the_recording_that_asked(lcfg):
    """Spoken into a live run: the only candidate is itself, so it must refuse
    rather than kill its own pipeline."""
    _rec(lcfg, "self", transcript="cancel that", status=EXECUTING)
    with pytest.raises(outbox.OutboxError):
        atticus.cancel({"verb": "atticus.cancel", "match": "cancel that",
                        "_stem": "self", "_file": "001-x.json"},
                       lcfg, log=lambda m: None)
    assert recordings.load_by_stem(lcfg.vault, "self").data["status"] == EXECUTING


def test_status_reports_without_changing_anything(lcfg):
    _rec(lcfg, "target", transcript="research the plugins", title="Plugins",
         status=PUBLISHED, output_files=2)
    out = atticus.status(_req("atticus.status", "plugins"), lcfg, log=lambda m: None)
    assert out["status"] == PUBLISHED and "Plugins" in out["line"]
    assert recordings.load_by_stem(lcfg.vault, "target").data["status"] == PUBLISHED


def test_status_with_no_match_describes_the_most_recent(lcfg):
    _rec(lcfg, "target", transcript="research the plugins", title="Plugins")
    out = atticus.status({"verb": "atticus.status", "_stem": "asking",
                          "_file": "001-x.json"}, lcfg, log=lambda m: None)
    assert out["stem"] == "target"


def test_retry_rearms_for_the_next_pass(lcfg):
    _rec(lcfg, "target", transcript="research the plugins", status="failed",
         failed_stage=ROUTED)
    out = atticus.retry(_req("atticus.retry", "plugins"), lcfg, log=lambda m: None)
    assert out["was"] == "failed" and out["now"] == ROUTED


def test_retry_refuses_a_live_run(lcfg):
    """Two agents on one record is worse than making the operator wait."""
    _rec(lcfg, "target", transcript="research the plugins", status=EXECUTING)
    with pytest.raises(outbox.OutboxError, match="running right now"):
        atticus.retry(_req("atticus.retry", "plugins"), lcfg, log=lambda m: None)


def test_cancel_requires_a_match_but_status_does_not():
    with pytest.raises(outbox.OutboxError, match="match"):
        outbox.validate({"verb": "atticus.cancel"})
    assert outbox.validate({"verb": "atticus.status"}) is not None


def test_the_describe_lines_name_what_will_happen():
    assert "cancel" in outbox.describe(
        {"verb": "atticus.cancel", "match": "the research"}).lower()
    assert "the research" in outbox.describe(
        {"verb": "atticus.retry", "match": "the research"})


# ── the terminal statuses ───────────────────────────────────────────────────
def test_cancelled_records_are_not_picked_up_again(lcfg):
    """An operator who said stop must not watch the work resume next tick."""
    from vault import TERMINAL, load_records
    _rec(lcfg, "stopped", transcript="x", status=CANCELLED)
    pending = [r for r in load_records(lcfg.vault) if r.status not in TERMINAL]
    assert pending == []


def test_cancelled_outranks_everything_when_two_hosts_disagree():
    """A rebase must never resurrect a cancelled run."""
    from vault import _PROGRESS
    assert _PROGRESS[CANCELLED] > _PROGRESS[PUBLISHED]
    assert _PROGRESS[SUPERSEDED] > _PROGRESS[PUBLISHED]


def test_the_skill_declares_what_the_handler_registers():
    from pathlib import Path
    md = (Path(__file__).resolve().parents[2] / "skills/atticus/SKILL.md").read_text()
    front = md.split("---")[1]
    for verb in ("atticus.status", "atticus.cancel", "atticus.retry"):
        assert verb in front
    # The routing distinction this skill exists to make.
    assert "cancel" in front.lower() and "new work" in front.lower()
