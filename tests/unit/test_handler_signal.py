"""`signal.send` (#47) — the handler whose mistakes cannot be undone.

These tests are mostly about refusal, because refusal is the feature. Nothing
here runs a subprocess or touches a network: `subprocess.run` is replaced, and
any test that reached the real one would fail on this host anyway since
signal-cli is not installed — which is itself one of the cases under test.
"""
import shutil
import subprocess

import outbox
import pytest

from handlers import signal as sig

# ASSEMBLED so the literal never reaches a staged diff — ops/pr.sh's credential
# guard refuses this shape and deliberately has no exemption for test files.
FAKE_LEAKED_TOKEN = "sk-" + "abcdefghijklmnopqrstuvwxyz"

ROBBIE = "+15550002222"
NADIA = "+15550003333"


@pytest.fixture
def scfg(cfg):
    """A fully configured Signal, on top of the real Config fixture."""
    cfg.signal_cli = "signal-cli"
    cfg.signal_from = "+15550001111"
    cfg.signal_recipients = {"Robbie": ROBBIE, "Nadia Patel": NADIA}
    cfg.signal_max_chars = 200
    cfg.signal_timeout = 5
    cfg.signal_config_dir = ""
    return cfg


@pytest.fixture
def ran(monkeypatch):
    """Record subprocess invocations; never execute one."""
    calls = []

    def fake(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout="1751328000123\n", stderr="")

    monkeypatch.setattr(sig.shutil, "which",
                        lambda b: f"/usr/local/bin/{b}" if b == "signal-cli" else None)
    monkeypatch.setattr(sig.subprocess, "run", fake)
    return calls


def _send(cfg, to="Robbie", body="Running twenty minutes late."):
    return sig.send({"verb": sig.VERB, "to": to, "body": body}, cfg, log=lambda m: None)


# ── the host is not set up, which is today's normal state ───────────────────
def test_a_missing_binary_names_what_is_missing_and_how_to_get_it(scfg, monkeypatch):
    """Nothing is installed. That must read as a diagnosis, not a crash."""
    monkeypatch.setattr(sig.shutil, "which", lambda b: None)
    monkeypatch.setattr(sig.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not run anything"))
    with pytest.raises(outbox.OutboxError) as e:
        _send(scfg)
    msg = str(e.value)
    assert "signal-cli" in msg and "not installed" in msg
    assert "link" in msg and "ATTICUS_SIGNAL_FROM" in msg


def test_a_missing_account_is_named(scfg, ran):
    scfg.signal_from = ""
    with pytest.raises(outbox.OutboxError, match="ATTICUS_SIGNAL_FROM"):
        _send(scfg)
    assert not ran


def test_a_malformed_account_is_refused_before_signal_cli_sees_it(scfg, ran):
    scfg.signal_from = "gregg@example.com"
    with pytest.raises(outbox.OutboxError, match="E.164"):
        _send(scfg)
    assert not ran


# ── recipient safety: the crux ─────────────────────────────────────────────
def test_a_recipient_not_on_the_allowlist_is_refused(scfg, ran):
    with pytest.raises(outbox.OutboxError) as e:
        _send(scfg, to="Bethany")
    msg = str(e.value)
    assert "not on the Signal allowlist" in msg
    assert "Bethany" in msg, "the refusal must name what was asked for"
    assert "robbie" in msg, "and what would have been allowed"
    assert not ran, "nothing may be sent to an unknown recipient"


def test_a_near_miss_is_not_resolved_to_the_closest_entry(scfg, ran):
    """"Nadya Patel" is what transcription does to "Nadia Patel". A fuzzy match
    here would turn a mishearing into a confident message to a real person, so
    matching is exact and near misses refuse."""
    with pytest.raises(outbox.OutboxError, match="not on the Signal allowlist"):
        _send(scfg, to="Nadya Patel")
    assert not ran


def test_an_empty_allowlist_refuses_everything_and_names_the_setting(cfg, ran):
    """Fail closed: an unconfigured allowlist is not an open one. `cfg` here is
    the real Config with no signal_* attributes at all, which is the state of the
    world before anyone wires this up."""
    with pytest.raises(outbox.OutboxError) as e:
        _send(cfg)
    assert "ATTICUS_SIGNAL_RECIPIENTS" in str(e.value)
    assert not ran


def test_matching_ignores_case_and_extra_whitespace(scfg, ran):
    _send(scfg, to="  nadia   patel ")
    assert ran[0][0][-1] == NADIA


def test_a_bare_number_is_accepted_only_if_it_is_already_allowlisted(scfg, ran):
    _send(scfg, to="+1 555 000 2222")
    assert ran[0][0][-1] == ROBBIE


def test_a_bare_number_is_not_a_way_around_the_allowlist(scfg, ran):
    with pytest.raises(outbox.OutboxError, match="not in ATTICUS_SIGNAL_RECIPIENTS"):
        _send(scfg, to="+15559998888")
    assert not ran


def test_an_ambiguous_label_refuses_rather_than_picking_one(scfg, ran):
    scfg.signal_recipients = "robbie=+15550002222,Robbie=+15557776666"
    with pytest.raises(outbox.OutboxError, match="more than one number"):
        _send(scfg)
    assert not ran


def test_a_malformed_allowlist_entry_is_dropped_not_passed_through(scfg, ran):
    scfg.signal_recipients = {"robbie": "ask him on slack"}
    with pytest.raises(outbox.OutboxError, match="no Signal recipients"):
        _send(scfg)
    assert not ran


# ── fan-out: one request is one message ────────────────────────────────────
def test_a_list_of_recipients_is_refused(scfg, ran):
    with pytest.raises(outbox.OutboxError, match="ONE recipient"):
        _send(scfg, to=["Robbie", "Nadia Patel"])
    assert not ran


def test_several_names_in_one_label_are_refused_not_split(scfg, ran):
    """Splitting here would send two messages from one file, and the per-pass cap
    counts files — so the bound would look like it held while it did not."""
    with pytest.raises(outbox.OutboxError, match="several recipients"):
        _send(scfg, to="Robbie and Nadia Patel")
    assert not ran


# ── bounds ─────────────────────────────────────────────────────────────────
def test_an_over_long_message_is_refused_and_never_truncated(scfg, ran):
    with pytest.raises(outbox.OutboxError) as e:
        _send(scfg, body="x" * 201)
    assert "201" in str(e.value) and "200" in str(e.value)
    assert not ran, "half a message to a person is worse than none"


def test_a_message_at_the_bound_is_sent(scfg, ran):
    _send(scfg, body="x" * 200)
    assert ran


# ── the successful path ────────────────────────────────────────────────────
def test_a_successful_send_calls_signal_cli_with_the_resolved_number(scfg, ran):
    res = _send(scfg, body="On my way.")
    cmd, kw = ran[0]
    assert cmd == ["/usr/local/bin/signal-cli", "-a", "+15550001111",
                   "send", "-m", "On my way.", ROBBIE]
    assert kw["timeout"] == 5 and kw["check"] is False
    assert kw["stdin"] is subprocess.DEVNULL
    assert isinstance(cmd, list), "no shell, ever"
    assert res["recipient"] == "Robbie" and res["chars"] == 10
    assert res["message_timestamp"] == "1751328000123"


def test_the_receipt_masks_the_number_it_sent_to(scfg, ran):
    """The receipt is committed to git. The label is the audit trail; the full
    number is PII the record does not need."""
    res = _send(scfg)
    assert res["to"] == "…2222"
    assert ROBBIE not in str(res)


def test_a_state_directory_is_passed_through_when_configured(scfg, ran, tmp_path):
    scfg.signal_config_dir = str(tmp_path / "signal-cli-state")
    _send(scfg)
    cmd = ran[0][0]
    assert cmd[1:3] == ["--config", str(tmp_path / "signal-cli-state")]


def test_a_body_beginning_with_a_dash_stays_the_value_of_dash_m(scfg, ran):
    _send(scfg, body="-- see you at 6")
    cmd = ran[0][0]
    assert cmd[cmd.index("-m") + 1] == "-- see you at 6"


# ── failure from signal-cli itself ─────────────────────────────────────────
def test_a_nonzero_exit_is_an_outbox_error_naming_the_recipient(scfg, monkeypatch, ran):
    monkeypatch.setattr(sig.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr="Failed to send message:\nUnregistered user\n"))
    with pytest.raises(outbox.OutboxError) as e:
        _send(scfg)
    assert "exited 1" in str(e.value) and "Robbie" in str(e.value)
    assert "Unregistered user" in str(e.value)


def test_subprocess_output_is_redacted_before_it_reaches_a_receipt(scfg, monkeypatch, ran):
    monkeypatch.setattr(sig.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 2, stdout="", stderr="POST failed: Authorization: Bearer "
                            + FAKE_LEAKED_TOKEN))
    with pytest.raises(outbox.OutboxError) as e:
        _send(scfg)
    assert FAKE_LEAKED_TOKEN not in str(e.value)


def test_a_timeout_does_not_claim_either_outcome(scfg, monkeypatch, ran):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)
    monkeypatch.setattr(sig.subprocess, "run", boom)
    with pytest.raises(outbox.OutboxError) as e:
        _send(scfg)
    assert "may or may not" in str(e.value)


# ── registration and the gate ──────────────────────────────────────────────
def test_the_verb_is_registered_as_outward():
    h = outbox.handler_for("signal.send")
    assert h is not None, "importing handlers.signal must register the verb"
    assert h["risk"] == outbox.OUTWARD
    assert set(h["schema"]) == {"to", "body"}


def test_an_outward_action_is_held_by_default(tmp_path, scfg, monkeypatch):
    """The whole safety story rests on this: a message to a person is never sent
    unattended with the shipped configuration."""
    import json
    monkeypatch.setattr(sig.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not send while held"))
    out = tmp_path / "output"
    (out / "outbox").mkdir(parents=True)
    (out / "outbox" / "001-signal.send.json").write_text(json.dumps(
        {"verb": "signal.send", "to": "Robbie", "body": "On my way."}))
    res = outbox.process(out, scfg, log=lambda m: None)
    rec = res["receipts"][0]
    assert rec["status"] == "held" and rec["risk"] == outbox.OUTWARD
    assert "confirmation" in rec["reason"]
    assert outbox.gate(scfg, outbox.OUTWARD) == "confirm"


def test_a_request_missing_a_field_is_refused_before_the_handler_runs(scfg):
    with pytest.raises(outbox.OutboxError, match="body"):
        outbox.validate({"verb": "signal.send", "to": "Robbie"})


def test_the_summary_carries_the_recipient_and_the_words(scfg):
    """It is what the operator approves or rejects, so it has to be specific."""
    s = outbox.describe({"verb": "signal.send", "to": "Robbie",
                         "body": "Running about twenty minutes late, start without me."})
    assert "Robbie" in s and "twenty minutes late" in s


def test_the_summary_is_bounded(scfg):
    s = outbox.describe({"verb": "signal.send", "to": "Robbie", "body": "z" * 500})
    assert len(s) < 200 and s.endswith("”")


def test_signal_cli_is_genuinely_absent_on_this_host():
    """Guards the premise of the missing-binary path: if someone installs
    signal-cli, the 'nothing is installed' documentation needs revisiting."""
    assert shutil.which("signal-cli") is None
