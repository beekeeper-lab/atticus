"""The tests that back the isolation claims in the README and execute.py.

If any of these fail, a claim in the documentation has become false. That is the
whole reason they exist: the previous version of those claims was asserted in
prose and contradicted by the code.
"""
import os
import shutil
import subprocess
import types
from pathlib import Path

import pytest

import execute as ex

pytestmark = pytest.mark.sandbox


def _bwrap_actually_works() -> bool:
    """Whether bwrap can really create a namespace here.

    Checking only that the binary exists is not enough, and CI proved it: on
    Ubuntu 24.04 runners AppArmor restricts unprivileged user namespaces, so
    bwrap is installed, runs, and silently produces nothing. The tests then
    compared empty strings to expected output and failed for a reason that had
    nothing to do with the code under test.
    """
    if not shutil.which("bwrap"):
        return False
    try:
        # Probe through the REAL wrapper rather than a hand-rolled bwrap line.
        # A hand-rolled one omitted the /lib64 symlink and failed with
        # "execvp: No such file or directory" — which is what a missing dynamic
        # linker looks like, not a missing binary. Testing the thing we actually
        # ship cannot drift from it.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "output").mkdir()
            cfg = types.SimpleNamespace(sandbox=True, claude_bin="true")
            cmd = ex.wrap_sandbox(["/usr/bin/true"], ws, ws / "output", cfg,
                                  log=lambda m: None)
            return subprocess.run(cmd, capture_output=True,
                                  timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


needs_bwrap = pytest.mark.skipif(
    not _bwrap_actually_works(),
    reason="bwrap cannot create a namespace here (restricted unprivileged userns?)")


def test_sandbox_is_available_where_it_is_required():
    """In CI, the isolation tests must RUN, not skip.

    A green build with every security test skipped reads as proof while proving
    nothing, which is a worse outcome than a red one. This replaces a fragile
    shell grep of pytest's summary line that silently reported zero.
    """
    if not os.environ.get("CI"):
        pytest.skip("only enforced in CI")
    assert _bwrap_actually_works(), (
        "bwrap cannot create a namespace on this runner, so every isolation "
        "test would skip. Fix the runner, do not accept the skip.")


def test_env_allowlist_excludes_credentials(tmp_path, monkeypatch):
    """A1 — nothing credential-shaped survives into the agent's environment.

    The fake values are ASSEMBLED rather than written literally. ops/pr.sh's
    credential guard scans the staged diff for exactly these shapes, and it has
    no exemption mechanism on purpose — a bypass for "it's only a test" is a hole,
    since a test file is a perfectly ordinary place to paste a real key by
    accident. Concatenating keeps the guard absolutely strict.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "should-never-appear")
    monkeypatch.setenv("ATTICUS_NOTIFY_URL", "https://" + "ntfy.sh/secret-topic")
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
@pytest.mark.skipif(not shutil.which("claude"),
                    reason="claude CLI not installed; nothing to bind or find")
def test_cli_is_still_runnable(tmp_path):
    """Containment must not break the thing it contains.

    Only meaningful where the CLI exists. On a runner without it, wrap_sandbox
    correctly declines to bind a binary that is not there — asserting it is
    findable would be asserting something about the runner, not the code.
    """
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


@needs_bwrap
def test_the_vault_is_neither_readable_nor_writable(tmp_path):
    """A2's acceptance criterion says "and that the vault is not writable", and
    nothing probed the vault path at all — the item was ticked regardless.

    This matters beyond exfiltration: the vault IS the queue, so an agent able to
    write it could advance another recording's status, plant a task file, or
    forge output for a recording it was never given.
    """
    from config import Config
    vault = Config().vault
    out = _run_in_sandbox(
        tmp_path,
        f'test -e {vault} && echo EXISTS || echo ABSENT; '
        f'ls -A {vault} 2>/dev/null | wc -l; '
        f'touch {vault}/.agent-probe 2>/dev/null && echo WROTE || echo NOWRITE')
    assert out.split() == ["ABSENT", "0", "NOWRITE"], \
        f"vault reachable from inside the sandbox: {out!r}"
    assert not (vault / ".agent-probe").exists(), \
        "the agent wrote into the real vault"


@needs_bwrap
def test_the_rest_of_dot_claude_is_not_exposed(tmp_path):
    """Only the credential and the allowlisted global skills may be bound.

    ~/.claude also holds history.jsonl, settings.json and sessions/ — none of
    which the agent needs, and all of which describe the operator's other work.
    """
    out = _run_in_sandbox(
        tmp_path,
        'cat ~/.claude/history.jsonl 2>/dev/null | wc -c; '
        'cat ~/.claude/settings.json 2>/dev/null | wc -c; '
        'ls -A ~/.claude/sessions 2>/dev/null | wc -l')
    assert out.split() == ["0", "0", "0"], f"extra ~/.claude material: {out!r}"


@needs_bwrap
def test_sandbox_net_none_removes_the_network(tmp_path):
    """The loopback-ingress hole: the shared netns makes local services — the
    vault web UI and its write token among them — reachable from a sandbox that
    supposedly cannot see the vault. This asserts the switch actually switches.
    """
    cfg = types.SimpleNamespace(sandbox=True, sandbox_net="none",
                                claude_bin="claude", global_skills=[])
    cmd = ex.wrap_sandbox(["/bin/bash", "-c",
                           "getent hosts example.com >/dev/null 2>&1 "
                           "&& echo NET || echo NONET"],
                          tmp_path, tmp_path / "output", cfg)
    assert "--unshare-net" in cmd
    p = subprocess.run(cmd, env=ex.agent_env(tmp_path, tmp_path / "output"),
                       capture_output=True, text=True, timeout=60)
    assert p.stdout.strip() == "NONET", f"network still reachable: {p.stdout!r}"


def test_token_mode_keeps_the_credential_file_out_of_the_namespace(tmp_path):
    """#68 — with ATTICUS_CLAUDE_TOKEN_FILE set, the operator's credential file
    (and the refresh token inside it) must not be bound in at all, and the
    agent's env must carry exactly the dedicated token instead."""
    tf = tmp_path / "oat"
    tf.write_text("sk-ant-" + "oat01-test-token\n")
    (tmp_path / "output").mkdir(exist_ok=True)
    cfg = types.SimpleNamespace(sandbox=True, claude_bin="true",
                                claude_token_file=str(tf))
    args = ex.wrap_sandbox(["/usr/bin/true"], tmp_path, tmp_path / "output", cfg,
                           log=lambda m: None)
    assert not any(".credentials.json" in str(a) for a in args), \
        "token mode must not bind the interactive credential"
    env = ex.agent_env(tmp_path, tmp_path / "output", cfg)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-" + "oat01-test-token"


def test_blank_token_file_keeps_the_old_bind(tmp_path):
    (tmp_path / "output").mkdir(exist_ok=True)
    cfg = types.SimpleNamespace(sandbox=True, claude_bin="true",
                                claude_token_file="")
    args = ex.wrap_sandbox(["/usr/bin/true"], tmp_path, tmp_path / "output", cfg,
                           log=lambda m: None)
    if (Path.home() / ".claude/.credentials.json").is_file():
        assert any(".credentials.json" in str(a) for a in args)
    env = ex.agent_env(tmp_path, tmp_path / "output", cfg)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


@pytest.mark.parametrize("prep", ["missing", "empty", "multiline"])
def test_a_configured_but_unusable_token_refuses_rather_than_falls_back(tmp_path, prep):
    """Silent fallback to the credential file would quietly un-do the migration
    this setting exists for. Refusal, naming the fix, is the contract."""
    tf = tmp_path / "oat"
    if prep == "empty":
        tf.write_text("")
    elif prep == "multiline":
        tf.write_text("line-one\nline-two\n")
    cfg = types.SimpleNamespace(sandbox=True, claude_token_file=str(tf))
    with pytest.raises(ex.ExecutionError, match="setup-token"):
        ex.agent_env(tmp_path, tmp_path / "output", cfg)


def test_expired_interactive_credential_is_not_blamed_in_token_mode():
    cfg = types.SimpleNamespace(claude_token_file="/anywhere")
    assert ex._credential_problem(cfg) is None
