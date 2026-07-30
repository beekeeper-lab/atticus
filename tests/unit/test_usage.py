"""Usage accounting, and the api/subscription split it exists to enforce.

The split is the point. The agent runs on the operator's Claude subscription: it
consumes rate-limit quota and bills nothing per token, while the CLI still reports
an imputed `total_cost_usd`. An earlier version of this pipeline treated that
figure as money and told the operator "$8 spent" for usage that cost nothing —
these tests exist so that cannot come back.
"""
import json

import pytest
import usage


def _vault(tmp_path):
    v = tmp_path / "vault"
    (v / ".state").mkdir(parents=True)
    return v


# --- the split ------------------------------------------------------------

def test_subscription_usage_never_counts_as_money(tmp_path):
    v = _vault(tmp_path)
    usage.record(v, kind="agent", billing=usage.SUBSCRIPTION,
                 model="claude-fable-5", usd=99.0, output_tokens=5000)
    assert usage.api_spend(v) == 0.0, "subscription usage must not be money"
    s = usage.summarise(v)
    assert s["api_total_usd"] == 0.0
    assert s["subscription_imputed_usd"] == 99.0


def test_api_usage_does_count(tmp_path):
    v = _vault(tmp_path)
    usage.record(v, kind="transcription", billing=usage.API, usd=0.0012)
    usage.record(v, kind="adjudicator", billing=usage.API, usd=0.0003)
    assert usage.api_spend(v) == pytest.approx(0.0015)


def test_billing_must_be_named_explicitly(tmp_path):
    """No inference. A caller that has to name it cannot file tokens as money."""
    with pytest.raises(ValueError):
        usage.record(_vault(tmp_path), kind="agent", billing="guess", usd=1.0)


# --- budget ---------------------------------------------------------------

def test_budget_exhausts_on_api_spend_only(tmp_path, cfg):
    v = _vault(tmp_path)
    cfg.api_budget_usd = 1.00
    usage.record(v, kind="agent", billing=usage.SUBSCRIPTION, usd=500.0)
    assert usage.budget_state(v, cfg)["exhausted"] is False, \
        "subscription usage must never exhaust the money budget"
    usage.record(v, kind="transcription", billing=usage.API, usd=1.00)
    assert usage.budget_state(v, cfg)["exhausted"] is True


def test_a_zero_budget_disables_the_cap(tmp_path, cfg):
    """Otherwise a blank setting would read as instantly-exhausted and stop the
    pipeline dead — the opposite of 'no limit configured'."""
    v = _vault(tmp_path)
    cfg.api_budget_usd = 0
    usage.record(v, kind="transcription", billing=usage.API, usd=10.0)
    st = usage.budget_state(v, cfg)
    assert st["enabled"] is False and st["exhausted"] is False
    assert st["remaining_usd"] is None


def test_spend_is_scoped_to_the_current_month(tmp_path, cfg):
    v = _vault(tmp_path)
    cfg.api_budget_usd = 1.00
    # A prior month's line, written directly.
    (v / ".state" / "usage-old.jsonl").write_text(json.dumps(
        {"month": "2020-01", "billing": "api", "usd": 99.0, "kind": "transcription"}) + "\n")
    assert usage.api_spend(v) == 0.0
    assert usage.budget_state(v, cfg)["exhausted"] is False


# --- cost arithmetic ------------------------------------------------------

@pytest.mark.parametrize("model,seconds,expected", [
    ("gpt-4o-transcribe", 60, 0.006),
    ("gpt-4o-transcribe", 12, 0.0012),
    ("gpt-4o-mini-transcribe", 60, 0.003),
    ("gpt-4o-transcribe", 0, 0.0),
])
def test_transcription_cost(model, seconds, expected):
    assert usage.transcription_usd(seconds, model) == pytest.approx(expected)


def test_a_months_worth_of_short_commands_fits_the_default_budget():
    """Sanity-check the $4 default against the documented workload: ~20
    recordings a day of 10-30s. If this ever fails, the default is wrong."""
    per_day = 20 * usage.transcription_usd(20, "gpt-4o-transcribe")
    assert per_day * 31 < 4.00


def test_adjudicator_cost_uses_chat_pricing():
    # gpt-4o-mini: $0.15/1M in, $0.60/1M out
    assert usage.chat_usd("gpt-4o-mini", 1_000_000, 0) == pytest.approx(0.15)
    assert usage.chat_usd("gpt-4o-mini", 0, 1_000_000) == pytest.approx(0.60)


# --- parsing the CLI envelope --------------------------------------------

CLAUDE_JSON = {
    "total_cost_usd": 0.289442,
    "num_turns": 3,
    "duration_ms": 4593,
    "usage": {"input_tokens": 2, "output_tokens": 4,
              "cache_creation_input_tokens": 13646,
              "cache_read_input_tokens": 15721,
              "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1}},
    "modelUsage": {
        "claude-haiku-4-5-20251001": {"outputTokens": 12,
                                      "canonicalModel": "claude-haiku-4-5"},
        "claude-fable-5": {"outputTokens": 4000, "canonicalModel": "claude-fable-5"},
    },
}


def test_from_claude_json_extracts_the_fields_we_report():
    u = usage.from_claude_json(CLAUDE_JSON)
    assert u["usd"] == pytest.approx(0.289442)
    assert u["input_tokens"] == 2 and u["output_tokens"] == 4
    assert u["cache_read_tokens"] == 15721
    assert u["cache_write_tokens"] == 13646
    assert u["web_searches"] == 3, "search + fetch requests both count"
    assert u["turns"] == 3


def test_from_claude_json_picks_the_model_that_did_the_work():
    """A run can touch a small routing model plus the main one; report the main."""
    assert usage.from_claude_json(CLAUDE_JSON)["model"] == "claude-fable-5"


def test_from_claude_json_tolerates_an_empty_or_odd_payload():
    for payload in ({}, {"usage": None}, {"modelUsage": {}}):
        u = usage.from_claude_json(payload)
        assert u["usd"] == 0.0 and u["input_tokens"] == 0


# --- durability -----------------------------------------------------------

def test_a_corrupt_ledger_line_does_not_blind_the_report(tmp_path):
    v = _vault(tmp_path)
    usage.record(v, kind="transcription", billing=usage.API, usd=0.01)
    with usage.ledger_path(v).open("a") as f:
        f.write("{truncated write\n")
    usage.record(v, kind="transcription", billing=usage.API, usd=0.02)
    assert usage.api_spend(v) == pytest.approx(0.03)


def test_recording_cannot_break_a_run(tmp_path):
    """An unwritable vault must not fail the pipeline — accounting is secondary
    to actually processing the recording."""
    ev = usage.record(tmp_path / "does-not-exist", kind="transcription",
                      billing=usage.API, usd=0.01, log=lambda m: None)
    assert ev["usd"] == 0.01     # returned, just not persisted


# --- the gate in the pipeline --------------------------------------------

def test_an_exhausted_budget_stops_transcription_non_retryably(tmp_path, cfg,
                                                               monkeypatch):
    """Non-retryable on purpose: a month's budget does not refill on a
    five-minute backoff, so three retries would hit the same wall and burn the
    record's attempt count for nothing."""
    import pipeline
    import transcribe as stt
    from vault import Record

    v = _vault(tmp_path)
    (v / "inbox").mkdir(exist_ok=True)
    cfg.vault = v
    cfg.api_budget_usd = 0.01
    usage.record(v, kind="transcription", billing=usage.API, usd=0.02)

    meta = v / "inbox" / "r.json"
    meta.write_text(json.dumps({"plaud_id": "p", "status": "raw",
                                "audio_filename": "r.mp3"}))
    rec = Record(meta, json.loads(meta.read_text()))

    with pytest.raises(stt.TranscriptionError) as e:
        pipeline.stage_transcribe(rec, cfg, pipeline.Log("ERROR"))
    assert e.value.retryable is False
    assert "budget" in str(e.value).lower()
    assert "ATTICUS_API_BUDGET_USD" in str(e.value), "must name the remedy"


def test_a_healthy_budget_does_not_block_transcription(tmp_path, cfg, monkeypatch):
    """The gate must not be the reason a normal recording fails."""
    import pipeline
    from vault import Record

    v = _vault(tmp_path)
    (v / "inbox").mkdir(exist_ok=True)
    cfg.vault = v
    cfg.api_budget_usd = 4.00

    meta = v / "inbox" / "r.json"
    meta.write_text(json.dumps({"plaud_id": "p", "status": "raw",
                                "audio_filename": "r.mp3"}))
    rec = Record(meta, json.loads(meta.read_text()))

    # Fail *past* the gate, at the audio read — proves the gate let us through.
    # Match the gate's own wording, not the bare word "budget", which also
    # appears in pytest's tmp_path for this test.
    with pytest.raises(Exception) as e:
        pipeline.stage_transcribe(rec, cfg, pipeline.Log("ERROR"))
    assert "API budget for" not in str(e.value)
