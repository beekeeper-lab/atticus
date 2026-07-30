"""ops/lib/check-staged.sh — the pre-commit credential guard.

This had no coverage at all. It was inline in ops/pr.sh, a linear script that
pushes and squash-merges, so exercising the guard meant landing a real PR. A
security control nobody can test is a security control nobody knows works — and
this one was also missing the credential this project's own design centres on.
"""
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "ops/lib/check-staged.sh"


@pytest.fixture
def staged(tmp_path):
    """A git repo where a test can stage arbitrary content and run the guard."""
    r = tmp_path / "repo"
    r.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(r), *args], check=True)

    def _run(files: dict) -> subprocess.CompletedProcess:
        for name, body in files.items():
            p = r / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        subprocess.run(["git", "-C", str(r), "add", "-A", "-f"], check=True)
        return subprocess.run(
            ["bash", "-c", f'source "{LIB}"; check_staged "{r}"'],
            capture_output=True, text=True)

    return _run


def test_clean_content_is_allowed(staged):
    r = staged({"main.py": "def hello():\n    return 42\n"})
    assert r.returncode == 0, r.stderr


# Every fake secret below is ASSEMBLED from fragments rather than written as a
# literal. The guard under test scans the staged diff for exactly these shapes and
# has NO exemption mechanism — deliberately, because "skip test files" is a hole:
# a test is an ordinary place to paste a real key by accident. So this file must
# not contain a matching literal, or it could never be committed. Verified: the
# guard caught this file on its first real run, which is how the rule was found.
_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.mark.parametrize("label,secret", [
    ("openai", "sk-" + _ALNUM),
    ("github pat", "ghp" + "_" + _ALNUM),
    ("github fine-grained", "github" + "_pat_" + _ALNUM[:24]),
    # The one the original regex missed, and the primary secret in this design:
    # the Plaud workspace token, harvested off a live request.
    ("plaud jwt", "ey" + "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                  "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"),
    ("aws key", "AK" + "IAIOSFODNN7EXAMPLE"),
    ("slack", "xox" + "b-1234567890-abcdefghijkl"),
    # An ntfy topic URL is a bearer capability: it can read alarms carrying
    # transcript fragments, and send spoofed ones.
    ("ntfy topic", "https://" + "ntfy.sh/atticus-alerts-9f3ab21c"),
    ("private key", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
])
def test_credential_shapes_are_refused(staged, label, secret):
    r = staged({"leak.py": f'TOKEN = "{secret}"\n'})
    assert r.returncode == 1, f"{label} was NOT refused"
    assert "credential-shaped" in r.stderr


def test_this_file_does_not_itself_trip_the_guard():
    """The fragments above must stay fragmented, or this file becomes
    uncommittable and every test in it unreachable."""
    r = subprocess.run(
        ["bash", "-c",
         f'source "{LIB}"; grep -cE "$CHECK_STAGED_CRED_RE" "{Path(__file__)}" || true'],
        capture_output=True, text=True)
    assert r.stdout.strip() == "0", (
        "test_check_staged.py contains a literal credential shape — reassemble it "
        "from fragments or this file can never be landed")


@pytest.mark.parametrize("path", [
    "ops/.env",
    "nested/ops/.env",
    ".env",
    "docs/recon/session.json",
    ".scratch-vault/inbox/x.json",
])
def test_forbidden_paths_are_refused(staged, path):
    r = staged({path: "harmless content\n"})
    assert r.returncode == 1, f"{path} was NOT refused"
    assert "refusing to commit" in r.stderr


def test_a_filename_containing_a_space_cannot_slip_the_guard(staged):
    """The original `for f in $(git diff --cached --name-only)` split on
    whitespace, so this path was tested as two nonexistent paths and passed."""
    r = staged({"docs/recon/my session.json": "x\n"})
    assert r.returncode == 1, "a path with a space slipped the guard"


def test_a_non_ascii_filename_cannot_slip_the_guard(staged):
    """git C-quotes non-ASCII names in normal output, which defeated the case
    match; -z gives raw bytes."""
    r = staged({"docs/recon/sesión.json": "x\n"})
    assert r.returncode == 1, "a non-ASCII path slipped the guard"


def test_env_example_is_still_committable(staged):
    """The guard must not block the tracked template — .env.example ships."""
    r = staged({"ops/.env.example": "ATTICUS_WAKE_PHRASE=atticus\n"})
    assert r.returncode == 0, r.stderr


def test_prose_mentioning_a_token_is_not_a_false_positive(staged):
    """Docs discuss tokens constantly; only real shapes may trip the guard."""
    r = staged({"SECURITY.md": "The workspace token is a Bearer JWT (eyJ…) and "
                               "must never be committed. Rotate sk- keys.\n"})
    assert r.returncode == 0, r.stderr


def test_pr_sh_still_sources_the_guard():
    """If pr.sh stops calling it, every test above becomes decorative."""
    body = (REPO / "ops/pr.sh").read_text()
    assert "lib/check-staged.sh" in body
    assert "check_staged" in body
