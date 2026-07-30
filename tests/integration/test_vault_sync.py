"""B1 — a record must not advance unless its state is durably in the remote."""
import subprocess
import pytest
from vault import Git, VaultSyncError


def test_push_reaches_the_remote(git_vault):
    g = Git(git_vault.work, "t", "t@t", retries=1)
    (git_vault.work / "inbox" / "new.txt").write_text("x")
    assert g.commit_push("add") is True
    log = subprocess.run(["git", "-C", str(git_vault.remote), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "add" in log


def test_unreachable_remote_raises_rather_than_lying(git_vault):
    subprocess.run(["git", "-C", str(git_vault.work), "remote", "set-url",
                    "origin", "/nonexistent/remote.git"], check=True, capture_output=True)
    g = Git(git_vault.work, "t", "t@t", retries=1, log=lambda m: None)
    (git_vault.work / "inbox" / "new.txt").write_text("x")
    with pytest.raises(VaultSyncError):
        g.commit_push("add")


def test_clean_tree_still_pushes_when_ahead(git_vault):
    """The second hole: a commit stranded by an earlier failed push was never
    retried, because a clean tree returned success without consulting the
    remote. For a record in its terminal state, no further commit would come."""
    subprocess.run(["git", "-C", str(git_vault.work), "commit", "--allow-empty",
                    "-m", "stranded"], check=True, capture_output=True)
    before = subprocess.run(["git", "-C", str(git_vault.remote), "log", "--oneline"],
                            capture_output=True, text=True).stdout
    assert "stranded" not in before

    g = Git(git_vault.work, "t", "t@t", retries=1, log=lambda m: None)
    assert g.commit_push("nothing new") is True

    after = subprocess.run(["git", "-C", str(git_vault.remote), "log", "--oneline"],
                           capture_output=True, text=True).stdout
    assert "stranded" in after, "stranded commit was never pushed"


def test_local_only_vault_is_fine(tmp_path):
    """A vault with no remote (tests, first-run) must not raise."""
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", k, v], check=True, capture_output=True)
    (tmp_path / "f.txt").write_text("x")
    assert Git(tmp_path, "t", "t@t").commit_push("local") is True
