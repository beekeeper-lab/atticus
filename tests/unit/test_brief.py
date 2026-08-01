"""The daily AI briefing.

The feature that actually matters here is deduplication, and it works by prompt
injection: the agent is sandboxed away from the vault, so brief.py reads the
covered-items ledger, writes it INTO the task, and appends whatever comes back in
covered.json. If either half of that round trip breaks, briefings silently start
repeating themselves — which looks fine and is worthless. Most of these tests are
about that loop.

No agent runs. `execute.run` is monkeypatched.
"""
import json
from datetime import date

import brief
import usage
import pytest

TODAY = date(2026, 8, 15)


def _ledger(cfg, rows):
    p = brief.ledger_path(cfg.vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _fake_agent(cfg, *, index="<html><body><h1>Brief</h1></body></html>",
                covered='[{"key":"k1","title":"A thing","kind":"new"}]',
                monkeypatch=None):
    """Stand in for execute.run: write the two contract files into the outdir."""
    def run(task, outdir, c, log=print):
        outdir.mkdir(parents=True, exist_ok=True)
        if index is not None:
            (outdir / "index.html").write_text(index)
        if covered is not None:
            (outdir / "covered.json").write_text(covered)
        run.task = task
        return {"files": 2, "bytes": 42, "usage": {"turns": 9, "usd": 1.5}}
    monkeypatch.setattr(brief.ex, "run", run)
    monkeypatch.setattr(brief, "Git", _SpyGit)
    monkeypatch.setattr(brief, "_notify", lambda *a, **k: None)
    return run


_git_calls = []


def _SpyGit(*args, **kwargs):
    """A stand-in that VALIDATES the call against the real Git signature.

    The first version of this was `lambda *a, **k: ...`, which swallowed
    everything — so `Git(cfg.vault, cfg, log=log)` passed every test and then
    raised TypeError on the first live run, after the briefing had already been
    written. A permissive double tests nothing about the call it replaces.
    """
    import inspect

    from vault import Git as RealGit
    inspect.signature(RealGit.__init__).bind(object(), *args, **kwargs)
    _git_calls.append((args, kwargs))
    return _NoGit()


class _NoGit:
    def commit_push(self, msg):
        self.msg = msg
        _git_calls.append(("commit_push", msg))
        return True


# ── the ledger, and the window ─────────────────────────────────────────────
def test_covered_items_inside_the_window_are_loaded(cfg):
    _ledger(cfg, [
        {"date": "2026-08-14", "key": "recent", "title": "Yesterday"},
        {"date": "2026-08-01", "key": "old", "title": "Two weeks back"},
        {"date": "2026-07-01", "key": "ancient", "title": "Long gone"},
    ])
    keys = {d["key"] for d in brief.load_covered(cfg.vault, today=TODAY)}
    assert "recent" in keys
    assert "ancient" not in keys, "outside the lookback window"


def test_a_torn_ledger_line_does_not_break_the_briefing(cfg):
    """Append-only log written by a live pipeline: a half-written final line is a
    normal thing to read, not a reason to skip a morning."""
    p = brief.ledger_path(cfg.vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": "2026-08-14", "key": "good"}) + "\n{\"date\"")
    got = brief.load_covered(cfg.vault, today=TODAY)
    assert [d["key"] for d in got] == ["good"]


def test_entries_with_no_key_or_a_bad_date_are_skipped(cfg):
    _ledger(cfg, [
        {"date": "2026-08-14", "title": "no key at all"},
        {"date": "not-a-date", "key": "bad-date"},
        {"date": "2026-08-14", "key": "fine"},
    ])
    assert [d["key"] for d in brief.load_covered(cfg.vault, today=TODAY)] == ["fine"]


# ── the injected prompt ────────────────────────────────────────────────────
def test_the_prompt_carries_prior_items_and_forbids_repeating_them():
    task = brief.build_task(
        [{"date": "2026-08-14", "key": "gpt5-pricing", "title": "Price cut",
          "kind": "new"}], TODAY)
    assert "gpt5-pricing" in task
    assert "Price cut" in task
    assert "not news" in task.lower() or "do not present" in task.lower()
    assert "2026-08-15" in task and "2026-08-14" in task, "window must be explicit"
    assert "covered.json" in task, "the contract must be stated in the prompt"


def test_the_first_ever_briefing_says_so_rather_than_showing_an_empty_list():
    """An empty 'already covered' block reads as a bug. Say it is day one."""
    task = brief.build_task([], TODAY)
    assert brief.NOTHING_COVERED in task


def test_prior_items_are_listed_newest_first():
    task = brief.format_covered([
        {"date": "2026-08-10", "key": "older", "title": "x"},
        {"date": "2026-08-14", "key": "newer", "title": "y"},
    ])
    assert task.index("newer") < task.index("older"), \
        "the likeliest repeats should lead"


# ── parsing what came back ─────────────────────────────────────────────────
def test_covered_output_is_normalised_and_stamped(tmp_path):
    (tmp_path / "covered.json").write_text(json.dumps([
        {"key": "  MiXeD-Case  ", "title": "T", "url": "u", "source": "s",
         "kind": "update"},
        {"key": "", "title": "no key — dropped"},
        "not a dict",
    ]))
    got = brief.read_covered_output(tmp_path, TODAY, log=lambda m: None)
    assert len(got) == 1
    assert got[0]["key"] == "mixed-case", "keys are compared, so normalise them"
    assert got[0]["kind"] == "update"
    assert got[0]["date"] == "2026-08-15"


@pytest.mark.parametrize("given", ["new", "NEW", "brand-new", "", None, 7])
def test_only_update_is_special_everything_else_is_new(tmp_path, given):
    """A junk `kind` must degrade to "new", not crash and not become "update".
    Mislabelling a new story as an update is the worse direction: it reads as
    "you already know this" about something you do not."""
    (tmp_path / "covered.json").write_text(json.dumps([{"key": "k", "kind": given}]))
    got = brief.read_covered_output(tmp_path, TODAY, log=lambda m: None)
    assert got[0]["kind"] == "new"


@pytest.mark.parametrize("body,why", [
    (None, "missing file"),
    ("{not json", "invalid JSON"),
    ('{"key": "not-a-list"}', "an object rather than a list"),
])
def test_a_broken_covered_file_warns_loudly_and_does_not_raise(tmp_path, body, why):
    """The briefing is already written, so this must not fail the run — but it is
    the failure that makes tomorrow repeat today, so it has to be loud."""
    if body is not None:
        (tmp_path / "covered.json").write_text(body)
    said = []
    got = brief.read_covered_output(tmp_path, TODAY, log=said.append)
    assert got == []
    assert said and "⚠" in said[0], f"{why} must produce a visible warning"
    assert "repeat" in said[0].lower() or "dedup" in said[0].lower(), \
        "the warning must say what it costs"


# ── the run ────────────────────────────────────────────────────────────────
def test_a_successful_run_publishes_to_reports_and_appends_the_ledger(cfg,
                                                                     monkeypatch):
    _fake_agent(cfg, monkeypatch=monkeypatch)
    res = brief.run(cfg, today=TODAY, log=lambda m: None)
    assert res["made"] is True
    dest = cfg.vault / "reports" / "ai-brief-2026-08-15"
    assert (dest / "index.html").is_file()
    meta = json.loads((dest / "meta.json").read_text())
    assert meta["tags"][0] == brief.TAG
    assert meta["date"] == "2026-08-15"
    assert "2026-08-15" in meta["title"]
    # the ledger grew, so tomorrow knows
    assert [d["key"] for d in brief.load_covered(cfg.vault, today=TODAY)] == ["k1"]


def test_it_publishes_to_reports_not_processed(cfg, monkeypatch):
    """A briefing is not a recording. Faking one would put a lie in the audit
    trail, and reports/ is already a first-class path in the vault browser."""
    _fake_agent(cfg, monkeypatch=monkeypatch)
    brief.run(cfg, today=TODAY, log=lambda m: None)
    assert (cfg.vault / "reports" / "ai-brief-2026-08-15").is_dir()
    assert not (cfg.vault / "processed").exists()
    assert not (cfg.vault / "inbox").exists()


def test_a_quiet_day_is_published_and_says_so(cfg, monkeypatch):
    """Some days nothing happens. That has to be a valid outcome, or the skill
    learns to manufacture news."""
    _fake_agent(cfg, covered="[]", monkeypatch=monkeypatch)
    res = brief.run(cfg, today=TODAY, log=lambda m: None)
    assert res["made"] is True and res["quiet"] is True
    meta = json.loads((cfg.vault / "reports" / "ai-brief-2026-08-15"
                       / "meta.json").read_text())
    assert "quiet" in meta["summary"].lower()


def test_a_second_run_on_the_same_day_is_a_no_op(cfg, monkeypatch):
    _fake_agent(cfg, monkeypatch=monkeypatch)
    brief.run(cfg, today=TODAY, log=lambda m: None)
    calls = []
    monkeypatch.setattr(brief.ex, "run",
                        lambda *a, **k: calls.append(1) or pytest.fail("re-ran"))
    res = brief.run(cfg, today=TODAY, log=lambda m: None)
    assert res["made"] is False and res["reason"] == "already exists"
    assert not calls


def test_no_index_html_publishes_nothing_and_keeps_the_evidence(cfg, monkeypatch):
    """A partial briefing in reports/ would be published by the next site build
    and would read as a complete quiet day. Staging exists to prevent that."""
    _fake_agent(cfg, index=None, monkeypatch=monkeypatch)
    res = brief.run(cfg, today=TODAY, log=lambda m: None)
    assert res["made"] is False
    assert not (cfg.vault / "reports" / "ai-brief-2026-08-15").exists()
    assert (cfg.vault / "reports" / ".ai-brief-2026-08-15.failed").is_dir(), \
        "the agent's output must survive for diagnosis"


def test_an_agent_failure_leaves_no_partial_report(cfg, monkeypatch):
    def boom(task, outdir, c, log=print):
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "index.html").write_text("half a briefing")
        raise brief.ex.ExecutionError("agent exceeded 3600s", retryable=True)
    monkeypatch.setattr(brief.ex, "run", boom)
    res = brief.run(cfg, today=TODAY, log=lambda m: None)
    assert res["made"] is False and "3600" in res["reason"]
    assert not (cfg.vault / "reports" / "ai-brief-2026-08-15").exists()
    assert not list((cfg.vault / "reports").glob("*.partial"))


def test_a_spent_money_budget_does_not_stop_the_briefing(cfg, monkeypatch):
    """Writing a briefing costs NO real money — the agent is subscription-billed.
    This used to block the whole briefing on the combined money budget, so an
    audio-heavy month silently stopped the morning briefing over spend it had not
    incurred. Only the optional audio is gated now."""
    _fake_agent(cfg, monkeypatch=monkeypatch)
    monkeypatch.setattr(usage, "budget_state",
                        lambda v, c, cat="tts": {"exhausted": True,
                                                 "spent_usd": 99.0,
                                                 "budget_usd": 10.0,
                                                 "month": "2026-08",
                                                 "env": "ATTICUS_TTS_BUDGET_USD"})
    res = brief.run(cfg, today=TODAY, log=lambda m: None)
    assert res["made"] is True, "the briefing itself must still be written"
    assert (cfg.vault / "reports" / "ai-brief-2026-08-15" / "index.html").is_file()


def test_an_exhausted_tts_budget_skips_only_the_audio(cfg, monkeypatch):
    _fake_agent(cfg, monkeypatch=monkeypatch)
    cfg.brief_audio = True
    monkeypatch.setattr(brief.pod, "generate",
                        lambda *a, **k: pytest.fail("must not spend"))
    monkeypatch.setattr(usage, "budget_state",
                        lambda v, c, cat="tts": {"exhausted": True,
                                                 "spent_usd": 99.0,
                                                 "budget_usd": 10.0,
                                                 "month": "2026-08",
                                                 "env": "ATTICUS_TTS_BUDGET_USD"})
    said = []
    res = brief.run(cfg, today=TODAY, log=said.append)
    assert res["made"] is True and res["audio"] is False
    assert any("TTS budget" in s for s in said)


def test_the_run_is_recorded_as_subscription_not_money(cfg, monkeypatch):
    """Same trap the execute stage documents: the agent bills nothing per token,
    so filing it as real money would resurrect the mistake that split fixed."""
    _fake_agent(cfg, monkeypatch=monkeypatch)
    brief.run(cfg, today=TODAY, log=lambda m: None)
    events = [e for e in brief.usage.load(cfg.vault) if e["kind"] == "agent"]
    assert len(events) == 1
    assert events[0]["billing"] == brief.usage.SUBSCRIPTION
    assert events[0]["stem"] == "ai-brief-2026-08-15"


def test_the_vault_is_committed_with_a_real_git_signature(cfg, monkeypatch):
    """Regression: brief.py called `Git(vault, cfg, log=...)` against a
    constructor that takes (vault, name, email, retries, log). Every test passed
    because the double accepted anything; the first live run raised TypeError
    AFTER the briefing was written and the ledger appended, leaving it
    uncommitted. _SpyGit now binds the real signature."""
    _git_calls.clear()
    _fake_agent(cfg, monkeypatch=monkeypatch)
    brief.run(cfg, today=TODAY, log=lambda m: None)
    ctor = [c for c in _git_calls if not (isinstance(c, tuple)
                                         and c and c[0] == "commit_push")]
    assert ctor, "Git must be constructed"
    pushes = [c for c in _git_calls if isinstance(c, tuple) and c and c[0] == "commit_push"]
    assert pushes, "the briefing must be committed and pushed"
    assert "ai-brief 2026-08-15" in pushes[0][1]


# ── the notification ───────────────────────────────────────────────────────
def test_the_briefing_is_pushed_with_a_link(cfg, monkeypatch):
    sent = {}

    def fake_notify(target, body, **kw):
        sent["url"] = target.notify_url
        sent["body"] = body
        sent["title"] = kw.get("title")
        return True
    monkeypatch.setattr(brief, "notify", fake_notify)
    # The shared cfg fixture nulls both notify urls so no test can post anywhere.
    cfg.result_notify_url = "https://ntfy.example/results"
    cfg.site_base_url = "http://forge/atticus"
    brief._notify(cfg, TODAY, [{"kind": "new", "title": "A thing"}], "ai-brief-x")
    assert sent["url"] == cfg.result_notify_url, "must use the RESULT topic"
    assert "http://forge/atticus/docs/ai-brief-x/index.html" in sent["body"]
    assert "2026-08-15" in sent["title"]


def test_a_failed_push_is_reported_not_swallowed(cfg, monkeypatch):
    """The first version discarded notify()'s return and logged nothing, so a
    briefing whose push failed reported complete success and the operator simply
    never heard about that morning. A 7am briefing nobody is told about is a file
    on a disk."""
    monkeypatch.setattr(brief, "notify", lambda *a, **k: False)
    said = []
    ok = brief._notify(cfg, TODAY, [], "slug", log=said.append)
    assert ok is False, "the outcome must be returned, not swallowed"


def test_the_run_reports_whether_it_notified(cfg, monkeypatch):
    _fake_agent(cfg, monkeypatch=monkeypatch)
    monkeypatch.setattr(brief, "_notify", lambda *a, **k: False)
    said = []
    res = brief.run(cfg, today=TODAY, log=said.append)
    assert res["notified"] is False
    assert any("NOT notified" in s for s in said), \
        "a failed push must be visible in the log, not inferred from silence"


def test_no_result_url_says_so_rather_than_returning_quietly(cfg, monkeypatch):
    monkeypatch.setattr(brief, "notify",
                        lambda *a, **k: pytest.fail("must not attempt a send"))
    cfg.result_notify_url = ""
    said = []
    assert brief._notify(cfg, TODAY, [], "slug", log=said.append) is False
    assert any("alerts nobody" in s for s in said)


def test_dry_run_builds_the_prompt_and_runs_nothing(cfg, monkeypatch):
    monkeypatch.setattr(brief.ex, "run",
                        lambda *a, **k: pytest.fail("dry run must not spend"))
    res = brief.run(cfg, today=TODAY, dry_run=True, log=lambda m: None)
    assert res["made"] is False and "covered.json" in res["task"]
    assert not (cfg.vault / "reports").exists()
