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
import os
import types
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
