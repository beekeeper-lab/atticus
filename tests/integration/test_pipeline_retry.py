"""C2 + M8 — retryable failures must actually auto-retry, and the RETRY_WAIT
transition must be committed to the queue.

C2: a due RETRY_WAIT record matched none of process()'s stage branches, so it
fell through every pass and only rearm() (manual --retry) ever revived it.
M8: the RETRY_WAIT transition was saved locally but never committed, so the
backoff state was invisible to any other pass or host.
"""
import subprocess

import pipeline as pl
from conftest import write_record
from vault import Git, RETRY_WAIT, TRANSCRIBED, load_records


def _remote_log(remote):
    return subprocess.run(["git", "-C", str(remote), "log", "--oneline"],
                          capture_output=True, text=True).stdout


def test_due_retry_wait_record_re_executes_the_failed_stage(cfg, git_vault, monkeypatch):
    # cfg.vault and git_vault.work share the same tmp_path.
    write_record(cfg.vault, status=RETRY_WAIT, failed_stage="raw",
                 retryable=True, attempts=1,
                 next_attempt_at="2000-01-01T00:00:00Z")   # deadline long past

    ran = []

    def fake_transcribe(rec, cfg, log):
        ran.append(rec.stem)
        rec.advance(TRANSCRIBED, word_count=5)

    monkeypatch.setattr(pl, "stage_transcribe", fake_transcribe)
    # Halt the flow cleanly right after the re-executed stage.
    monkeypatch.setattr(pl, "stage_route", lambda rec, cfg, log: False)

    rec = load_records(cfg.vault)[0]
    assert rec.status == RETRY_WAIT
    git = Git(git_vault.work, "t", "t@t", retries=1, log=lambda m: None)

    pl.process(rec, cfg, git, pl.Log("ERROR"))

    assert ran == [rec.stem], "the failed stage must re-execute on a due retry"
    assert rec.status != RETRY_WAIT


def test_retry_wait_transition_is_committed(cfg, git_vault, monkeypatch):
    import transcribe as stt
    write_record(cfg.vault)                                # a fresh RAW record

    def boom(rec, cfg, log):
        raise stt.TranscriptionError("upstream 503", retryable=True)

    monkeypatch.setattr(pl, "stage_transcribe", boom)

    rec = load_records(cfg.vault)[0]
    git = Git(git_vault.work, "t", "t@t", retries=1, log=lambda m: None)

    ok = pl.process(rec, cfg, git, pl.Log("ERROR"))

    assert ok is False
    assert rec.status == RETRY_WAIT
    assert "retry-wait" in _remote_log(git_vault.remote), \
        "the RETRY_WAIT transition must be pushed, not left local-only"
