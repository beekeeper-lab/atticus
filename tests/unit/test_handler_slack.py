"""The Slack handler (#48). No network: `requests.post` is always mocked.

The properties worth testing here are all refusals, plus one trap. A channel post
is read by everyone in the channel within seconds and cannot be unsent, and the
text derives from ambient audio, so:

  * the channel must come from the operator's allowlist, never from the request
    alone — "the standup channel" is one mishearing away from "#general";
  * a missing or wrong-shaped token must fail by NAME, not with a stack trace;
  * and Slack signals application errors with **HTTP 200 and {"ok": false}**, so a
    handler that checks only the status code reports every failure as a successful
    post. That is the one wrong answer that matters, because the operator's report
    would claim a channel was told something it never heard.
"""
import types

import outbox
import pytest
import requests
from handlers import slack


@pytest.fixture(autouse=True)
def _slack_cfg(cfg):
    """A configured-and-working Slack, which individual tests then break."""
    # ASSEMBLED, not written literally. ops/pr.sh's credential guard scans the
    # staged diff for exactly this shape and has no exemption mechanism on purpose:
    # a bypass for "it's only a test" is a hole, because a test file is a perfectly
    # ordinary place to paste a real token by accident. Same convention as
    # tests/security/test_agent_isolation.py.
    cfg.slack_bot_token = "xoxb" + "-test-token"
    cfg.slack_channels = ["ddi-platform", "standup"]
    cfg.slack_default_channel = ""
    cfg.slack_api_url = "http://127.0.0.1:1/none"
    cfg.slack_timeout = 1
    return cfg


class _Resp:
    def __init__(self, payload, status=200, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok(**over):
    return _Resp({"ok": True, "channel": "C0DDI", "ts": "1717171717.000100", **over})


@pytest.fixture
def sent(monkeypatch):
    """Capture calls to requests.post and reply {"ok": true} by default."""
    calls = []

    def fake_post(url, **kw):
        calls.append({"url": url, **kw})
        return _ok()
    monkeypatch.setattr(slack.requests, "post", fake_post)
    return calls


def _reply(monkeypatch, resp):
    calls = []

    def fake_post(url, **kw):
        calls.append({"url": url, **kw})
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(slack.requests, "post", fake_post)
    return calls


# ── registration ───────────────────────────────────────────────────────────
def test_the_verb_is_registered():
    assert "slack.post" in outbox.known_verbs()


def test_a_channel_post_is_an_outward_action(cfg):
    """OUTWARD is the point: visible to many people at once, immediate, and not
    recallable. It must default to held, which is what the risk class buys."""
    h = outbox.handler_for("slack.post")
    assert h["risk"] == outbox.OUTWARD
    assert outbox.gate(cfg, h["risk"]) == "confirm"


def test_text_is_required_by_the_schema():
    assert "text" in outbox.handler_for("slack.post")["schema"]


def test_the_summary_names_the_channel_and_shows_the_text():
    """It is what the operator reads when approving, so it has to be specific."""
    s = outbox.describe({"verb": "slack.post", "channel": "#standup",
                         "text": "Migration finished"})
    assert "standup" in s and "Migration finished" in s


# ── the credential ─────────────────────────────────────────────────────────
def test_a_missing_token_names_the_setting(cfg, sent):
    cfg.slack_bot_token = ""
    with pytest.raises(outbox.OutboxError, match="ATTICUS_SLACK_BOT_TOKEN"):
        slack.post({"channel": "standup", "text": "hi"}, cfg)
    assert not sent, "nothing may go on the wire without a token"


def test_a_config_without_slack_at_all_fails_cleanly(cfg, tmp_path, sent):
    """The normal state for a service nobody has set up: every setting absent, so
    every read is a getattr default. It must name a setting, not raise
    AttributeError from inside a handler nobody configured."""
    bare = types.SimpleNamespace(vault=tmp_path)
    with pytest.raises(outbox.OutboxError, match="ATTICUS_SLACK_CHANNELS"):
        slack.post({"channel": "standup", "text": "hi"}, bare)
    assert not sent


def test_a_user_token_is_refused_not_merely_warned(cfg, sent):
    """A xoxp- token would work, and that is exactly the problem: it can read
    every DM and act as the account. The narrow bot token is the control."""
    cfg.slack_bot_token = "xoxp" + "-a-user-token"
    with pytest.raises(outbox.OutboxError, match="bot token"):
        slack.post({"channel": "standup", "text": "hi"}, cfg)
    assert not sent


def test_the_token_is_sent_as_a_bearer_header(cfg, sent):
    slack.post({"channel": "standup", "text": "hi"}, cfg)
    assert sent[0]["headers"]["Authorization"] == "Bearer xoxb" + "-test-token"


# ── the channel allowlist ──────────────────────────────────────────────────
def test_a_channel_not_on_the_allowlist_is_refused(cfg, sent):
    """The whole safety story. One misheard sentence must not reach #general."""
    with pytest.raises(outbox.OutboxError, match="general"):
        slack.post({"channel": "general", "text": "hi"}, cfg)
    assert not sent, "a refused channel must not be posted to anyway"


def test_the_refusal_says_what_is_allowed(cfg, sent):
    with pytest.raises(outbox.OutboxError) as e:
        slack.post({"channel": "#random", "text": "hi"}, cfg)
    assert "ddi-platform" in str(e.value) and "standup" in str(e.value)


def test_an_empty_allowlist_means_off_not_anywhere(cfg, sent):
    """Fail-open here would be a bug with an audience."""
    cfg.slack_channels = []
    with pytest.raises(outbox.OutboxError, match="ATTICUS_SLACK_CHANNELS"):
        slack.post({"channel": "standup", "text": "hi"}, cfg)
    assert not sent


def test_the_allowlist_is_checked_before_the_credential(cfg, sent):
    """Order matters: a bad channel is reported as a bad channel even on a box
    where Slack was never configured, which is the more useful diagnosis."""
    cfg.slack_bot_token = ""
    with pytest.raises(outbox.OutboxError, match="allowlist"):
        slack.post({"channel": "general", "text": "hi"}, cfg)


@pytest.mark.parametrize("asked", ["standup", "#standup", " #Standup ", "STANDUP"])
def test_the_channel_is_matched_the_way_a_human_would_write_it(cfg, sent, asked):
    slack.post({"channel": asked, "text": "hi"}, cfg)
    assert sent[0]["json"]["channel"] == "#standup"


def test_a_channel_id_goes_on_the_wire_without_a_hash(cfg, sent):
    cfg.slack_channels = ["C0123456789"]
    slack.post({"channel": "C0123456789", "text": "hi"}, cfg)
    assert sent[0]["json"]["channel"] == "C0123456789"


def test_no_channel_and_no_default_is_refused(cfg, sent):
    with pytest.raises(outbox.OutboxError, match="ATTICUS_SLACK_DEFAULT_CHANNEL"):
        slack.post({"text": "hi"}, cfg)
    assert not sent


def test_the_default_channel_is_used_when_none_was_asked_for(cfg, sent):
    cfg.slack_default_channel = "standup"
    slack.post({"text": "hi"}, cfg)
    assert sent[0]["json"]["channel"] == "#standup"


def test_a_default_channel_off_the_allowlist_is_still_refused(cfg, sent):
    """The allowlist is the single gate; a second setting must not bypass it."""
    cfg.slack_default_channel = "general"
    with pytest.raises(outbox.OutboxError, match="allowlist"):
        slack.post({"text": "hi"}, cfg)
    assert not sent


# ── the trap: HTTP 200 with ok:false ───────────────────────────────────────
def test_ok_false_on_http_200_is_a_failure(cfg, monkeypatch):
    """Slack returns 200 for application errors. Trusting the status code would
    make the receipt say a channel was told something it never heard."""
    _reply(monkeypatch, _Resp({"ok": False, "error": "not_in_channel"}))
    with pytest.raises(outbox.OutboxError) as e:
        slack.post({"channel": "standup", "text": "hi"}, cfg)
    assert "not_in_channel" in str(e.value)
    assert "invite" in str(e.value), "must say what to do about it"


def test_a_missing_scope_error_names_the_scope_slack_wanted(cfg, monkeypatch):
    _reply(monkeypatch, _Resp({"ok": False, "error": "missing_scope",
                               "needed": "chat:write"}))
    with pytest.raises(outbox.OutboxError, match="chat:write"):
        slack.post({"channel": "standup", "text": "hi"}, cfg)


def test_an_unmapped_slack_error_is_still_reported_by_name(cfg, monkeypatch):
    _reply(monkeypatch, _Resp({"ok": False, "error": "some_new_error"}))
    with pytest.raises(outbox.OutboxError, match="some_new_error"):
        slack.post({"channel": "standup", "text": "hi"}, cfg)


def test_a_non_200_is_reported_with_a_truncated_body(cfg, monkeypatch):
    _reply(monkeypatch, _Resp(None, status=502, text="x" * 4000))
    with pytest.raises(outbox.OutboxError) as e:
        slack.post({"channel": "standup", "text": "hi"}, cfg)
    assert "502" in str(e.value)
    assert len(str(e.value)) < 400, "receipts are committed; git is forever"


def test_a_non_json_body_does_not_raise_a_bare_valueerror(cfg, monkeypatch):
    _reply(monkeypatch, _Resp(None, status=200, text="<html>nope</html>"))
    with pytest.raises(outbox.OutboxError, match="non-JSON"):
        slack.post({"channel": "standup", "text": "hi"}, cfg)


def test_a_timeout_does_not_claim_nothing_happened(cfg, monkeypatch):
    """The POST may have landed. Saying "not sent" would be a guess."""
    _reply(monkeypatch, requests.Timeout())
    with pytest.raises(outbox.OutboxError, match="may or may not"):
        slack.post({"channel": "standup", "text": "hi"}, cfg)


def test_a_network_error_is_an_outbox_error(cfg, monkeypatch):
    _reply(monkeypatch, requests.ConnectionError("boom"))
    with pytest.raises(outbox.OutboxError, match="network"):
        slack.post({"channel": "standup", "text": "hi"}, cfg)


# ── the happy path ─────────────────────────────────────────────────────────
def test_a_successful_post_returns_the_channel_and_timestamp(cfg, sent):
    res = slack.post({"channel": "ddi-platform",
                      "text": "Migration finished: https://example.invalid/r"}, cfg)
    assert res["ts"] == "1717171717.000100"
    assert res["channel"] == "C0DDI"

    call = sent[0]
    assert call["url"] == "http://127.0.0.1:1/none"
    assert call["json"] == {"channel": "#ddi-platform",
                            "text": "Migration finished: https://example.invalid/r"}
    assert call["timeout"] == 1


def test_the_default_endpoint_is_slacks_postmessage(cfg, sent):
    del cfg.slack_api_url
    slack.post({"channel": "standup", "text": "hi"}, cfg)
    assert sent[0]["url"] == "https://slack.com/api/chat.postMessage"


def test_an_over_long_message_is_refused_rather_than_truncated(cfg, sent):
    """Cutting a sentence mid-word in front of a whole channel is worse than a
    refusal the operator can read."""
    with pytest.raises(outbox.OutboxError, match="link to the report"):
        slack.post({"channel": "standup", "text": "x" * 5000}, cfg)
    assert not sent


def test_the_receipt_of_a_held_post_records_the_intent(cfg, tmp_path, sent):
    """End to end through the outbox: outward is held, so a real pass posts
    nothing and the operator still sees exactly what was going to be said."""
    import json
    out = tmp_path / "output"
    (out / "outbox").mkdir(parents=True)
    (out / "outbox" / "001-slack.post.json").write_text(json.dumps(
        {"verb": "slack.post", "channel": "standup", "text": "Deploy done"}))
    res = outbox.process(out, cfg, log=lambda m: None)
    assert not sent
    rec = res["receipts"][0]
    assert rec["status"] == "held" and rec["risk"] == outbox.OUTWARD
    assert "standup" in rec["summary"] and "Deploy done" in rec["summary"]
