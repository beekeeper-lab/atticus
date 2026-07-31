"""pipeline.stage_podcast — the wiring, not the synthesis.

The promise under test is narrow and load-bearing: **no audio outcome may cost a
finished report.** A record reaches this stage in EXECUTED with a good HTML
deliverable already on disk, and must leave it in EXECUTED regardless of what
happens here, carrying an explanation.

This exists because the equivalent guard on the execute stage shipped broken and
passing: `test_budget_exhaustion_is_not_retried` fed the code an error string
nobody emits, so the protection was never exercised against reality. Failure
paths get their own tests here.
"""
import pipeline as pl
import podcast as pod
import pytest
import usage
from conftest import write_record
from vault import EXECUTED, load_records


@pytest.fixture
def rec(cfg):
    """An EXECUTED record with a report on disk, ready for the audio stage."""
    write_record(cfg.vault, status=EXECUTED, executed=True)
    r = load_records(cfg.vault)[0]
    out = r.outdir(cfg.vault)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.html").write_text("<html><body><h1>R</h1></body></html>")
    return r


def _script(rec, cfg, body=None):
    body = body or "# T\n\n**A:** One two three.\n**B:** Four five six.\n"
    (rec.outdir(cfg.vault) / pod.SCRIPT_NAME).write_text(body)


class _Log:
    def __init__(self):
        self.info_lines, self.warn_lines = [], []

    def info(self, m):
        self.info_lines.append(str(m))

    def warn(self, m):
        self.warn_lines.append(str(m))


# ── the no-op case ─────────────────────────────────────────────────────────
def test_no_script_touches_nothing(rec, cfg, monkeypatch):
    """The overwhelmingly common path: audio was never asked for."""
    monkeypatch.setattr(pod, "generate", lambda *a, **k: pytest.fail("must not run"))
    log = _Log()
    pl.stage_podcast(rec, cfg, log)
    assert "podcast" not in rec.data, "a silent no-op must not annotate the record"
    assert log.warn_lines == []


def test_dry_run_does_not_spend(rec, cfg, monkeypatch):
    _script(rec, cfg)
    monkeypatch.setattr(pod, "generate", lambda *a, **k: pytest.fail("must not run"))
    pl.stage_podcast(rec, cfg, _Log(), dry_run=True)


# ── failure containment ────────────────────────────────────────────────────
def test_a_tts_failure_leaves_the_record_executed(rec, cfg, monkeypatch):
    _script(rec, cfg)
    monkeypatch.setattr(pod, "generate",
                        lambda *a, **k: {"made": False, "reason": "upstream 503"})
    log = _Log()
    pl.stage_podcast(rec, cfg, log)
    assert rec.status == EXECUTED, "audio must never move the record backwards"
    assert rec.data["podcast"]["reason"] == "upstream 503"
    assert any("503" in w for w in log.warn_lines), "a failure must be loud"


def test_an_unexpected_exception_is_contained_and_named(rec, cfg, monkeypatch):
    """A bug in podcast.py must not quarantine a good report — but it must also
    not vanish. Both halves matter."""
    def boom(*a, **k):
        raise ValueError("bad regex")
    monkeypatch.setattr(pod, "generate", boom)
    _script(rec, cfg)
    log = _Log()
    pl.stage_podcast(rec, cfg, log)          # must not raise
    assert rec.status == EXECUTED
    assert "ValueError" in rec.data["podcast"]["reason"]
    assert any("ValueError" in w for w in log.warn_lines)


def test_an_exhausted_monthly_budget_skips_audio_without_failing(rec, cfg,
                                                                monkeypatch):
    """TTS is real money on the same monthly budget as transcription. An exhausted
    month must cost the episode, not the report."""
    _script(rec, cfg)
    monkeypatch.setattr(pod, "generate", lambda *a, **k: pytest.fail("must not spend"))
    monkeypatch.setattr(usage, "budget_state",
                        lambda v, c: {"exhausted": True, "spent": 4.20})
    log = _Log()
    pl.stage_podcast(rec, cfg, log)
    assert rec.status == EXECUTED
    assert "budget" in rec.data["podcast"]["reason"]
    assert any("budget" in w for w in log.warn_lines)


# ── the success case, and what it must record ──────────────────────────────
def test_success_records_measured_cost_as_real_money(rec, cfg, monkeypatch):
    """Two things are easy to get wrong here and both matter for accounting:
    TTS is billing=api (unlike the agent, which is subscription), and the ledger
    must carry the MEASURED figure, not the pre-flight estimate."""
    _script(rec, cfg)
    monkeypatch.setattr(pod, "generate", lambda *a, **k: {
        "made": True, "reason": "ok", "audio": "podcast.mp3", "bytes": 1234,
        "seconds": 12.1, "usd": 0.003018, "estimated_usd": 0.002214,
        "turns": 2, "chars": 124, "report": "report.html",
    })
    pl.stage_podcast(rec, cfg, _Log())

    events = usage.load(cfg.vault)
    tts = [e for e in events if e["kind"] == "tts"]
    assert len(tts) == 1
    e = tts[0]
    assert e["billing"] == usage.API, "TTS is real money, not subscription"
    assert e["usd"] == pytest.approx(0.003018), "must log measured, not estimated"
    assert e["estimated_usd"] == pytest.approx(0.002214), "keep the estimate visible"
    assert e["audio_seconds"] == pytest.approx(12.1)
    assert e["stem"] == rec.stem


def test_tts_spend_counts_against_the_monthly_api_budget(rec, cfg, monkeypatch):
    """The whole point of filing it as billing=api: it has to be able to exhaust
    the same ceiling transcription does."""
    _script(rec, cfg)
    before = usage.api_spend(cfg.vault)
    monkeypatch.setattr(pod, "generate", lambda *a, **k: {
        "made": True, "reason": "ok", "audio": "a.mp3", "bytes": 1, "seconds": 60.0,
        "usd": 0.25, "estimated_usd": 0.24, "turns": 2, "chars": 9,
        "report": "report.html"})
    pl.stage_podcast(rec, cfg, _Log())
    assert usage.api_spend(cfg.vault) == pytest.approx(before + 0.25)
