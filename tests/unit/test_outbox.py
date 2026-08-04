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


# ── the per-verb override ──────────────────────────────────────────────────
# The three classes alone are too coarse. Observed while building the Outlook
# handlers: ATTICUS_OUTBOX_TRACKED=auto, which an operator would set so GitHub
# issues can flow, ALSO opens outlook.event — calendar invites to other people.
# Without an override the only way to open the verb you want is to open several you
# do not, which is an incentive to over-grant.

def test_a_verb_can_be_opened_without_opening_its_class(out, cfg):
    calls = _spy("github.issue", risk=outbox.TRACKED, schema=("to",))
    cfg.outbox_verbs = {"github.issue": "auto"}
    _req(out, "001-github.issue.json", verb="github.issue", to="x")
    res = outbox.process(out, cfg, log=lambda m: None)
    assert len(calls) == 1 and res["done"] == 1
    assert outbox.gate(cfg, outbox.TRACKED, "outlook.event") == "confirm", \
        "opening one verb must not open its whole class"


def test_a_verb_can_be_closed_while_its_class_is_open(out, cfg):
    """The other direction matters just as much: open tracked broadly, keep one
    dangerous member shut."""
    calls = _spy("outlook.event", risk=outbox.TRACKED, schema=("to",))
    cfg.outbox_tracked = "auto"
    cfg.outbox_verbs = {"outlook.event": "confirm"}
    _req(out, "001-outlook.event.json", verb="outlook.event", to="x")
    res = outbox.process(out, cfg, log=lambda m: None)
    assert not calls and res["receipts"][0]["status"] == "held"


def test_outbox_off_still_wins_over_a_per_verb_override(out, cfg):
    """Its whole purpose is a global stop, so nothing may override it."""
    calls = _spy(risk=outbox.INTERNAL)
    cfg.outbox = "off"
    cfg.outbox_verbs = {"test.do": "auto"}
    _req(out, "001-test.do.json", verb="test.do", to="x")
    outbox.process(out, cfg, log=lambda m: None)
    assert not calls


def test_the_held_message_names_both_ways_to_open_it(out, cfg):
    """An operator reading this should not have to guess which knob to turn, and
    should be told about the narrow one — not just the class-wide one."""
    _spy("signal.send", risk=outbox.OUTWARD, schema=("to",))
    _req(out, "001-signal.send.json", verb="signal.send", to="x")
    res = outbox.process(out, cfg, log=lambda m: None)
    reason = res["receipts"][0]["reason"]
    assert "ATTICUS_OUTBOX_OUTWARD=auto" in reason
    assert "ATTICUS_OUTBOX_VERB_SIGNAL_SEND=auto" in reason


# ── the receipt in the report ──────────────────────────────────────────────
# process() runs AFTER the agent exits, so the agent cannot know what happened.
# Without this the skills had to write "pending" and the report said pending
# forever, including long after the action succeeded.

def _report(out, body="<html><body><h1>R</h1><p>keep me</p></body></html>"):
    (out / "report.html").write_text(body)
    return out / "report.html"


def test_the_outcome_is_injected_into_the_report(out, cfg):
    _spy(risk=outbox.INTERNAL)
    page = _report(out)
    _req(out, "001-test.do.json", verb="test.do", to="milk")
    res = outbox.process(out, cfg, log=lambda m: None)
    assert res["injected"] is True
    text = page.read_text()
    assert "atticus-outbox" in text
    assert "do the thing to milk" in text
    assert "keep me" in text, "the report itself must survive"
    assert text.index("atticus-outbox") < text.index("<h1>")


def test_a_held_action_says_so_in_the_report(out, cfg):
    """The operator must be able to tell "waiting for you" from "done" by reading
    the document, not by opening a JSON sidecar."""
    _spy(risk=outbox.OUTWARD)
    page = _report(out)
    _req(out, "001-test.do.json", verb="test.do", to="Robbie")
    outbox.process(out, cfg, log=lambda m: None)
    assert "Waiting for you" in page.read_text()


def test_a_url_from_a_handler_becomes_a_link(out, cfg):
    @outbox.handler("t.u", risk=outbox.INTERNAL, schema=(),
                    describe=lambda r: "filed a ticket")
    def _h(req, cfg, log=print):
        return {"id": "1234", "url": "https://dev.azure.test/x/_workitems/edit/1234"}
    page = _report(out)
    _req(out, "001-t.u.json", verb="t.u")
    outbox.process(out, cfg, log=lambda m: None)
    assert 'href="https://dev.azure.test/x/_workitems/edit/1234"' in page.read_text()


def test_re_running_replaces_the_receipt_rather_than_stacking(out, cfg):
    _spy(risk=outbox.INTERNAL)
    page = _report(out)
    _req(out, "001-test.do.json", verb="test.do", to="milk")
    outbox.process(out, cfg, log=lambda m: None)
    outbox.process(out, cfg, log=lambda m: None)
    assert page.read_text().count('<div class="atticus-outbox">') == 1


def test_no_report_is_not_an_error(out, cfg):
    """A record can legitimately produce no HTML."""
    _spy(risk=outbox.INTERNAL)
    _req(out, "001-test.do.json", verb="test.do", to="x")
    res = outbox.process(out, cfg, log=lambda m: None)
    assert res["done"] == 1 and res["injected"] is False


def test_a_handler_reason_is_escaped_not_injected(out, cfg):
    """A reason can carry an API error body. It lands in HTML, so it is escaped."""
    _spy(risk=outbox.INTERNAL,
         boom=outbox.OutboxError('<script>alert("x")</script>'))
    page = _report(out)
    _req(out, "001-test.do.json", verb="test.do", to="x")
    outbox.process(out, cfg, log=lambda m: None)
    text = page.read_text()
    assert "<script>alert" not in text
    assert "&lt;script&gt;" in text


def test_the_receipt_carries_no_colour_of_its_own():
    """It is injected into a report somebody else styled, and that report may be
    dark, light, or its own invention. Every colour here is a grey rgba() or
    inherited ink for that reason — a hex would be right on exactly one theme.

    This is not hypothetical: the vault's own palette leaked into agent reports
    and produced 1.16:1 white-on-white, and the receipt was the block it was most
    visible in."""
    html = outbox.receipt_html([{"status": "done", "summary": "x",
                                 "url": "https://example.test/1"}])
    style = html[html.index("<style>"):html.index("</style>")]
    import re
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", style)
    assert not hexes, f"receipt CSS hard-codes {hexes}; it must stay theme-neutral"


def test_a_receipt_link_inherits_the_page_ink():
    """An unstyled <a> takes the browser default #0000EE, which measured 2.01:1
    on a dark agent report. Inheriting makes it exactly as readable as the text
    beside it, whatever the page is."""
    html = outbox.receipt_html([{"status": "done", "summary": "filed",
                                 "url": "https://example.test/1"}])
    assert ".atticus-outbox a{color:inherit" in html
    assert "https://example.test/1" in html
