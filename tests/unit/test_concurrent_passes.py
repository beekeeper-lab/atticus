"""Two processor passes must not run at once — the production race of 2026-07-30.

The lock's own docstring required its location be "STABLE and shared, or a manual
run and a timed run would take different locks and the race this exists to prevent
would still happen." It wasn't:

  * the processor unit has PrivateTmp=yes and NO XDG_RUNTIME_DIR in Environment,
    so a timed pass locked /tmp/atticus-<uid>/processor.lock inside its OWN
    private /tmp;
  * a manual pass locked $XDG_RUNTIME_DIR/atticus/processor.lock.

Two files, no exclusion. The 15:05 and 15:10 timer passes both walked into a
record a manual pass was actively executing, declared it "interrupted
mid-execution", and wrote a spurious failures/ entry for a run that went on to
publish normally. The lock now lives in the vault, which every participant sees
identically.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from lock import single_instance

REPO = Path(__file__).resolve().parents[2]


def test_the_lock_lives_in_the_vault_when_one_is_known(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    with single_instance("processor", vault=vault):
        assert (vault / ".git/atticus-processor.lock").exists()


def test_the_vault_lock_is_not_in_the_working_tree(tmp_path):
    """.git/ so `git add -A` can never stage it."""
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    with single_instance("processor", vault=vault):
        assert not list(p for p in vault.iterdir() if p.name != ".git")


def test_a_process_without_XDG_RUNTIME_DIR_is_still_excluded(tmp_path):
    """The exact production shape: the unit has no XDG_RUNTIME_DIR and a private
    /tmp, so only a vault-relative lock can exclude it."""
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    code = (
        f'import sys; sys.path.insert(0, {str(REPO / "processor")!r})\n'
        'from lock import single_instance, AlreadyRunning\n'
        'from pathlib import Path\n'
        'try:\n'
        f'    with single_instance("processor", vault=Path({str(vault)!r})):\n'
        '        print("GOT_LOCK")\n'
        'except AlreadyRunning:\n'
        '    print("EXCLUDED")\n'
    )
    with single_instance("processor", vault=vault):
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, env={"PATH": "/usr/bin"})
    assert p.stdout.strip() == "EXCLUDED", p.stdout + p.stderr


def test_the_holder_is_named_in_the_error(tmp_path):
    """A failed acquisition used to TRUNCATE the holder's pid before reporting,
    destroying the one fact the message wanted."""
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    code = (
        f'import sys; sys.path.insert(0, {str(REPO / "processor")!r})\n'
        'from lock import single_instance, AlreadyRunning\n'
        'from pathlib import Path\n'
        'try:\n'
        f'    with single_instance("processor", vault=Path({str(vault)!r})):\n'
        '        pass\n'
        'except AlreadyRunning as e:\n'
        '    print(e)\n'
    )
    with single_instance("processor", vault=vault):
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, env={"PATH": "/usr/bin"})
    assert f"pid {os.getpid()}" in p.stdout, p.stdout


def test_no_vault_still_falls_back(tmp_path, monkeypatch):
    """A pass whose config is broken must still take *some* lock."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with single_instance("processor"):
        assert (tmp_path / "atticus/processor.lock").exists()


# --- the EXECUTING liveness check -----------------------------------------

def _rec(tmp_path, **owner):
    from vault import EXECUTING, Record
    meta = tmp_path / "r.json"
    data = {"plaud_id": "p", "status": EXECUTING}
    if owner:
        data["executing_by"] = owner
    meta.write_text(json.dumps(data))
    return Record(meta, json.loads(meta.read_text()))


def test_a_live_run_by_this_process_is_not_declared_abandoned(tmp_path, cfg):
    """The spurious-failure bug: a record being executed RIGHT NOW must be left
    alone, not failed."""
    import pipeline
    from vault import utcnow
    rec = _rec(tmp_path, host=os.uname().nodename, pid=os.getpid(), at=utcnow())
    assert pipeline._execution_is_live(rec, cfg, pipeline.Log("ERROR")) is True


def test_a_dead_pid_is_abandoned(tmp_path, cfg):
    import pipeline
    from vault import utcnow
    # PID 2**22 is above the default pid_max and cannot exist.
    rec = _rec(tmp_path, host=os.uname().nodename, pid=2**22, at=utcnow())
    assert pipeline._execution_is_live(rec, cfg, pipeline.Log("ERROR")) is False


def test_a_stamp_older_than_the_timeout_is_abandoned(tmp_path, cfg):
    """Even a live-looking pid cannot hold a record forever."""
    import pipeline
    rec = _rec(tmp_path, host=os.uname().nodename, pid=os.getpid(),
               at="2020-01-01T00:00:00Z")
    assert pipeline._execution_is_live(rec, cfg, pipeline.Log("ERROR")) is False


def test_another_hosts_recent_run_is_treated_as_live(tmp_path, cfg):
    """Declaring a remote peer's live run dead would double-execute it."""
    import pipeline
    from vault import utcnow
    rec = _rec(tmp_path, host="some-other-box", pid=1, at=utcnow())
    assert pipeline._execution_is_live(rec, cfg, pipeline.Log("ERROR")) is True


def test_an_unstamped_executing_record_is_abandoned(tmp_path, cfg):
    """Records written before the stamp existed, and genuine kills that never
    got to write one."""
    import pipeline
    rec = _rec(tmp_path)
    assert pipeline._execution_is_live(rec, cfg, pipeline.Log("ERROR")) is False
