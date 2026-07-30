"""M20 — vault.py push retry + _resolve_benign, exercised with two real clones.

Two hosts push to one bare remote. When they collide on an ingest record and a
`.state/seen` ledger line (the BENIGN paths), the rebase must auto-resolve:
first-push-wins for the record, UNION for the append-only ledger so no host
loses its entries. A collision anywhere else must refuse and leave no rebase in
progress. The benign case also proves the retry loop (first push fails, second
succeeds) with retries>1.
"""
import subprocess

import pytest

from vault import Git, VaultSyncError


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=check)


def _config(work):
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")


@pytest.fixture
def two_clones(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   check=True, capture_output=True)
    # Seed the remote via a throwaway work tree so main exists.
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
    _config(seed)
    (seed / "inbox").mkdir()
    (seed / ".state").mkdir()
    (seed / "README").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    a = tmp_path / "A"
    b = tmp_path / "B"
    subprocess.run(["git", "clone", str(remote), str(a)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(b)], check=True, capture_output=True)
    _config(a)
    _config(b)
    return remote, a, b


def _write(work, rel, text):
    p = work / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_benign_collision_auto_resolves_and_unions_the_ledger(two_clones):
    remote, a, b = two_clones

    # B ingests first and pushes cleanly.
    _write(b, "inbox/rec.json", '{"host": "B"}\n')
    _write(b, ".state/seen.jsonl", "B-line\n")
    assert Git(b, "t", "t@t", retries=1, log=lambda m: None).commit_push("B ingest") is True

    # A ingests the SAME recording (different bookkeeping) and a different
    # ledger line, then pushes into the collision. retries>1 so the retry loop
    # can succeed after the rebase.
    _write(a, "inbox/rec.json", '{"host": "A"}\n')
    _write(a, ".state/seen.jsonl", "A-line\n")
    assert Git(a, "t", "t@t", retries=3, log=lambda m: None).commit_push("A ingest") is True

    # Pull the resolved state down and inspect it.
    fresh = a.parent / "verify"
    subprocess.run(["git", "clone", str(remote), str(fresh)], check=True, capture_output=True)
    ledger = (fresh / ".state/seen.jsonl").read_text()
    assert "A-line" in ledger and "B-line" in ledger, "the ledger union dropped an entry"
    # First push wins for the metadata record.
    assert '"host": "B"' in (fresh / "inbox/rec.json").read_text()


def test_non_benign_collision_refuses_and_leaves_no_rebase(two_clones):
    remote, a, b = two_clones

    _write(b, "processed/report.txt", "from B\n")
    assert Git(b, "t", "t@t", retries=1, log=lambda m: None).commit_push("B publish") is True

    _write(a, "processed/report.txt", "from A\n")
    g = Git(a, "t", "t@t", retries=3, log=lambda m: None)
    with pytest.raises(VaultSyncError):
        g.commit_push("A publish")

    # The rebase must have been aborted, not left half-applied for a human.
    assert not (a / ".git/rebase-merge").exists()
    assert not (a / ".git/rebase-apply").exists()


def test_concurrent_pulls_do_not_corrupt_fetch_head(git_vault):
    """The production failure this lock exists for, as a regression test.

    Ingest and the processor share one working tree but took DIFFERENT
    single-instance locks, so nothing excluded them. Two concurrent `git fetch`es
    leave .git/FETCH_HEAD holding more than one branch and `pull --rebase` aborts
    with "fatal: Cannot rebase onto multiple branches" — which alarmed as "the
    vault remote is unreachable" while the remote was perfectly healthy.

    Verified by hand before the fix: eight concurrent raw `git pull --rebase` in a
    clone failed 8/8 with that exact message; through Git.pull() they succeed 8/8.
    """
    import concurrent.futures as cf
    from vault import Git

    def pull(i):
        g = Git(git_vault.work, "t", "t@t", 1, log=lambda m: None)
        return g.pull(), g.last_error

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(pull, range(8)))

    failures = [err for ok, err in results if not ok]
    assert not failures, f"concurrent pulls failed: {failures}"
    # One line per fetched branch. More than one is the corruption itself.
    fetch_head = git_vault.work / ".git/FETCH_HEAD"
    if fetch_head.exists():
        assert len([ln for ln in fetch_head.read_text().splitlines() if ln.strip()]) <= 1


def test_the_git_lock_is_reentrant(git_vault):
    """commit_push() -> _push_with_retry() -> pull() all nest, and flock() would
    deadlock against itself on a second descriptor."""
    from vault import Git
    g = Git(git_vault.work, "t", "t@t", 1, log=lambda m: None)
    (git_vault.work / "inbox" / "nested.json").write_text("{}")
    assert g.commit_push("nested lock acquisition must not deadlock") is True
    assert g._lock_depth == 0, "the lock was not released"


def test_the_lock_file_is_never_committed(git_vault):
    """It lives in .git/, so it cannot be staged by `add -A`."""
    from vault import Git
    g = Git(git_vault.work, "t", "t@t", 1, log=lambda m: None)
    (git_vault.work / "inbox" / "x.json").write_text("{}")
    g.commit_push("lock must stay out of the tree")
    tracked = subprocess.run(["git", "-C", str(git_vault.work), "ls-files"],
                             capture_output=True, text=True).stdout
    assert "atticus-git.lock" not in tracked
