"""Two defects observed on 2026-07-31, both of which looked like nothing.

1. `git add -A` staged the whole worktree, so pipeline commits swept unrelated
   in-progress edits and pushed them under misleading messages — four times in one
   session. CLAUDE.md asserts the roles own disjoint paths; nothing enforced it.

2. A record that failed and then succeeded kept its failures/ entry forever, so
   `atticus doctor` and the failures/ count overreported permanently.
"""
import json

import pytest
from conftest import write_record
from vault import (OWNED_BRIEF, OWNED_INGEST, OWNED_PROCESSOR, PUBLISHED, Git,
                   load_records)


# ── ownership is declared in one place and is disjoint where it must be ────
def test_no_role_claims_reports_or_site_except_the_brief():
    """These are the directories that actually got swept. reports/ belongs to the
    briefing alone; site/ belongs to no role at all and must never be staged by
    the pipeline."""
    for owned in (OWNED_INGEST, OWNED_PROCESSOR):
        assert "reports" not in owned
        assert "site" not in owned
    assert "reports" in OWNED_BRIEF
    assert "site" not in OWNED_BRIEF


def test_the_processor_owns_the_paths_it_actually_writes():
    """It advances record metadata in inbox/, writes processed/ and failures/, and
    appends the usage ledger in .state/. Missing any one of those means its own
    work silently stops being committed — a worse failure than the one being
    fixed."""
    for p in ("inbox", "processed", "failures", ".state"):
        assert p in OWNED_PROCESSOR, p


# ── the scoping actually reaches git ──────────────────────────────────────
class _Spy(Git):
    """Captures argv instead of running git."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []

    def _run(self, *args, check=True):
        self.calls.append(args)
        import subprocess
        return subprocess.CompletedProcess(list(args), 0, "", "")


def _git(tmp_path, **kw):
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    return _Spy(tmp_path, "t", "t@t", 1, log=lambda m: None, **kw)


def test_a_scoped_git_stages_only_its_own_paths(tmp_path):
    g = _git(tmp_path, paths=["inbox", ".state"])
    g._commit_push_locked("msg")
    add = next(c for c in g.calls if c[0] == "add")
    assert add == ("add", "-A", "--", "inbox", ".state"), add


def test_a_scoped_git_asks_status_about_the_same_scope(tmp_path):
    """Asking about the whole tree would see a neighbouring dirty file, call the
    tree dirty, then commit nothing — and `git commit` on an empty index exits
    non-zero, which this code turns into a failed pass. An editor open in another
    window must not be able to fail the pipeline."""
    g = _git(tmp_path, paths=["inbox"])
    g._commit_push_locked("msg")
    st = next(c for c in g.calls if c[0] == "status")
    assert "--" in st and "inbox" in st, st


def test_an_unscoped_git_still_stages_everything(tmp_path):
    """Opt-in, not imposed: retention rewrites records anywhere under inbox/ and
    the sweep path exists to recover work stranded by an interrupted pass."""
    g = _git(tmp_path)
    g._commit_push_locked("msg")
    add = next(c for c in g.calls if c[0] == "add")
    assert add == ("add", "-A"), add


def test_every_role_passes_a_scope_where_it_matters():
    """A regression guard on the wiring, not the mechanism. The mechanism worked
    from the start; what was missing was any caller using it."""
    import inspect

    import pipeline
    import brief
    for mod, want in ((pipeline, "OWNED_PROCESSOR"), (brief, "OWNED_BRIEF")):
        src = inspect.getsource(mod)
        assert f"paths={want}" in src, f"{mod.__name__} must scope its Git"


# ── the stale error file ──────────────────────────────────────────────────
def test_publishing_clears_a_stale_error_file(cfg):
    write_record(cfg.vault, status="executed")
    rec = load_records(cfg.vault)[0]
    err = rec.error_path(cfg.vault)
    err.parent.mkdir(parents=True, exist_ok=True)
    err.write_text(json.dumps({"stage": "executing", "error": "interrupted"}))
    assert err.is_file()

    rec.advance(PUBLISHED, executed=True)
    assert rec.clear_error(cfg.vault) is True
    assert not err.exists(), "a published record must not keep a failures/ entry"


def test_clearing_is_idempotent_and_never_raises(cfg):
    """Called on every publish, including the overwhelming majority that never
    failed. A published record must not be un-published by a filesystem problem."""
    write_record(cfg.vault)
    rec = load_records(cfg.vault)[0]
    assert rec.clear_error(cfg.vault) is False
    assert rec.clear_error(cfg.vault) is False


def test_the_records_own_history_survives_clearing(cfg):
    """The error FILE is a live signal, not an archive — attempts, failed_stage
    and last_error stay in the record, so deleting it loses nothing."""
    write_record(cfg.vault)
    rec = load_records(cfg.vault)[0]
    rec.fail(cfg.vault, "executing", "agent exited 1", False)
    assert rec.error_path(cfg.vault).is_file()
    rec.clear_error(cfg.vault)
    reloaded = load_records(cfg.vault)[0]
    assert reloaded.data["attempts"] == 1
    assert reloaded.data["failed_stage"] == "executing"
    assert "agent exited 1" in reloaded.data["last_error"]


@pytest.mark.parametrize("terminal", ["executed", "gated"])
def test_both_terminal_success_paths_clear_the_error(cfg, monkeypatch, terminal):
    """Publishing a deliverable and filing a gated note are both successes. Only
    the first one was clearing, so a recording that failed transcription, retried,
    and then gated kept its error file."""
    import inspect

    import pipeline
    src = inspect.getsource(pipeline)
    assert src.count("rec.clear_error(cfg.vault)") >= 2, \
        "both the published and the gated-note transitions must clear"
