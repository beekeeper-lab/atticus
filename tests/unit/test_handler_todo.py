"""`todo.add` — the first credentialed handler, and the proof of the #42 gate.

Issue #51. Nothing here touches the network, a real token, or a subprocess: the
whole `requests` module is replaced inside the handler, so a test that forgets to
mock fails loudly instead of quietly calling Microsoft with the operator's token.

The properties worth pinning are the ones that will actually bite:

  * **Write consent does not exist yet.** The stored m365 token is read-only, so
    the normal state of this handler on first deploy is "refused by Azure". It has
    to say `Tasks.ReadWrite` out loud, because the fix is a human action.
  * **INTERNAL means unattended.** If this ever drifts to held, the feature is
    useless — you would read "a task is pending" instead of finding it on a phone.
  * **A due date is never guessed**, and a list is never created.
  * **The m365 CLI's token file is read, never written.** Two processes rotating
    one refresh token is how an account locks itself out.
"""
import json

import outbox
import pytest
import requests
from handlers import todo

TOKEN_OK = {"access_token": "at-1", "refresh_token": "rt-rotated",
            "scope": "offline_access Tasks.ReadWrite", "expires_in": 3600}
LISTS = {"value": [
    {"id": "L-flag", "displayName": "Flagged email", "wellknownListName": "flaggedEmails"},
    {"id": "L-def", "displayName": "Tasks", "wellknownListName": "defaultList"},
    {"id": "L-shop", "displayName": "Shopping", "wellknownListName": "none"},
]}

_NO_JSON = object()


class _Resp:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(
            payload if payload is not _NO_JSON else {})

    def json(self):
        if self._payload is _NO_JSON:
            raise ValueError("not json")
        return self._payload


class _FakeRequests:
    """Stands in for the `requests` module inside the handler."""

    RequestException = requests.RequestException

    def __init__(self):
        self.token = _Resp(200, TOKEN_OK)
        self.lists = _Resp(200, LISTS)
        self.created = _Resp(201, {"id": "T-1"})
        self.raise_on = None            # exception to raise instead of answering
        self.token_posts = []
        self.graph = []

    def post(self, url, data=None, timeout=None):
        self.token_posts.append({"url": url, "data": data, "timeout": timeout})
        if self.raise_on == "token":
            raise requests.ConnectionError("no route")
        return self.token

    def request(self, method, url, headers=None, timeout=None, json=None):
        self.graph.append({"method": method, "url": url, "headers": headers,
                           "json": json, "timeout": timeout})
        if self.raise_on == "graph":
            raise requests.ConnectionError("no route")
        return self.lists if method == "GET" else self.created


@pytest.fixture
def net(monkeypatch):
    f = _FakeRequests()
    monkeypatch.setattr(todo, "requests", f)
    return f


@pytest.fixture
def token_file(tmp_path, cfg):
    """A token store shaped exactly like the m365 CLI's, pointed at by config."""
    p = tmp_path / "m365.json"
    p.write_text(json.dumps({
        "client_id": "cid", "tenant_id": "tid", "refresh_token": "rt-original",
        "timezone": "America/New_York",
        "access_token": "stale-read-only-token", "access_token_expires": 9999999999,
    }))
    cfg.todo_token_file = str(p)
    return p


def _add(cfg, **body):
    return todo.add({"verb": "todo.add", **body}, cfg, log=lambda m: None)


# ── registration and the gate ──────────────────────────────────────────────
def test_the_verb_is_registered_with_a_required_title():
    h = outbox.handler_for("todo.add")
    assert h is not None, "importing handlers.todo must register the verb"
    assert h["schema"] == ("title",)


def test_the_risk_class_is_internal_so_it_runs_unattended(cfg):
    """Only the operator sees a todo and undoing it is one tap. Holding it for
    confirmation would defeat the point: the item has to be on the phone."""
    assert outbox.handler_for("todo.add")["risk"] == outbox.INTERNAL
    assert outbox.gate(cfg, outbox.INTERNAL) == "auto"


def test_an_unattended_pass_actually_performs_the_add(tmp_path, cfg, net, token_file):
    """End to end through the outbox, with nobody present to approve."""
    out = tmp_path / "output"
    (out / "outbox").mkdir(parents=True)
    (out / "outbox" / "001-todo.add.json").write_text(json.dumps(
        {"verb": "todo.add", "title": "Pick up the prescription"}))
    res = outbox.process(out, cfg, log=lambda m: None)
    assert res["done"] == 1 and res["failed"] == 0 and res["refused"] == 0
    rec = res["receipts"][0]
    assert rec["status"] == "done" and rec["risk"] == outbox.INTERNAL
    assert rec["id"] == "T-1"
    assert "Pick up the prescription" in rec["summary"], "the operator reads this"


def test_the_summary_names_the_task_the_list_and_the_date():
    s = outbox.describe({"verb": "todo.add", "title": "Renew the domain",
                         "list": "Shopping", "due": "2026-08-07"})
    assert "Renew the domain" in s and "Shopping" in s and "2026-08-07" in s


# ── not authorised yet: the normal state on first deploy ────────────────────
def test_azure_refusing_the_write_scope_names_the_scope(cfg, net, token_file):
    """The stored token is read-only. Until someone re-consents this is what
    happens on every pass, and the message is the only thing that gets it fixed."""
    net.token = _Resp(400, {"error": "invalid_grant",
                            "error_description": "AADSTS65001: The user has not "
                                                 "consented to Tasks.ReadWrite"})
    with pytest.raises(outbox.OutboxError) as e:
        _add(cfg, title="Pick up the prescription")
    msg = str(e.value)
    assert "Tasks.ReadWrite" in msg
    assert "m365-auth" in msg, "must say what the operator has to run"
    assert "AADSTS65001" in msg, "must carry Microsoft's own reason"
    assert not net.graph, "nothing may be attempted against Graph"


def test_a_token_granted_without_the_write_scope_is_refused_before_the_post(
        cfg, net, token_file):
    """Azure can hand back a narrower token than the one asked for; that must not
    surface later as an unexplained 403."""
    net.token = _Resp(200, {**TOKEN_OK, "scope": "offline_access Tasks.Read"})
    with pytest.raises(outbox.OutboxError, match="Tasks.ReadWrite"):
        _add(cfg, title="x")
    assert not net.graph


def test_graph_refusing_the_write_is_reported_as_a_consent_problem(cfg, net, token_file):
    """Consent can be revoked between passes."""
    net.created = _Resp(403, _NO_JSON, text="ErrorAccessDenied")
    with pytest.raises(outbox.OutboxError, match="Tasks.ReadWrite"):
        _add(cfg, title="x")


def test_no_token_file_at_all_says_so_and_names_the_command(cfg, net, tmp_path):
    cfg.todo_token_file = str(tmp_path / "nope.json")
    with pytest.raises(outbox.OutboxError) as e:
        _add(cfg, title="x")
    assert "nope.json" in str(e.value) and "m365-auth" in str(e.value)
    assert not net.token_posts, "must not try to mint a token with nothing to mint from"


def test_a_token_file_missing_its_refresh_token_is_named_field_by_field(
        cfg, net, tmp_path):
    p = tmp_path / "m365.json"
    p.write_text(json.dumps({"client_id": "cid", "tenant_id": "tid"}))
    cfg.todo_token_file = str(p)
    with pytest.raises(outbox.OutboxError, match="refresh_token"):
        _add(cfg, title="x")


def test_a_network_failure_is_a_sentence_not_a_traceback(cfg, net, token_file):
    net.raise_on = "token"
    with pytest.raises(outbox.OutboxError, match="ConnectionError"):
        _add(cfg, title="x")


# ── the successful add ─────────────────────────────────────────────────────
def test_a_task_is_created_in_the_default_list(cfg, net, token_file):
    res = _add(cfg, title="Pick up the prescription")
    assert res == {"id": "T-1", "list": "Tasks",
                   "title": "Pick up the prescription", "due": None}
    post = [g for g in net.graph if g["method"] == "POST"]
    assert len(post) == 1
    assert post[0]["url"].endswith("/me/todo/lists/L-def/tasks"), \
        "wellknownListName=defaultList, not simply the first list"
    assert post[0]["json"] == {"title": "Pick up the prescription"}
    assert post[0]["headers"]["Authorization"] == "Bearer at-1"


def test_the_token_request_asks_only_for_the_write_scope(cfg, net, token_file):
    _add(cfg, title="x")
    data = net.token_posts[0]["data"]
    assert data["grant_type"] == "refresh_token"
    assert data["refresh_token"] == "rt-original"
    assert "Tasks.ReadWrite" in data["scope"] and "offline_access" in data["scope"]
    assert "Mail.Read" not in data["scope"], "a todo token must not be able to read mail"


def test_the_m365_token_file_is_never_rewritten(cfg, net, token_file):
    """Azure keeps the old refresh token valid, so there is nothing to save — and
    two writers rotating one token is how the account locks itself out."""
    before = token_file.read_text()
    _add(cfg, title="x")
    assert token_file.read_text() == before


def test_a_note_becomes_the_task_body(cfg, net, token_file):
    _add(cfg, title="Renew the domain", note='spoken: "renew the attic us dev domain"')
    body = [g for g in net.graph if g["method"] == "POST"][0]["json"]
    assert body["body"] == {"content": 'spoken: "renew the attic us dev domain"',
                            "contentType": "text"}


def test_a_long_title_is_trimmed_rather_than_silently_truncated_by_graph(
        cfg, net, token_file):
    res = _add(cfg, title="z" * 400)
    assert len(res["title"]) == todo.TITLE_MAX


# ── due dates ──────────────────────────────────────────────────────────────
def test_a_calendar_date_becomes_local_noon_expressed_in_utc(cfg, net, token_file):
    """To Do treats a due date as date-only but Graph demands a datetime. Midnight
    lands on the wrong day under one of the two plausible client behaviours; noon
    is right under both. The token file says America/New_York (UTC-4 in August)."""
    _add(cfg, title="x", due="2026-08-07")
    body = [g for g in net.graph if g["method"] == "POST"][0]["json"]
    assert body["dueDateTime"] == {"dateTime": "2026-08-07T16:00:00.0000000",
                                   "timeZone": "UTC"}


def test_a_spoken_phrase_in_due_is_refused_and_nothing_is_created(cfg, net, token_file):
    """Resolving "by Friday" needs the day the words were spoken, which the agent
    has and the pipeline does not. A phrase arriving here means it could not."""
    with pytest.raises(outbox.OutboxError) as e:
        _add(cfg, title="x", due="by Friday")
    assert "YYYY-MM-DD" in str(e.value)
    assert not [g for g in net.graph if g["method"] == "POST"]


def test_an_impossible_date_is_refused(cfg, net, token_file):
    with pytest.raises(outbox.OutboxError, match="not a real date"):
        _add(cfg, title="x", due="2026-02-31")


def test_an_unknown_timezone_falls_back_to_utc_rather_than_failing(
        cfg, net, tmp_path):
    p = tmp_path / "m365.json"
    p.write_text(json.dumps({"client_id": "c", "tenant_id": "t",
                             "refresh_token": "r", "timezone": "Mars/Olympus"}))
    cfg.todo_token_file = str(p)
    _add(cfg, title="x", due="2026-08-07")
    body = [g for g in net.graph if g["method"] == "POST"][0]["json"]
    assert body["dueDateTime"]["dateTime"] == "2026-08-07T12:00:00.0000000"


def test_no_due_date_sends_no_due_date(cfg, net, token_file):
    _add(cfg, title="x")
    assert "dueDateTime" not in [g for g in net.graph if g["method"] == "POST"][0]["json"]


# ── which list ─────────────────────────────────────────────────────────────
def test_a_named_list_is_matched_case_insensitively(cfg, net, token_file):
    _add(cfg, title="milk", list="shopping")
    assert [g for g in net.graph if g["method"] == "POST"][0]["url"].endswith(
        "/me/todo/lists/L-shop/tasks")


def test_the_config_default_list_is_used_when_the_request_names_none(
        cfg, net, token_file):
    cfg.todo_list = "Shopping"
    _add(cfg, title="milk")
    assert [g for g in net.graph if g["method"] == "POST"][0]["url"].endswith(
        "/me/todo/lists/L-shop/tasks")


def test_the_request_overrides_the_config_default(cfg, net, token_file):
    cfg.todo_list = "Shopping"
    _add(cfg, title="x", list="Tasks")
    assert [g for g in net.graph if g["method"] == "POST"][0]["url"].endswith(
        "/me/todo/lists/L-def/tasks")


def test_an_unknown_list_is_refused_and_never_created(cfg, net, token_file):
    """A misheard "Groseries" beside "Groceries" would be a task the operator never
    finds — the quiet failure this project treats as the worst kind."""
    with pytest.raises(outbox.OutboxError) as e:
        _add(cfg, title="milk", list="Groseries")
    msg = str(e.value)
    assert "Groseries" in msg and "Shopping" in msg, "name the lists that do exist"
    assert not [g for g in net.graph if g["method"] == "POST"], "no list, no task"


def test_an_account_with_no_lists_says_so(cfg, net, token_file):
    net.lists = _Resp(200, {"value": []})
    with pytest.raises(outbox.OutboxError, match="no To Do lists"):
        _add(cfg, title="x")


# ── configuration ──────────────────────────────────────────────────────────
def test_the_token_file_defaults_to_the_m365_clis_own_store(partial_cfg):
    """No setting configured: use exactly the path `m365` uses, because that token
    is already on the host."""
    assert todo._token_file(partial_cfg).as_posix().endswith("/.secrets/m365.json")


def test_a_named_account_resolves_like_m365_account(cfg):
    cfg.todo_token_file = ""
    cfg.todo_account = "organservices"
    assert todo._token_file(cfg).name == "m365-organservices.json"


def test_an_account_name_cannot_escape_the_secrets_directory(cfg):
    cfg.todo_token_file = ""
    cfg.todo_account = "../../etc/passwd"
    p = todo._token_file(cfg)
    assert p.parent.name == ".secrets" and ".." not in p.name


def test_settings_are_read_with_defaults_so_an_older_config_still_works(partial_cfg,
                                                                        net, tmp_path):
    """`partial_cfg` genuinely lacks every todo_* attribute."""
    p = tmp_path / "m365.json"
    p.write_text(json.dumps({"client_id": "c", "tenant_id": "t", "refresh_token": "r"}))
    partial_cfg.todo_token_file = str(p)
    assert todo.add({"verb": "todo.add", "title": "x"}, partial_cfg,
                    log=lambda m: None)["id"] == "T-1"
    assert net.graph[0]["timeout"] == 20
