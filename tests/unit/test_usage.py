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


# --- budget threshold alerts ---------------------------------------------

def _spend(v, amount):
    usage.record(v, kind="transcription", billing=usage.API, usd=amount)


def test_thresholds_fire_as_spend_passes_them(tmp_path, cfg):
    v = _vault(tmp_path)
    cfg.api_budget_usd = 4.00
    cfg.budget_alert_usd = [2.00, 3.00, 4.00]

    assert usage.newly_crossed(v, cfg) == []
    _spend(v, 2.10)
    assert usage.newly_crossed(v, cfg) == [2.00]


def test_a_threshold_is_announced_exactly_once(tmp_path, cfg):
    """The property that matters. Spend stays over a threshold for the rest of the
    month, so anything time-based would re-announce it every 5-minute pass."""
    v = _vault(tmp_path)
    cfg.api_budget_usd = 4.00
    cfg.budget_alert_usd = [2.00, 3.00, 4.00]
    _spend(v, 2.50)

    assert usage.newly_crossed(v, cfg) == [2.00]
    usage.mark_alerted(v, 2.00, 2.50)
    assert usage.newly_crossed(v, cfg) == []
    _spend(v, 0.10)                     # more spend, still under $3
    assert usage.newly_crossed(v, cfg) == []


def test_a_jump_past_two_thresholds_announces_both_in_order(tmp_path, cfg):
    """A single expensive recording must not silently skip a warning."""
    v = _vault(tmp_path)
    cfg.api_budget_usd = 4.00
    cfg.budget_alert_usd = [2.00, 3.00, 4.00]
    _spend(v, 3.50)
    assert usage.newly_crossed(v, cfg) == [2.00, 3.00]


def test_markers_do_not_count_as_spend_or_pollute_the_report(tmp_path, cfg):
    v = _vault(tmp_path)
    cfg.api_budget_usd = 4.00
    cfg.budget_alert_usd = [2.00]
    _spend(v, 2.00)
    before = usage.api_spend(v)
    usage.mark_alerted(v, 2.00, 2.00)

    assert usage.api_spend(v) == before, "a marker is not money"
    s = usage.summarise(v)
    assert "budget-alert" not in s["api"]
    assert "budget-alert" not in s["subscription"]
    assert s["events"] == 1, "only the transcription is a consumption event"


def test_subscription_usage_never_trips_a_threshold(tmp_path, cfg):
    v = _vault(tmp_path)
    cfg.api_budget_usd = 4.00
    cfg.budget_alert_usd = [2.00, 3.00, 4.00]
    usage.record(v, kind="agent", billing=usage.SUBSCRIPTION, usd=500.0)
    assert usage.newly_crossed(v, cfg) == []


def test_no_thresholds_configured_means_no_alerts(tmp_path, cfg):
    v = _vault(tmp_path)
    cfg.api_budget_usd = 4.00
    cfg.budget_alert_usd = []
    _spend(v, 99.0)
    assert usage.newly_crossed(v, cfg) == []


def test_thresholds_reset_next_month(tmp_path, cfg):
    """Last month's announcement must not silence this month's crossing."""
    v = _vault(tmp_path)
    cfg.api_budget_usd = 4.00
    cfg.budget_alert_usd = [2.00]
    (v / ".state" / "usage-old.jsonl").write_text(json.dumps(
        {"month": "2020-01", "billing": usage.META, "kind": "budget-alert",
         "threshold_usd": 2.00, "usd": 0}) + "\n")
    _spend(v, 2.00)
    assert usage.newly_crossed(v, cfg) == [2.00]


def test_the_final_threshold_reports_that_transcription_stopped(tmp_path, cfg,
                                                               monkeypatch):
    """The $4 alert must say what actually happened, not just quote a number."""
    import pipeline
    sent = []
    monkeypatch.setattr(pipeline, "notify",
                        lambda cfg, text, log, **kw: sent.append((text, kw)))
    v = _vault(tmp_path)
    cfg.vault = v
    cfg.api_budget_usd = 4.00
    cfg.budget_alert_usd = [2.00, 4.00]
    _spend(v, 4.00)

    pipeline._alarm_budget_thresholds(cfg, pipeline.Log("ERROR"))
    assert len(sent) == 2, "both thresholds announced"
    final_text, final_kw = sent[-1]
    assert "STOPPED" in final_text
    assert "ATTICUS_API_BUDGET_USD" in final_text, "must name the remedy"
    assert final_kw["priority"] == "high"
    # And it is not announced again on the next pass.
    sent.clear()
    pipeline._alarm_budget_thresholds(cfg, pipeline.Log("ERROR"))
    assert sent == []


def test_a_warning_threshold_says_the_agent_is_not_counted(tmp_path, cfg,
                                                           monkeypatch):
    """The distinction is the whole point of the feature — say it in the push."""
    import pipeline
    sent = []
    monkeypatch.setattr(pipeline, "notify",
                        lambda cfg, text, log, **kw: sent.append((text, kw)))
    v = _vault(tmp_path)
    cfg.vault = v
    cfg.api_budget_usd = 4.00
    cfg.budget_alert_usd = [2.00]
    _spend(v, 2.00)

    pipeline._alarm_budget_thresholds(cfg, pipeline.Log("ERROR"))
    text, kw = sent[0]
    assert "subscription" in text.lower()
    assert kw["priority"] != "high", "a warning is not an emergency"


def test_alerts_go_through_the_REAL_notify_path(tmp_path, cfg, monkeypatch):
    """Regression: the first version of this feature never delivered anything.

    pipeline.notify() hardcoded title=, so passing a custom title raised
    "got multiple values for keyword argument 'title'" — swallowed by
    _alarm_budget_thresholds' own except and visible only as a log line. Every
    unit test passed because they all monkeypatched pipeline.notify and so never
    exercised its signature. This one patches the HTTP layer instead, leaving the
    real call chain intact.
    """
    import urllib.request

    import notify as nt
    import pipeline

    # notify() imports urllib.request inside the function, so patch the module
    # itself rather than an attribute on notify.
    posted = []
    monkeypatch.setattr(nt, "STATE", tmp_path / "stamps")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: posted.append(req) or _Resp())

    v = _vault(tmp_path)
    cfg.vault = v
    cfg.notify_url = "https://ntfy.example/atticus"
    cfg.api_budget_usd = 4.00
    cfg.budget_alert_usd = [2.00]
    _spend(v, 2.00)

    pipeline._alarm_budget_thresholds(cfg, pipeline.Log("ERROR"))
    assert len(posted) == 1, "the alert never reached the transport"
    assert posted[0].get_header("Title")           # header actually set
    assert usage.newly_crossed(v, cfg) == [], "and it was marked as announced"


class _Resp:
    """Minimal stand-in for urlopen's context-manager response."""
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_a_custom_title_no_longer_collides(cfg):
    """The narrow fix, pinned on its own so it cannot silently regress."""
    import notify as nt
    import pipeline
    seen = {}
    orig = nt.notify
    try:
        nt.notify = lambda cfg, text, **kw: seen.update(kw) or True
        pipeline._notify = nt.notify
        pipeline.notify(cfg, "x", pipeline.Log("ERROR"), title="Custom")
        assert seen["title"] == "Custom"
        seen.clear()
        pipeline.notify(cfg, "x", pipeline.Log("ERROR"))
        assert seen["title"] == "Atticus processor", "default still applies"
    finally:
        nt.notify = orig
        pipeline._notify = orig
