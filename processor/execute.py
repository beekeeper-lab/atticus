"""Run the agent against a transcript.

Two properties matter more than anything else here:

  1. The agent never touches git. It writes files into an output directory
     and the pipeline commits them. No deploy key in its environment, so a
     confused agent cannot rewrite history or push a branch.

  2. The agent runs in a scratch workspace, not in the vault. It sees the
     skills directory and an empty output dir — nothing else. Whatever it
     produces is copied into the vault afterwards.

Skill selection is left to the model. Claude Code already reads
.claude/skills/*/SKILL.md descriptions and picks a match, so adding a
capability means adding a skill directory — no routing table here.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

PREAMBLE = """\
# Spoken instruction

The text below was dictated aloud into a wearable recorder and transcribed
automatically. Treat it as a task to carry out, not as text to reproduce.
Transcription is imperfect — infer intent from context; do not fixate on
literal wording, and do not ask clarifying questions, since nobody is
available to answer.

If one of your available skills fits this request, use it. Otherwise just do
what was asked, sensibly and completely.

## Output contract

- Write every deliverable into: `{outdir}`
- Prefer **one self-contained HTML file** — this is the house standard for
  guides, reports, comparisons and plans. Follow the `html-artifact-output`
  skill if it is available.
- Do not modify anything outside `{outdir}`.
- Do not run git. The pipeline commits your output for you.
- Finish the work. Partial output with a note saying what is missing is much
  worse than a smaller deliverable that is complete.

## Transcript

{transcript}
"""


class ExecutionError(RuntimeError):
    def __init__(self, msg, *, retryable=False):
        super().__init__(msg)
        self.retryable = retryable


def build_task(transcript: str, outdir_label: str) -> str:
    return PREAMBLE.format(outdir=outdir_label, transcript=transcript.strip())


def run(task_md: str, dest_outdir: Path, cfg, *, log=print) -> dict:
    """Execute the task in a scratch workspace; copy results to dest_outdir.

    Returns {'files': n, 'bytes': n, 'stdout_tail': str}.
    """
    dest_outdir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="atticus-job-") as tmp:
        ws = Path(tmp)
        out = ws / "output"
        out.mkdir()

        # Make skills visible to the agent without exposing the repo.
        if cfg.skills_dir.is_dir():
            (ws / ".claude").mkdir()
            try:
                (ws / ".claude/skills").symlink_to(cfg.skills_dir.resolve())
            except OSError:
                shutil.copytree(cfg.skills_dir, ws / ".claude/skills")

        (ws / "TASK.md").write_text(task_md)

        cmd = [cfg.claude_bin, "-p", "--output-format", "text",
               "--permission-mode", "acceptEdits",
               "--add-dir", str(out)]
        if cfg.claude_model:
            cmd += ["--model", cfg.claude_model]

        env = {k: v for k, v in os.environ.items()
               if k not in ("GIT_SSH_COMMAND", "GIT_ASKPASS", "SSH_AUTH_SOCK")}
        env["ATTICUS_OUTPUT_DIR"] = str(out)

        log(f"    running agent (timeout {cfg.exec_timeout}s)…")
        try:
            proc = subprocess.run(
                cmd, input=task_md, cwd=ws, env=env,
                capture_output=True, text=True, timeout=cfg.exec_timeout,
            )
        except subprocess.TimeoutExpired:
            raise ExecutionError(
                f"agent exceeded {cfg.exec_timeout}s", retryable=True)
        except FileNotFoundError:
            raise ExecutionError(f"claude binary not found: {cfg.claude_bin}")

        tail = (proc.stdout or "")[-4000:]
        if proc.returncode != 0:
            err = (proc.stderr or "")[-500:]
            raise ExecutionError(
                f"agent exited {proc.returncode}: {err}", retryable=True)

        produced = [p for p in out.rglob("*") if p.is_file()]
        if not produced:
            # The agent may have answered in prose without writing a file.
            # Salvage it rather than losing the work.
            if tail.strip():
                (out / "response.md").write_text(tail.strip() + "\n")
                produced = [out / "response.md"]
                log("    agent wrote no files; saved its response as response.md")
            else:
                raise ExecutionError("agent produced no output at all",
                                     retryable=True)

        total = 0
        for src in produced:
            rel = src.relative_to(out)
            dst = dest_outdir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            total += src.stat().st_size

        return {"files": len(produced), "bytes": total, "stdout_tail": tail}
