"""The Outlook handler (#44 draft, #45 event) — writes only, and only after consent.

Two properties carry this file. First, **the scope check must fail cleanly and name
the scope**, because that is the state every deployment starts in: the `m365` token is
read-only on purpose, so `Mail.ReadWrite` and `Calendars.ReadWrite` do not exist until
someone re-consents. An unhelpful failure here is what an operator meets on day one.

Second, **a draft must not send**. Nothing in this module may reach `/send` or ask for
`Mail.Send`, and a request that asks it to must be refused rather than downgraded.

No network and no subprocess: `requests.post` is replaced by a recorder that answers
the token endpoint and Graph from a script, so every assertion is about what would
have gone on the wire.
"""
import json

import outbox
import pytest
from handlers import outlook

READ_ONLY = "offline_access User.Read Mail.Read Calendars.Read Contacts.Read People.Read"


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Wire:
    """Stands in for `requests.post`. Records every call; answers by URL."""

    def __init__(self, *, granted=None, token_error=None, graph=None, graph_status=200):
        self.granted = granted
        self.token_error = token_error
        self.graph = graph if graph is not None else {
            "id": "AAMkAD==", "webLink": "https://outlook.office.com/x"}
        self.graph_status = graph_status
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if "/oauth2/v2.0/token" in url:
            if self.token_error:
                return _Resp(400, self.token_error)
            return _Resp(200, {"access_token": "at-write", "refresh_token": "rt-new",
                               "expires_in": 3600, "scope": self.granted or READ_ONLY})
        return _Resp(self.graph_status, self.graph)

    @property
    def graph_calls(self):
        return [c for c in self.calls if "/oauth2/" not in c["url"]]


@pytest.fixture
def wired(cfg, tmp_path, monkeypatch):
    """A config pointing at a fake `m365` secret store, with the wire mocked."""
    store = tmp_path / "m365.json"
    store.write_text(json.dumps({"client_id": "cid", "tenant_id": "tid",
                                 "refresh_token": "rt-old", "timezone": "America/Indiana/Indianapolis"}))
    cfg.outlook_secrets = str(store)
    cfg.outlook_account = "default"

    def _install(wire):
        monkeypatch.setattr(outlook.requests, "post", wire)
        return wire
    cfg._install = _install
    cfg._store = store
    return cfg


# ── the scope gate: the state every deployment starts in ───────────────────────
def test_a_draft_without_mail_readwrite_names_the_scope(wired):
    """The token comes back valid but read-only. That must not become a Graph 403."""
    wire = wired._install(_Wire(granted=READ_ONLY))
    with pytest.raises(outbox.OutboxError) as e:
        outlook.draft({"to": "robbie@example.com", "subject": "s", "body": "b"},
                      wired, log=lambda m: None)
    assert "Mail.ReadWrite" in str(e.value)
    assert "m365-auth" in str(e.value)
    # Nothing was attempted against Graph, so nothing half-happened.
    assert wire.graph_calls == []


def test_an_event_without_calendars_readwrite_names_the_scope(wired):
    wire = wired._install(_Wire(granted=READ_ONLY))
    with pytest.raises(outbox.OutboxError) as e:
        outlook.event({"subject": "s", "start": "2026-08-04T14:00"}, wired,
                      log=lambda m: None)
    assert "Calendars.ReadWrite" in str(e.value)
    assert wire.graph_calls == []


def test_an_unconsented_scope_is_reported_as_the_scope_not_as_invalid_grant(wired):
    """Entra refuses the exchange with AADSTS65001. "invalid_grant" alone is not a
    diagnosis; the operator needs the permission to add."""
    wired._install(_Wire(token_error={
        "error": "invalid_grant",
        "error_description": "AADSTS65001: The user or administrator has not "
                             "consented to use the application."}))
    with pytest.raises(outbox.OutboxError) as e:
        outlook.draft({"to": "r@example.com", "subject": "s", "body": "b"}, wired,
                      log=lambda m: None)
    assert "Mail.ReadWrite" in str(e.value)


def test_an_absent_credential_names_the_file(wired, tmp_path):
    wired.outlook_secrets = str(tmp_path / "nope.json")
    wired._install(_Wire())
    with pytest.raises(outbox.OutboxError) as e:
        outlook.draft({"to": "r@example.com", "subject": "s", "body": "b"}, wired,
                      log=lambda m: None)
    assert "nope.json" in str(e.value) and "m365-auth" in str(e.value)


def test_the_account_is_configuration_not_a_constant(cfg):
    """Two accounts with different licensing; neither may be assumed."""
    cfg.outlook_secrets = ""        # the conftest fence points this at scratch
    assert outlook._store_path(cfg).name == "m365.json"
    cfg.outlook_account = "organservices"
    assert outlook._store_path(cfg).name == "m365-organservices.json"


# ── a successful draft ─────────────────────────────────────────────────────────
def test_a_draft_is_created_and_never_sent(wired):
    wire = wired._install(_Wire(granted=READ_ONLY + " Mail.ReadWrite"))
    out = outlook.draft({"to": "Robbie Page <Robbie@Example.com>", "cc": "cfo@example.com",
                         "subject": "  Migration  timing ", "body": "Thursday works."},
                        wired, log=lambda m: None)

    assert [c["url"] for c in wire.graph_calls] == \
        ["https://graph.microsoft.com/v1.0/me/messages"]
    body = wire.graph_calls[0]["json"]
    assert body["toRecipients"] == [{"emailAddress": {"address": "robbie@example.com"}}]
    assert body["ccRecipients"] == [{"emailAddress": {"address": "cfo@example.com"}}]
    assert body["subject"] == "Migration timing"
    # Text, not HTML: the body came from a transcript.
    assert body["body"] == {"contentType": "Text", "content": "Thursday works."}
    assert out["id"] == "AAMkAD==" and out["sent"] is False
    assert wire.graph_calls[0]["headers"]["Authorization"] == "Bearer at-write"
    # The one thing that must never appear.
    assert not any("send" in c["url"].lower() for c in wire.calls)


def test_send_true_is_refused_by_name(wired):
    wire = wired._install(_Wire(granted=READ_ONLY + " Mail.ReadWrite"))
    with pytest.raises(outbox.OutboxError) as e:
        outlook.draft({"to": "r@example.com", "subject": "s", "body": "b",
                       "send": True}, wired, log=lambda m: None)
    assert "Mail.Send" in str(e.value)
    assert wire.calls == []          # refused before the credential is even read


def test_a_recipient_that_cannot_be_verified_is_refused(wired, monkeypatch):
    """Resolution is #43 and lives elsewhere. Absent it, a bare name is a refusal —
    never a guess, because a draft is one keypress from delivery."""
    monkeypatch.setattr(outlook, "contacts", None)
    wire = wired._install(_Wire(granted=READ_ONLY + " Mail.ReadWrite"))
    with pytest.raises(outbox.OutboxError) as e:
        outlook.draft({"to": "Robbie", "subject": "s", "body": "b"}, wired,
                      log=lambda m: None)
    assert "Robbie" in str(e.value) and "#43" in str(e.value)
    assert wire.calls == []


def test_the_resolver_is_used_when_it_is_there_and_unambiguous(wired, monkeypatch):
    import types
    monkeypatch.setattr(outlook, "contacts", types.SimpleNamespace(
        resolve=lambda name, channel=None: [
            {"name": "Robbie Page", "handle": "robbie@example.com", "confidence": 0.94},
            {"name": "Robbie Other", "handle": "other@example.com", "confidence": 0.4}]))
    wire = wired._install(_Wire(granted=READ_ONLY + " Mail.ReadWrite"))
    outlook.draft({"to": "Robbie", "subject": "s", "body": "b"}, wired,
                  log=lambda m: None)
    assert wire.graph_calls[0]["json"]["toRecipients"] == \
        [{"emailAddress": {"address": "robbie@example.com"}}]


def test_two_confident_candidates_refuse_rather_than_pick_one(wired, monkeypatch):
    import types
    monkeypatch.setattr(outlook, "contacts", types.SimpleNamespace(
        resolve=lambda name, channel=None: [
            {"name": "Robbie Page", "handle": "robbie@example.com", "confidence": 0.94},
            {"name": "Robbie Ng", "handle": "ng@example.com", "confidence": 0.93}]))
    wired._install(_Wire(granted=READ_ONLY + " Mail.ReadWrite"))
    with pytest.raises(outbox.OutboxError) as e:
        outlook.draft({"to": "Robbie", "subject": "s", "body": "b"}, wired,
                      log=lambda m: None)
    assert "2 candidate" in str(e.value)


def test_the_rotated_refresh_token_is_saved_so_m365_reads_keep_working(wired):
    wired._install(_Wire(granted=READ_ONLY + " Mail.ReadWrite"))
    outlook.draft({"to": "r@example.com", "subject": "s", "body": "b"}, wired,
                  log=lambda m: None)
    saved = json.loads(wired._store.read_text())
    assert saved["refresh_token"] == "rt-new"
    # Our write-capable access token must NOT be cached into a read-only tool's store.
    assert "access_token" not in saved


# ── a successful event ─────────────────────────────────────────────────────────
def test_an_event_is_created_with_attendees_and_the_configured_zone(wired):
    wire = wired._install(_Wire(granted=READ_ONLY + " Calendars.ReadWrite"))
    out = outlook.event({"subject": "Sync", "start": "2026-08-04T14:00",
                         "minutes": 45, "attendees": ["robbie@example.com"],
                         "location": "Teams"}, wired, log=lambda m: None)

    assert [c["url"] for c in wire.graph_calls] == \
        ["https://graph.microsoft.com/v1.0/me/events"]
    body = wire.graph_calls[0]["json"]
    assert body["start"] == {"dateTime": "2026-08-04T14:00:00",
                             "timeZone": "America/Indiana/Indianapolis"}
    assert body["end"]["dateTime"] == "2026-08-04T14:45:00"
    assert body["attendees"] == [{"emailAddress": {"address": "robbie@example.com"},
                                  "type": "required"}]
    assert body["location"] == {"displayName": "Teams"}
    assert out["id"] == "AAMkAD=="


def test_an_offset_start_is_normalised_to_utc_rather_than_relabelled(wired):
    """The failure this prevents moves a meeting by hours and looks like success."""
    wire = wired._install(_Wire(granted="Calendars.ReadWrite"))
    outlook.event({"subject": "Sync", "start": "2026-08-04T14:00:00+02:00",
                   "end": "2026-08-04T15:00:00+02:00"}, wired, log=lambda m: None)
    body = wire.graph_calls[0]["json"]
    assert body["start"] == {"dateTime": "2026-08-04T12:00:00", "timeZone": "UTC"}
    assert body["end"]["dateTime"] == "2026-08-04T13:00:00"


def test_the_default_duration_comes_from_configuration(wired):
    wired.outlook_event_minutes = 15
    wire = wired._install(_Wire(granted="Calendars.ReadWrite"))
    outlook.event({"subject": "Sync", "start": "2026-08-04T09:00"}, wired,
                  log=lambda m: None)
    assert wire.graph_calls[0]["json"]["end"]["dateTime"] == "2026-08-04T09:15:00"


@pytest.mark.parametrize("bad,expect", [
    ({"subject": "s", "start": "next tuesday"}, "ISO-8601"),
    ({"subject": "s", "start": "2026-08-04T14:00", "end": "2026-08-04T13:00"},
     "not after start"),
])
def test_an_unusable_time_is_refused_before_the_credential_is_read(wired, bad, expect):
    wire = wired._install(_Wire(granted="Calendars.ReadWrite"))
    with pytest.raises(outbox.OutboxError) as e:
        outlook.event(bad, wired, log=lambda m: None)
    assert expect in str(e.value)
    assert wire.calls == []


# ── failure containment ────────────────────────────────────────────────────────
def test_a_graph_error_is_an_outbox_error_with_the_graph_code(wired):
    wired._install(_Wire(granted="Mail.ReadWrite", graph_status=403,
                         graph={"error": {"code": "ErrorAccessDenied",
                                          "message": "Access is denied."}}))
    with pytest.raises(outbox.OutboxError) as e:
        outlook.draft({"to": "r@example.com", "subject": "s", "body": "b"}, wired,
                      log=lambda m: None)
    assert "403" in str(e.value) and "ErrorAccessDenied" in str(e.value)


def test_a_timeout_says_the_write_may_have_landed(wired, monkeypatch):
    def _boom(url, **kw):
        if "/oauth2/" in url:
            return _Resp(200, {"access_token": "at", "refresh_token": "rt-old",
                               "scope": "Mail.ReadWrite"})
        raise outlook.requests.Timeout()
    monkeypatch.setattr(outlook.requests, "post", _boom)
    with pytest.raises(outbox.OutboxError) as e:
        outlook.draft({"to": "r@example.com", "subject": "s", "body": "b"}, wired,
                      log=lambda m: None)
    assert "may or may not" in str(e.value)


def test_too_many_recipients_is_refused(wired):
    wire = wired._install(_Wire(granted="Mail.ReadWrite"))
    to = [f"p{i}@example.com" for i in range(6)]
    with pytest.raises(outbox.OutboxError) as e:
        outlook.draft({"to": to, "subject": "s", "body": "b"}, wired, log=lambda m: None)
    assert "cap is 5" in str(e.value)
    assert wire.calls == []


# ── registration ───────────────────────────────────────────────────────────────
def test_both_verbs_are_tracked():
    """TRACKED defaults to `confirm`, which is the gate both of these need: an invite
    is visible to attendees immediately, and a draft is a pre-addressed message one
    click from delivery."""
    for verb in ("outlook.draft", "outlook.event"):
        h = outbox.handler_for(verb)
        assert h is not None, f"{verb} is not registered"
        assert h["risk"] == outbox.TRACKED

    # And the shipped default really does hold them for a human.
    from config import Config
    from pathlib import Path
    real = Config(env_file=Path(__file__).resolve().parents[2] / "ops/.env.example")
    assert outbox.gate(real, outbox.TRACKED) == "confirm"


def test_no_read_verb_is_registered():
    """Reads cannot be served by an outbox (see outbox.py). Registering something
    like outlook.mail_search would promise the agent data it can never receive."""
    assert [v for v in outbox.known_verbs() if v.startswith("outlook.")] == \
        ["outlook.draft", "outlook.event"]


def test_the_describe_line_shows_who_and_what(wired):
    assert "robbie@example.com" in outbox.describe(
        {"verb": "outlook.draft", "to": ["robbie@example.com"], "subject": "Migration"})
    assert "Sync" in outbox.describe(
        {"verb": "outlook.event", "subject": "Sync", "start": "2026-08-04T14:00"})


def test_an_explicit_alert_is_set_and_an_ordinary_event_keeps_the_default(wired):
    """alert_minutes_before exists for the reminders companion (#66): the alert
    must fire AT the start. An event that does not ask must not carry reminder
    fields at all — the operator's calendar default is not ours to override."""
    wire = wired._install(_Wire(granted="Calendars.ReadWrite"))
    outlook.event({"subject": "s", "start": "2026-08-04T14:00",
                   "alert_minutes_before": 0}, wired, log=lambda m: None)
    body = wire.graph_calls[0]["json"]
    assert body["isReminderOn"] is True
    assert body["reminderMinutesBeforeStart"] == 0

    outlook.event({"subject": "s2", "start": "2026-08-04T15:00"}, wired,
                  log=lambda m: None)
    body = wire.graph_calls[1]["json"]
    assert "isReminderOn" not in body and "reminderMinutesBeforeStart" not in body


@pytest.mark.parametrize("bad", ["-5", "soon", 3.7])
def test_an_unusable_alert_is_refused_before_the_credential_is_read(wired, bad):
    wire = wired._install(_Wire(granted="Calendars.ReadWrite"))
    with pytest.raises(outbox.OutboxError, match="alert_minutes_before"):
        outlook.event({"subject": "s", "start": "2026-08-04T14:00",
                       "alert_minutes_before": bad}, wired, log=lambda m: None)
    assert wire.graph_calls == []
