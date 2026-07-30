"""The tests that back the isolation claims in the README and execute.py.

If any of these fail, a claim in the documentation has become false. That is the
whole reason they exist: the previous version of those claims was asserted in
prose and contradicted by the code.
"""
import shutil
import subprocess
import types
from pathlib import Path

import pytest

import execute as ex

pytestmark = pytest.mark.sandbox
needs_bwrap = pytest.mark.skipif(not shutil.which("bwrap"), reason="bwrap absent")


def test_env_allowlist_excludes_credentials(tmp_path, monkeypatch):
    """A1 — nothing credential-shaped survives into the agent's environment."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-appear")
    monkeypatch.setenv("ATTICUS_NOTIFY_URL", "https://ntfy.sh/secret-topic")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_nope")

    env = ex.agent_env(tmp_path, tmp_path / "output")
    blob = " ".join(f"{k}={v}" for k, v in env.items())

    assert "sk-should-never-appear" not in blob
    assert "secret-topic" not in blob
    assert "ghp_nope" not in blob
    for k in ("OPENAI_API_KEY", "ATTICUS_NOTIFY_URL", "AWS_SECRET_ACCESS_KEY",
              "GITHUB_TOKEN"):
        assert k not in env


def test_agent_home_is_not_the_operators(tmp_path):
    env = ex.agent_env(tmp_path, tmp_path / "output")
    assert env["HOME"] != str(Path.home())
    assert str(tmp_path) in env["HOME"]


def _run_in_sandbox(tmp_path, script: str) -> str:
    (tmp_path / "output").mkdir(exist_ok=True)
    cfg = types.SimpleNamespace(sandbox=True, claude_bin="claude")
    cmd = ex.wrap_sandbox(["/bin/bash", "-c", script], tmp_path,
                          tmp_path / "output", cfg)
    p = subprocess.run(cmd, env=ex.agent_env(tmp_path, tmp_path / "output"),
                       capture_output=True, text=True, timeout=60)
    return p.stdout.strip()


@needs_bwrap
def test_ssh_keys_are_unreachable(tmp_path):
    """A2 — 'the agent cannot touch git' must be enforced, not asserted.

    The deploy key was readable before this landed, which made stripping
    GIT_SSH_COMMAND cosmetic: the agent has a shell.
    """
    out = _run_in_sandbox(tmp_path, 'ls ~/.ssh 2>/dev/null | wc -l; '
                                    f'cat {Path.home()}/.ssh/* 2>/dev/null | wc -c')
    assert out.split() == ["0", "0"], f"ssh material reachable: {out!r}"


@needs_bwrap
def test_shared_credential_file_is_unreachable(tmp_path):
    out = _run_in_sandbox(tmp_path, f'cat {Path.home()}/.config/ai/env 2>/dev/null | wc -c')
    assert out == "0", f"credential file readable: {out!r}"


@needs_bwrap
def test_operator_home_is_empty(tmp_path):
    out = _run_in_sandbox(tmp_path, f'ls -A {Path.home()} 2>/dev/null | wc -l')
    assert out == "0", f"operator home visible: {out} entries"


@needs_bwrap
def test_only_the_cli_binary_is_bound_not_the_whole_bin_dir(tmp_path):
    """Binding ~/.local/bin wholesale would hand the agent notify-push,
    transcribe-audio and uv as a side effect of needing one executable."""
    out = _run_in_sandbox(tmp_path, f'ls {Path.home()}/.local/bin 2>/dev/null | wc -l')
    assert out == "0", f"~/.local/bin exposed: {out} entries"


@needs_bwrap
def test_cli_is_still_runnable(tmp_path):
    """Containment must not break the thing it contains."""
    out = _run_in_sandbox(tmp_path, 'command -v claude >/dev/null && echo found || echo MISSING')
    assert out == "found"


@needs_bwrap
def test_workspace_is_writable(tmp_path):
    """The containment must not be so tight the agent cannot do its job."""
    out = _run_in_sandbox(tmp_path, f'touch {tmp_path}/probe && echo ok')
    assert out == "ok"


@needs_bwrap
def test_dns_resolves_inside_the_sandbox(tmp_path):
    """Regression: /etc/resolv.conf symlinks into /run on systemd-resolved
    hosts. Without binding the target, DNS died and Claude Code reported the
    unhelpful 'API Error: Unable to connect to API (ENOTIMP)'."""
    out = _run_in_sandbox(tmp_path, 'test -s /etc/resolv.conf && echo ok || echo broken')
    assert out == "ok"


def test_sandbox_can_be_disabled_but_says_so(tmp_path):
    said = []
    cfg = types.SimpleNamespace(sandbox=False, claude_bin="claude")
    cmd = ex.wrap_sandbox(["true"], tmp_path, tmp_path / "output", cfg,
                          log=said.append)
    assert cmd == ["true"]
    assert any("DISABLED" in m for m in said)


def test_vault_path_never_reaches_the_prompt():
    """Defect #2 — the preamble used to hand the agent the vault path."""
    task = ex.build_task("do a thing")
    assert "atticus-vault" not in task
    assert "./output/" in task
