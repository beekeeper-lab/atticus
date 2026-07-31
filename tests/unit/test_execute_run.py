"""C1 + M7 — output collection is an exfiltration boundary, and ATTICUS_SANDBOX=off
must actually be able to start the agent.

C1: collection runs in the PIPELINE namespace, where ~/.ssh and the vault DO
exist. The agent (sandboxed, unable to see them) can still plant a symlink in
output/ and let collection follow it out. Refuse symlinks and paths that escape
output/.

M7: with the sandbox off there is no mount namespace, so the synthetic PATH
(ws/bin) and HOME (ws/home) that only the sandbox populated point at nothing.
The binary must be resolved to an absolute host path and HOME must be real.
"""
import json
import os
import types

import pytest
from pathlib import Path

import execute as ex


def _fake_claude(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


def test_symlink_in_output_is_refused_and_secret_never_copied(cfg, tmp_path):
    """A symlink the agent plants in output/ must not be followed during
    collection, and a file that escapes output/ via a symlinked PARENT dir must
    not be copied either."""
    cfg.sandbox = False
    cfg.skills_dir = tmp_path / "no-skills"      # skip the skills copytree

    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    secret = secret_dir / "id_rsa"
    secret.write_bytes(b"TOP-SECRET-DEPLOY-KEY-BYTES")
    nested = secret_dir / "more"
    nested.mkdir()
    (nested / "token").write_bytes(b"ANOTHER-SECRET-TOKEN")

    # The fake agent writes one real file, plants a symlink at the secret, and
    # symlinks a whole directory (so its children resolve outside output/).
    script = _fake_claude(tmp_path / "claude", f"""
ln -s '{secret}' "$ATTICUS_OUTPUT_DIR/leak"
ln -s '{nested}' "$ATTICUS_OUTPUT_DIR/sub"
printf 'a real deliverable' > "$ATTICUS_OUTPUT_DIR/report.html"
exit 0
""")
    cfg.claude_bin = str(script)

    dest = tmp_path / "dest"
    res = ex.run("TASK", dest, cfg, log=lambda m: None)

    # The genuine file made it through.
    assert (dest / "report.html").read_text() == "a real deliverable"
    # The symlink and the escaping child did NOT.
    assert not (dest / "leak").exists()
    assert not (dest / "sub").exists()
    assert res["files"] == 1

    # And no secret bytes reached the destination by any path.
    blob = b"".join(p.read_bytes() for p in dest.rglob("*") if p.is_file())
    assert b"TOP-SECRET-DEPLOY-KEY-BYTES" not in blob
    assert b"ANOTHER-SECRET-TOKEN" not in blob


def test_sandbox_off_resolves_absolute_binary_and_real_home(cfg, tmp_path, monkeypatch):
    cfg.sandbox = False
    cfg.skills_dir = tmp_path / "no-skills"

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_claude(bindir / "claude", "exit 0")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    cfg.claude_bin = "claude"                    # a bare name, as in prod

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        out = Path(kw["env"]["ATTICUS_OUTPUT_DIR"])
        (out / "r.html").write_text("<html>x</html>")
        return types.SimpleNamespace(returncode=0, stdout="done", stderr="")

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    res = ex.run("TASK", tmp_path / "dest", cfg, log=lambda m: None)

    # The bare "claude" was resolved to an absolute host path...
    assert os.path.isabs(captured["cmd"][0])
    assert captured["cmd"][0].endswith("/claude")
    # ...and HOME points at a directory that actually exists, so Claude Code can
    # find its credential and config instead of a temp path holding neither.
    home = captured["env"]["HOME"]
    assert Path(home).is_dir()
    assert res["files"] == 1


# The REAL envelope Claude Code emits when it hits --max-budget-usd, captured
# from a live run on 2026-07-31. Note stderr is EMPTY and every budget signal is
# in stdout — the inverse of what the original version of this test asserted.
REAL_BUDGET_ENVELOPE = json.dumps({
    "type": "result",
    "subtype": "error_max_budget_usd",
    "terminal_reason": "budget_exhausted",
    "errors": ["Reached maximum budget ($2)"],
    "permission_denials": [],
    "duration_ms": 502292,
    "usage": {"input_tokens": 30, "output_tokens": 18000,
              "cache_read_input_tokens": 400000},
    "total_cost_usd": 2.0,
})


def _fake_proc(monkeypatch, *, stdout="", stderr="", rc=1):
    import subprocess

    import execute as ex

    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(a[0] if a else [], rc, stdout, stderr)
    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    monkeypatch.setattr(ex, "wrap_sandbox", lambda cmd, *a, **k: cmd)
    return ex


def test_budget_exhaustion_is_not_retried(tmp_path, cfg, monkeypatch):
    """Retrying a deterministic failure spends the ceiling again to hit the same
    wall. Observed 2026-07-30, and then again on 2026-07-31 — because the guard
    this test was written to protect never fired.

    The original version of this test fed the code
    `stderr="Error: Exceeded USD budget (2)"` with empty stdout. That string was
    invented to match the implementation's regex, and it is the inverse of
    reality: the CLI puts budget exhaustion in the stdout JSON envelope and
    leaves stderr empty. So both the code and the test encoded the same wrong
    assumption, the test passed, and the protection was dead for a week. This now
    uses the captured envelope.
    """
    ex = _fake_proc(monkeypatch, stdout=REAL_BUDGET_ENVELOPE, stderr="")
    with pytest.raises(ex.ExecutionError) as e:
        ex.run("task", tmp_path / "out", cfg, log=lambda m: None)
    assert e.value.retryable is False, "budget exhaustion must not be retried"
    assert "spend ceiling" in str(e.value)
    assert "ATTICUS_MAX_BUDGET_USD" in str(e.value), "must name the remedy"


def test_a_ceiling_hit_carries_its_usage_so_the_ledger_is_not_zero(tmp_path, cfg,
                                                                  monkeypatch):
    """A run killed at the ceiling consumed the whole ceiling and produced
    nothing. It was recorded as $0.00, which made the most expensive events in
    the system the invisible ones."""
    ex = _fake_proc(monkeypatch, stdout=REAL_BUDGET_ENVELOPE)
    with pytest.raises(ex.ExecutionError) as e:
        ex.run("task", tmp_path / "out", cfg, log=lambda m: None)
    assert e.value.usage, "the exception must carry what the run spent"
    assert e.value.usage.get("usd", 0) > 0
    assert e.value.usage.get("output_tokens", 0) > 0


@pytest.mark.parametrize("stdout,stderr,why", [
    (REAL_BUDGET_ENVELOPE, "", "the real envelope, stderr empty"),
    ('{"subtype": "error_max_budget_usd"}', "", "subtype alone"),
    ('{"terminal_reason": "budget_exhausted"}', "", "terminal_reason alone"),
    ('{"errors": ["Reached maximum budget ($2)"]}', "", "errors list alone"),
    ("", "Error: Exceeded USD budget (2)", "legacy stderr wording"),
    ("", "reached maximum budget", "prose on stderr"),
])
def test_every_budget_signal_is_recognised(stdout, stderr, why):
    """Any one field could be renamed by a CLI upgrade, and failing OPEN here
    means retrying a deterministic failure — so breadth is the safer error."""
    import execute as ex
    assert ex.budget_exhausted(stdout, stderr) is True, why


@pytest.mark.parametrize("stdout,stderr", [
    ("", ""),
    ('{"type": "result", "subtype": "success"}', ""),
    ("not json at all", "some unrelated failure"),
    ('{"errors": ["rate limited"]}', ""),
])
def test_unrelated_failures_are_not_mistaken_for_budget(stdout, stderr):
    """Over-matching would make a transient failure permanently non-retryable,
    which loses work."""
    import execute as ex
    assert ex.budget_exhausted(stdout, stderr) is False


def test_an_ordinary_agent_failure_is_still_retried(tmp_path, cfg, monkeypatch):
    """The fix above must not make every failure terminal."""
    import subprocess
    import execute as ex

    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(
            a[0] if a else [], 1, "", "transient upstream hiccup")

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    monkeypatch.setattr(ex, "wrap_sandbox", lambda cmd, *a, **k: cmd)
    with pytest.raises(ex.ExecutionError) as e:
        ex.run("task", tmp_path / "out", cfg, log=lambda m: None)
    assert e.value.retryable is True
