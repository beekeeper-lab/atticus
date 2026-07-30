"""commit_push() must never report success without committing.

This was the worst silent failure in the system: `git add` and `git commit` ran
with their return codes discarded, and because a failed `status --porcelain`
yields empty stdout that reads as a clean tree, the function returned True having
committed nothing. The caller marked the record done, the ledger was already
written, and the work sat uncommitted and invisible to the other stage with no
error anywhere.
"""
import subprocess

import pytest
from vault import Git, VaultSyncError


def _repo(tmp_path):
    v = tmp_path / "vault"
    (v / "inbox").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(v)], check=True)
    subprocess.run(["git", "-C", str(v), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(v), "config", "user.name", "t"], check=True)
    return v


def _git(vault, **kw):
    return Git(vault, "t", "t@t", 1, log=lambda m: None, **kw)


def test_failed_add_raises_instead_of_reporting_success(tmp_path, monkeypatch):
    vault = _repo(tmp_path)
    (vault / "inbox" / "x.json").write_text("{}")
    git = _git(vault)

    real = Git._run

    def fake(self, *args, check=True, timeout=None):
        if args and args[0] == "add":
            return subprocess.CompletedProcess(list(args), 1, "", "index.lock exists")
        return real(self, *args, check=check, timeout=timeout)

    monkeypatch.setattr(Git, "_run", fake)
    with pytest.raises(VaultSyncError, match="add failed"):
        git.commit_push("should not silently succeed")


def test_failed_status_raises_rather_than_reading_as_a_clean_tree(tmp_path, monkeypatch):
    vault = _repo(tmp_path)
    (vault / "inbox" / "x.json").write_text("{}")
    git = _git(vault)

    real = Git._run

    def fake(self, *args, check=True, timeout=None):
        if args and args[0] == "status":
            return subprocess.CompletedProcess(list(args), 128, "", "fatal: bad index")
        return real(self, *args, check=check, timeout=timeout)

    monkeypatch.setattr(Git, "_run", fake)
    with pytest.raises(VaultSyncError, match="status failed"):
        git.commit_push("empty stdout is not a clean tree")


def test_failed_commit_raises(tmp_path, monkeypatch):
    vault = _repo(tmp_path)
    (vault / "inbox" / "x.json").write_text("{}")
    git = _git(vault)

    real = Git._run

    def fake(self, *args, check=True, timeout=None):
        if args and args[0] == "commit":
            return subprocess.CompletedProcess(list(args), 1, "", "pre-commit hook failed")
        return real(self, *args, check=check, timeout=timeout)

    monkeypatch.setattr(Git, "_run", fake)
    with pytest.raises(VaultSyncError, match="commit failed"):
        git.commit_push("a dirty tree that refuses to commit")


def test_clean_tree_with_no_remote_is_still_success(tmp_path):
    """The happy no-op must stay a no-op — this is the common case every pass."""
    vault = _repo(tmp_path)
    assert _git(vault).commit_push("nothing to do") is True


def test_a_hung_git_times_out_instead_of_blocking_forever(tmp_path, monkeypatch):
    """No git call had a timeout, so a half-open TCP to the remote blocked the
    pass — and a manual run hung indefinitely holding the processor lock, after
    which every timed pass exited 0 'skipped' while the pipeline was stalled."""
    vault = _repo(tmp_path)
    git = _git(vault)

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=kw.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = git._run("status", "--porcelain", check=False)
    assert r.returncode == 124
    assert "timed out" in r.stderr
