"""The outbox contract (#42) — how a sandboxed agent causes external actions.

Every credentialed skill goes through here, so these tests are the contract. The
properties that matter are all about refusal: an unknown verb must be reported
rather than dropped, anything outward-facing must default to held, fan-out must be
bounded, and no handler failure may cost the report the agent already wrote.
"""
import json

import outbox
import pytest


@pytest.fixture(autouse=True)
def _clean_registry():
    """Tests register their own verbs; the real handlers are irrelevant here."""
    saved = dict(outbox._HANDLERS)
    outbox._HANDLERS.clear()
    yield
    outbox._HANDLERS.clear()
    outbox._HANDLERS.update(saved)


@pytest.fixture
def out(tmp_path):
    d = tmp_path / "output"
    (d / "outbox").mkdir(parents=True)
    return d


def _req(out, name, **body):
    (out / "outbox" / name).write_text(json.dumps(body))


def _spy(verb="test.do", risk=outbox.INTERNAL, schema=("to",), boom=None):
    calls = []

    @outbox.handler(verb, risk=risk, schema=schema,
                    describe=lambda r: f"do the thing to {r.get('to')}")
    def _h(req, cfg, log=print):
        if boom:
            raise boom
        calls.append(req)
        return {"id": "xyz"}
    return calls


# ── reading and ordering ───────────────────────────────────────────────────
def test_requests_run_in_filename_order(out, cfg):
    """"File the ticket then tell Robbie about it" has a sequence, and the agent
    controls it through the zero-padded prefix."""
    calls = _spy()
    _req(out, "002-test.do.json", verb="test.do", to="second")
    _req(out, "001-test.do.json", verb="test.do", to="first")
    _req(out, "010-test.do.json", verb="test.do", to="third")
    outbox.process(out, cfg, log=lambda m: None)
    assert [c["to"] for c in calls] == ["first", "second", "third"]


def test_a_misnamed_file_is_reported_and_skipped(out, cfg):
    calls = _spy()
    _req(out, "001-test.do.json", verb="test.do", to="ok")
    (out / "outbox" / "notes.json").write_text('{"verb":"test.do","to":"x"}')
    said = []
    outbox.process(out, cfg, log=said.append)
    assert len(calls) == 1
    assert any("notes.json" in s for s in said)


def test_malformed_json_does_not_silence_a_good_request_beside_it(out, cfg):
    calls = _spy()
    (out / "outbox" / "001-test.do.json").write_text("{not json")
    _req(out, "002-test.do.json", verb="test.do", to="ok")
    outbox.process(out, cfg, log=lambda m: None)
    assert [c["to"] for c in calls] == ["ok"]


def test_no_outbox_directory_is_the_normal_case(out, cfg):
    import shutil
    shutil.rmtree(out / "outbox")
    assert outbox.process(out, cfg, log=lambda m: None)["requests"] == 0


# ── refusal ────────────────────────────────────────────────────────────────
def test_an_unknown_verb_is_refused_by_name_not_dropped(out, cfg):
    """Silently dropping it would leave the operator believing a message was sent.
    The refusal has to name the verb and say what is known."""
    _spy("test.do")
    _req(out, "001-signal.send.json", verb="signal.send", to="Robbie")
    said = []
    res = outbox.process(out, cfg, log=said.append)
    assert res["refused"] == 1 and res["done"] == 0
    r = res["receipts"][0]
    assert "unknown verb" in r["reason"] and "signal.send" in r["reason"]
    assert "test.do" in r["reason"], "must say what IS known"


def test_a_request_missing_a_required_field_is_refused(out, cfg):
    _spy(schema=("to", "body"))
    _req(out, "001-test.do.json", verb="test.do", to="Robbie")
    res = outbox.process(out, cfg, log=lambda m: None)
    assert res["refused"] == 1
    assert "body" in res["receipts"][0]["reason"]


def test_a_request_with_no_verb_is_refused(out, cfg):
    _spy()
    _req(out, "001-nothing.json", to="Robbie")
    assert outbox.process(out, cfg, log=lambda m: None)["refused"] == 1


# ── the gate ───────────────────────────────────────────────────────────────
def test_outward_actions_are_held_by_default(out, cfg):
    """The instruction originates in a microphone worn in public, and a message to
    a person cannot be taken back. Held is the default and must stay that way."""
    calls = _spy(risk=outbox.OUTWARD)
    _req(out, "001-test.do.json", verb="test.do", to="Robbie")
    res = outbox.process(out, cfg, log=lambda m: None)
    assert not calls, "an outward action must not be performed unattended"
    assert res["receipts"][0]["status"] == "held"
    assert "confirmation" in res["receipts"][0]["reason"]


def test_tracked_actions_are_held_by_default(out, cfg):
    calls = _spy(risk=outbox.TRACKED)
    _req(out, "001-test.do.json", verb="test.do", to="x")
    outbox.process(out, cfg, log=lambda m: None)
    assert not calls


def test_internal_actions_run_unattended(out, cfg):
    """A todo or a reminder only you can see, trivially undone — gating it would
    make the feature useless without buying anything."""
    calls = _spy(risk=outbox.INTERNAL)
    _req(out, "001-test.do.json", verb="test.do", to="milk")
    res = outbox.process(out, cfg, log=lambda m: None)
    assert len(calls) == 1 and res["done"] == 1


def test_outbox_off_records_intent_and_performs_nothing(out, cfg):
    """Also how a new handler is tested safely."""
    calls = _spy(risk=outbox.INTERNAL)
    cfg.outbox = "off"
    _req(out, "001-test.do.json", verb="test.do", to="x")
    res = outbox.process(out, cfg, log=lambda m: None)
    assert not calls
    assert res["receipts"][0]["status"] == "held"
    assert "off" in res["receipts"][0]["reason"]


def test_a_risk_class_can_be_opened_independently(out, cfg):
    calls = _spy(risk=outbox.OUTWARD)
    cfg.outbox_outward = "auto"
    _req(out, "001-test.do.json", verb="test.do", to="Robbie")
    outbox.process(out, cfg, log=lambda m: None)
    assert len(calls) == 1
    assert outbox.gate(cfg, outbox.TRACKED) == "confirm", "others stay closed"


# ── bounds and containment ─────────────────────────────────────────────────
def test_the_per_pass_cap_bounds_fan_out(out, cfg):
    """One misheard sentence must not be able to send thirty messages."""
    calls = _spy(risk=outbox.INTERNAL)
    cfg.outbox_max_actions = 2
    for i in range(6):
        _req(out, f"{i:03d}-test.do.json", verb="test.do", to=f"n{i}")
    res = outbox.process(out, cfg, log=lambda m: None)
    assert len(calls) == 2 and res["done"] == 2 and res["refused"] == 4
    assert "cap" in res["receipts"][-1]["reason"]


def test_a_handler_exception_is_contained_and_named(out, cfg):
    """A handler bug must not cost the report the agent already wrote."""
    _spy(risk=outbox.INTERNAL, boom=ValueError("bad token"))
    _req(out, "001-test.do.json", verb="test.do", to="x")
    res = outbox.process(out, cfg, log=lambda m: None)   # must not raise
    assert res["failed"] == 1
    assert "ValueError" in res["receipts"][0]["reason"]


def test_an_outbox_error_reports_its_own_message(out, cfg):
    _spy(risk=outbox.INTERNAL,
         boom=outbox.OutboxError("SIGNAL_NUMBER is not configured"))
    _req(out, "001-test.do.json", verb="test.do", to="x")
    res = outbox.process(out, cfg, log=lambda m: None)
    assert res["failed"] == 1
    assert res["receipts"][0]["reason"] == "SIGNAL_NUMBER is not configured"


# ── the receipt ────────────────────────────────────────────────────────────
def test_a_receipt_is_written_beside_the_deliverable(out, cfg):
    _spy(risk=outbox.INTERNAL)
    _req(out, "001-test.do.json", verb="test.do", to="x")
    outbox.process(out, cfg, log=lambda m: None, stem="stem-1")
    receipt = json.loads((out / "outbox-receipt.json").read_text())
    assert receipt["stem"] == "stem-1" and receipt["done"] == 1
    assert receipt["receipts"][0]["summary"] == "do the thing to x"
    assert receipt["receipts"][0]["id"] == "xyz", "handler result is kept"


def test_the_summary_comes_from_the_handler_not_the_verb(out, cfg):
    """It is what the operator reads in a confirmation, so it has to be specific."""
    _spy(risk=outbox.INTERNAL)
    _req(out, "001-test.do.json", verb="test.do", to="Robbie Page")
    assert outbox.describe({"verb": "test.do", "to": "Robbie Page"}) \
        == "do the thing to Robbie Page"


# ── registration ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["send", "Signal.send", "signal-send",
                                 "signal.", ".send", "signal.send.now"])
def test_a_malformed_verb_cannot_be_registered(bad):
    with pytest.raises(ValueError, match="service.action"):
        outbox.handler(bad, risk=outbox.INTERNAL)(lambda *a, **k: None)


def test_an_unknown_risk_class_cannot_be_registered():
    with pytest.raises(ValueError, match="risk must be"):
        outbox.handler("x.y", risk="mild")(lambda *a, **k: None)


def test_the_contract_text_states_the_rules_a_skill_must_follow():
    """Ten skills paste this. If it stops saying these things they will drift."""
    c = outbox.CONTRACT
    for must in ("output/outbox/", "NNN-verb.json", "One action per file",
                 "held for confirmation", "unknown verb"):
        assert must in c, must
