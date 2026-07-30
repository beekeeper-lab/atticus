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
