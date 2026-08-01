"""The Azure DevOps handler (#49) — the first credentialed write.

What matters here is not that a POST goes out. It is that:

  * an unconfigured service refuses by NAME, because nothing is set up on a fresh
    box and a stack trace is not a diagnosis;
  * the target project comes from config and never from the request, because a
    misheard project name would file into another team's backlog;
  * the result says which work-item type it used, because "file a ticket" is
    ambiguous and the record has to be honest about what it did;
  * the risk class stays TRACKED, so the #42 gate holds it for confirmation by
    default rather than filing tickets unattended.

No network. `requests.post`/`requests.patch` are replaced in the handler module.
"""
import types

import outbox
import pytest
from handlers import ado


# ── doubles ────────────────────────────────────────────────────────────────
class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or (str(payload) if payload else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


CREATED = {
    "id": 4711,
    "url": "https://dev.azure.com/acme/_apis/wit/workItems/4711",
    "_links": {"html": {"href": "https://dev.azure.com/acme/DDI/_workitems/edit/4711"}},
    "fields": {"System.Title": "Rotate the deploy key"},
}


@pytest.fixture
def calls(monkeypatch):
    """Record every outgoing call; return CREATED unless a test says otherwise."""
    seen = []

    def _fake(kind):
        def send(url, json=None, headers=None, auth=None, timeout=None):
            seen.append({"kind": kind, "url": url, "ops": json, "headers": headers,
                         "auth": auth, "timeout": timeout})
            return seen[-1].get("_resp") or FakeResp(200, CREATED)
        return send

    monkeypatch.setattr(ado.requests, "post", _fake("post"))
    monkeypatch.setattr(ado.requests, "patch", _fake("patch"))
    return seen


@pytest.fixture
def ado_cfg(cfg):
    """A configured ADO target. The real Config has no ado_* attributes yet, which
    is exactly the unconfigured state the refusal tests below rely on."""
    cfg.ado_pat = "pat-token"
    cfg.ado_org = "acme"
    cfg.ado_project = "DDI"
    cfg.ado_area_path = "DDI\\Platform"
    cfg.ado_iteration_path = "DDI\\Sprint 42"
    cfg.ado_timeout = 5
    return cfg


def _fields(ops):
    return {o["path"]: o["value"] for o in ops}


def _req(**over):
    r = {"verb": "ado.workitem", "title": "Rotate the deploy key"}
    r.update(over)
    return r


# ── refusing cleanly when nothing is configured ────────────────────────────
def test_a_missing_pat_refuses_by_name(cfg, calls):
    """The normal state on a box where nobody has set ADO up."""
    cfg.ado_org, cfg.ado_project = "acme", "DDI"
    with pytest.raises(outbox.OutboxError, match="ATTICUS_ADO_PAT"):
        ado.create_workitem(_req(), cfg, log=lambda m: None)
    assert not calls, "nothing may go out without a credential"


def test_a_missing_project_refuses_by_name(cfg, calls):
    cfg.ado_pat, cfg.ado_org = "pat-token", "acme"
    with pytest.raises(outbox.OutboxError, match="ATTICUS_ADO_PROJECT"):
        ado.create_workitem(_req(), cfg, log=lambda m: None)
    assert not calls


def test_a_missing_org_refuses_by_name(cfg, calls):
    cfg.ado_pat, cfg.ado_project = "pat-token", "DDI"
    with pytest.raises(outbox.OutboxError, match="ATTICUS_ADO_ORG"):
        ado.create_workitem(_req(), cfg, log=lambda m: None)
    assert not calls


def test_a_credential_property_that_raises_is_still_a_clean_refusal():
    """config.py reads secrets through lazy properties that raise when the
    credential file has no entry. That must surface as a named OutboxError, not as
    the property's RuntimeError escaping through getattr()."""
    class C:
        ado_org, ado_project = "acme", "DDI"

        @property
        def ado_pat(self):
            raise RuntimeError("ADO_PAT not found in ~/.config/ai/env")

    with pytest.raises(outbox.OutboxError, match="ATTICUS_ADO_PAT"):
        ado.create_workitem(_req(), C(), log=lambda m: None)


def test_settings_absent_entirely_still_refuse(calls):
    """A config object predating the setting — getattr fallbacks, no AttributeError."""
    with pytest.raises(outbox.OutboxError, match="ATTICUS_ADO_"):
        ado.create_workitem(_req(), types.SimpleNamespace(), log=lambda m: None)


# ── a successful create ────────────────────────────────────────────────────
def test_a_successful_create_returns_the_id_and_a_clickable_url(ado_cfg, calls):
    """The receipt and the HTML record both link to the item, so the handler has to
    hand back the browser URL — not the REST resource, which is useless in a report."""
    res = ado.create_workitem(_req(), ado_cfg, log=lambda m: None)
    assert res["id"] == 4711
    assert res["url"] == "https://dev.azure.com/acme/DDI/_workitems/edit/4711"
    assert "_apis" not in res["url"]
    assert len(calls) == 1 and calls[0]["kind"] == "post"


def test_the_create_call_is_shaped_the_way_the_ado_api_wants_it(ado_cfg, calls):
    ado.create_workitem(_req(), ado_cfg, log=lambda m: None)
    c = calls[0]
    assert c["url"] == ("https://dev.azure.com/acme/DDI/_apis/wit/workitems/$Task"
                        "?api-version=7.1")
    assert c["headers"]["Content-Type"] == "application/json-patch+json"
    assert c["auth"] == ("", "pat-token"), "PAT goes in basic auth with no username"
    assert c["timeout"] == 5
    assert all(o["op"] == "add" for o in c["ops"])


def test_a_browser_url_is_constructed_when_ado_omits_the_link(ado_cfg, calls, monkeypatch):
    monkeypatch.setattr(ado.requests, "post",
                        lambda *a, **k: FakeResp(200, {"id": 99}))
    res = ado.create_workitem(_req(), ado_cfg, log=lambda m: None)
    assert res["url"] == "https://dev.azure.com/acme/DDI/_workitems/edit/99"


def test_project_area_and_iteration_come_from_config_not_from_the_request(ado_cfg, calls):
    """The agent has no basis for guessing three fields, and a misheard sentence must
    not be able to file into an arbitrary project."""
    ado.create_workitem(_req(project="OTHER-TEAM", area_path="OTHER\\Area",
                             iteration_path="OTHER\\Sprint 1"),
                        ado_cfg, log=lambda m: None)
    c = calls[0]
    assert "/DDI/_apis/" in c["url"] and "OTHER-TEAM" not in c["url"]
    f = _fields(c["ops"])
    assert f["/fields/System.AreaPath"] == "DDI\\Platform"
    assert f["/fields/System.IterationPath"] == "DDI\\Sprint 42"


def test_an_unset_area_path_is_omitted_rather_than_invented(cfg, calls):
    cfg.ado_pat, cfg.ado_org, cfg.ado_project = "p", "acme", "DDI"
    res = ado.create_workitem(_req(), cfg, log=lambda m: None)
    assert "/fields/System.AreaPath" not in _fields(calls[0]["ops"])
    assert res["area_path"] == "(project default)"


def test_the_description_is_escaped_not_passed_through(ado_cfg, calls):
    """It derives from ambient audio and renders in someone else's browser."""
    ado.create_workitem(_req(description="a <script>alert(1)</script> & more"),
                        ado_cfg, log=lambda m: None)
    body = _fields(calls[0]["ops"])["/fields/System.Description"]
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "&amp; more" in body
    assert "Atticus" in body, "provenance line, so the item says where it came from"


def test_an_overlong_title_is_truncated_without_losing_what_was_said(ado_cfg, calls):
    ado.create_workitem(_req(title="x" * 400), ado_cfg, log=lambda m: None)
    f = _fields(calls[0]["ops"])
    assert len(f["/fields/System.Title"]) == 255
    assert "Full title" in f["/fields/System.Description"]


# ── the work-item type, said out loud ──────────────────────────────────────
def test_the_default_type_is_task_and_the_result_says_so(ado_cfg, calls):
    """"File a ticket" is ambiguous between Bug / Task / User Story / Issue, so the
    record has to name the type it actually used."""
    res = ado.create_workitem(_req(), ado_cfg, log=lambda m: None)
    assert res["work_item_type"] == "Task"
    assert res["type_source"] == "config default"
    assert "$Task" in calls[0]["url"]


def test_the_default_type_is_configurable(ado_cfg, calls):
    ado_cfg.ado_workitem_type = "User Story"
    res = ado.create_workitem(_req(), ado_cfg, log=lambda m: None)
    assert res["work_item_type"] == "User Story"
    assert "$User%20Story" in calls[0]["url"]


def test_a_requested_type_on_the_allowlist_is_honoured(ado_cfg, calls):
    res = ado.create_workitem(_req(type="bug"), ado_cfg, log=lambda m: None)
    assert res["work_item_type"] == "Bug", "canonical casing, not the agent's"
    assert res["type_source"] == "request"


def test_an_unknown_requested_type_falls_back_and_says_it_did(ado_cfg, calls):
    """Refusing the whole ticket over one misheard word would lose the thing that
    was asked for; filing a 'Tickit' work item would 404."""
    res = ado.create_workitem(_req(type="Tickit"), ado_cfg, log=lambda m: None)
    assert res["work_item_type"] == "Task"
    assert "Tickit" in res["type_note"]


# ── API failures ───────────────────────────────────────────────────────────
def test_an_api_error_becomes_an_outbox_error_with_the_status(ado_cfg, monkeypatch):
    monkeypatch.setattr(ado.requests, "post",
                        lambda *a, **k: FakeResp(400, None, "TF401347: bad field"))
    with pytest.raises(outbox.OutboxError, match="400"):
        ado.create_workitem(_req(), ado_cfg, log=lambda m: None)


def test_a_rejected_credential_names_the_setting_and_the_scope(ado_cfg, monkeypatch):
    monkeypatch.setattr(ado.requests, "post", lambda *a, **k: FakeResp(401, None, "nope"))
    with pytest.raises(outbox.OutboxError, match="ATTICUS_ADO_PAT") as e:
        ado.create_workitem(_req(), ado_cfg, log=lambda m: None)
    assert "read/write" in str(e.value)


def test_a_404_points_at_the_org_project_and_type(ado_cfg, monkeypatch):
    monkeypatch.setattr(ado.requests, "post", lambda *a, **k: FakeResp(404, None, "gone"))
    with pytest.raises(outbox.OutboxError, match="ATTICUS_ADO_PROJECT"):
        ado.create_workitem(_req(), ado_cfg, log=lambda m: None)


def test_a_timeout_is_reported_as_one(ado_cfg, monkeypatch):
    def boom(*a, **k):
        raise ado.requests.Timeout()
    monkeypatch.setattr(ado.requests, "post", boom)
    with pytest.raises(outbox.OutboxError, match="timed out"):
        ado.create_workitem(_req(), ado_cfg, log=lambda m: None)


def test_a_network_error_does_not_leak_the_exception_text(ado_cfg, monkeypatch):
    def boom(*a, **k):
        raise ado.requests.ConnectionError("proxy http://user:pw@host failed")
    monkeypatch.setattr(ado.requests, "post", boom)
    with pytest.raises(outbox.OutboxError) as e:
        ado.create_workitem(_req(), ado_cfg, log=lambda m: None)
    assert "ConnectionError" in str(e.value) and "user:pw" not in str(e.value)


def test_a_response_with_no_id_is_not_reported_as_success(ado_cfg, monkeypatch):
    monkeypatch.setattr(ado.requests, "post", lambda *a, **k: FakeResp(200, {"count": 0}))
    with pytest.raises(outbox.OutboxError, match="no work-item id"):
        ado.create_workitem(_req(), ado_cfg, log=lambda m: None)


def test_a_non_json_body_is_not_reported_as_success(ado_cfg, monkeypatch):
    monkeypatch.setattr(ado.requests, "post",
                        lambda *a, **k: FakeResp(200, None, "<html>sign in</html>"))
    with pytest.raises(outbox.OutboxError, match="non-JSON"):
        ado.create_workitem(_req(), ado_cfg, log=lambda m: None)


# ── commenting on an existing item ─────────────────────────────────────────
def test_a_comment_patches_the_history_field_of_the_named_item(ado_cfg, calls):
    res = ado.add_comment({"verb": "ado.comment", "id": "4711", "body": "shipped in PR 12"},
                          ado_cfg, log=lambda m: None)
    c = calls[0]
    assert c["kind"] == "patch"
    assert c["url"] == ("https://dev.azure.com/acme/DDI/_apis/wit/workitems/4711"
                        "?api-version=7.1")
    assert "shipped in PR 12" in _fields(c["ops"])["/fields/System.History"]
    assert res["id"] == 4711 and res["url"].endswith("/_workitems/edit/4711")


def test_a_comment_needs_a_numeric_id(ado_cfg, calls):
    with pytest.raises(outbox.OutboxError, match="numeric work-item id"):
        ado.add_comment({"id": "the deploy key one", "body": "hi"},
                        ado_cfg, log=lambda m: None)
    assert not calls


def test_a_comment_missing_a_pat_refuses_before_calling(cfg, calls):
    cfg.ado_org, cfg.ado_project = "acme", "DDI"
    with pytest.raises(outbox.OutboxError, match="ATTICUS_ADO_PAT"):
        ado.add_comment({"id": "1", "body": "hi"}, cfg, log=lambda m: None)
    assert not calls


# ── registration and the gate ──────────────────────────────────────────────
@pytest.mark.parametrize("verb", ["ado.workitem", "ado.comment"])
def test_both_verbs_are_registered_as_tracked(verb):
    """TRACKED, not OUTWARD: a work item is visible but reversible, in a system whose
    whole purpose is tracking things people file. TRACKED also means the #42 gate
    holds it for confirmation by default — the point of going first."""
    h = outbox.handler_for(verb)
    assert h is not None, f"{verb} is not registered — is handlers/__init__ importing ado?"
    assert h["risk"] == outbox.TRACKED


def test_the_default_gate_holds_a_work_item_rather_than_filing_it(cfg):
    assert outbox.gate(cfg, outbox.TRACKED) == "confirm"


def test_the_required_fields_are_rejected_before_a_credential_is_touched():
    with pytest.raises(outbox.OutboxError, match="title"):
        outbox.validate({"verb": "ado.workitem"})
    with pytest.raises(outbox.OutboxError, match="body"):
        outbox.validate({"verb": "ado.comment", "id": "12"})


def test_the_summary_an_operator_confirms_names_the_actual_ticket():
    assert outbox.describe({"verb": "ado.workitem", "title": "Rotate the deploy key"}) \
        == "file an ADO work item: 'Rotate the deploy key'"
    assert outbox.describe({"verb": "ado.comment", "id": 4711}) \
        == "comment on ADO work item #4711"
