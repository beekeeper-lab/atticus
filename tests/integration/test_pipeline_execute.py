"""M18 — the gate->execute path, end to end, with a FAKE `claude` binary.

Drives process() over a temp vault with a tiny shell script standing in for the
agent: it writes an HTML file into $ATTICUS_OUTPUT_DIR and exits 0. Two paths:

  (a) an ambient transcript (no wake phrase) is filed as a note, NOT executed;
  (b) a wake-phrase transcript is executed and published.

Run with ATTICUS_SANDBOX=off (cfg.sandbox=False) — full bwrap is impractical in
CI, and the off path is exactly what M7 made usable. That is the one thing this
does not cover: the mount namespace itself (see tests/security for that).
"""
import subprocess

import pipeline as pl
from conftest import write_record
from vault import Git, load_records, write_atomic


def _fake_claude(tmp_path):
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        "printf '<html><body>a report about the topic</body></html>' "
        "> \"$ATTICUS_OUTPUT_DIR/report.html\"\n"
        "exit 0\n")
    script.chmod(0o755)
    return script


def _prepared(cfg, git_vault, tmp_path, transcript, stem):
    """A record parked at TRANSCRIBED with its transcript on disk, so process()
    exercises route+execute without a live transcription call."""
    cfg.sandbox = False
    cfg.wake_adjudicator = False          # no network for the ambient path
    cfg.claude_bin = str(_fake_claude(tmp_path))
    cfg.skills_dir = tmp_path / "no-skills"
    cfg.claude_model = None
    cfg.allowed_tools = []

    write_record(cfg.vault, stem=stem, status="transcribed", word_count=len(transcript.split()))
    rec = next(r for r in load_records(cfg.vault) if r.stem == stem)
    write_atomic(rec.transcript_path(cfg.vault), transcript + "\n")
    git = Git(git_vault.work, "t", "t@t", retries=1, log=lambda m: None)
    return rec, git


def _remote_has(remote, needle):
    log = subprocess.run(["git", "-C", str(remote), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    return needle in log


def test_ambient_transcript_is_filed_as_a_note_not_executed(cfg, git_vault, tmp_path):
    rec, git = _prepared(
        cfg, git_vault, tmp_path,
        "please water the plants and remember to call mom later today",
        "2026-07-29T090000Z_ambient")

    pl.process(rec, cfg, git, pl.Log("ERROR"))

    assert rec.status == "published"
    assert rec.data.get("executed") is False
    assert (rec.outdir(cfg.vault) / "note.md").exists()
    assert not (rec.outdir(cfg.vault) / "report.html").exists()


def test_wake_phrase_transcript_executes_and_publishes(cfg, git_vault, tmp_path):
    rec, git = _prepared(
        cfg, git_vault, tmp_path,
        "Atticus, write a short summary about local birdsong please",
        "2026-07-29T100000Z_wake")

    pl.process(rec, cfg, git, pl.Log("ERROR"))

    assert rec.status == "published"
    assert rec.data.get("executed") is True
    report = rec.outdir(cfg.vault) / "report.html"
    assert report.exists() and b"report about the topic" in report.read_bytes()
    assert _remote_has(git_vault.remote, "publish")
