"""The approval queue (#83): the gate's middle setting, made real.

`confirm` used to mean HELD FOREVER — the intent was recorded and nothing could
ever approve it, so the middle setting was `off` with better paperwork and every
enabled verb was pushed to `auto`. These tests pin the queue that fixes that,
and the refusals that keep it from becoming a way around the gate.

The security property worth stating, because it shaped the design: approving
must not be reachable from the sandbox. The vault browser was the obvious home
and is the wrong one — it answers on loopback, the sandbox shares the network
namespace, and its write token is embedded in every published page (#69), so an
injected agent could approve its own held actions. Decisions therefore arrive on
a second ntfy topic the agent cannot discover, and every one carries a nonce.

No network anywhere: the poll and both notification paths are monkeypatched.
"""
import json
from datetime import UTC, datetime, timedelta

import approval_drain as drain
import approvals
import outbox
import pytest
from handlers import todo  # noqa: F401  registers todo.add


@pytest.fixture
def acfg(cfg, tmp_path):
    cfg.vault = tmp_path / "vault"
    cfg.vault.mkdir(parents=True, exist_ok=True)
    cfg.approval_topic_url = "https://ntfy.example/atticus-approvals"
    cfg.approvals_enabled = True
    cfg.approval_ttl_hours = 24
    cfg.notify_url = "https://ntfy.example/atticus"
    # Hold every tracked verb, which is what the queue exists for.
    cfg.outbox_verbs = {}
    cfg.outbox_tracked = "confirm"
    return cfg


@pytest.fixture
def sent(monkeypatch):
    """Capture both notification paths."""
    pushes, actions = [], []
    monkeypatch.setattr(drain.nf, "alarm",
                        lambda cfg, text, **kw: pushes.append({"text": text, **kw})
                        or {"ntfy": True, "calendar": False, "deferred": False})
    monkeypatch.setattr(drain.nf, "notify_with_actions",
                        lambda cfg, text, **kw: actions.append({"text": text, **kw}) or True)
    return {"pushes": pushes, "actions": actions}


def _req(**kw):
    return {"verb": "todo.add", "title": "Buy milk", "_file": "001-todo.add.json",
            "_stem": "rec-1", **kw}


def _enqueue(acfg, **kw):
    return approvals.enqueue(acfg.vault, _req(**kw), risk="tracked",
                             summary="add “Buy milk” to the list", stem="rec-1")


# ── the ledger ──────────────────────────────────────────────────────────────
def test_enqueue_then_pending(acfg):
    item = _enqueue(acfg)
    assert item["duplicate"] is False and item["status"] == approvals.PENDING
    assert item["nonce"], "every item needs a nonce to defeat a replayed push"
    assert [a["id"] for a in approvals.pending(acfg.vault)] == [item["id"]]


def test_a_retry_of_the_same_recording_does_not_queue_twice(acfg):
    """--retry re-runs a whole outbox. Two identical rows would mean approving
    one and leaving the other pending forever."""
    a = _enqueue(acfg)
    b = _enqueue(acfg)
    assert b["duplicate"] is True and b["id"] == a["id"]
    assert len(approvals.pending(acfg.vault)) == 1


def test_pipeline_internal_fields_are_not_stored_as_the_agents_request(acfg):
    item = _enqueue(acfg)
    assert "_file" not in item["request"] and "_stem" not in item["request"]
    assert item["request"]["title"] == "Buy milk"


def test_approve_then_ready(acfg):
    item = _enqueue(acfg)
    approvals.decide(acfg.vault, item["id"], "approve", nonce=item["nonce"])
    assert [a["id"] for a in approvals.approved_ready(acfg.vault)] == [item["id"]]
    assert approvals.pending(acfg.vault) == []


def test_deny_performs_nothing_and_leaves_nothing_ready(acfg):
    item = _enqueue(acfg)
    approvals.decide(acfg.vault, item["id"], "deny", nonce=item["nonce"])
    assert approvals.approved_ready(acfg.vault) == []
    assert approvals.pending(acfg.vault) == []


def test_a_stale_nonce_is_refused(acfg):
    """A push from last week must not be re-tappable. The topic is a bearer
    capability, so this is the layer that bounds it."""
    item = _enqueue(acfg)
    with pytest.raises(approvals.ApprovalError, match="nonce"):
        approvals.decide(acfg.vault, item["id"], "approve", nonce="wrong")
    assert approvals.pending(acfg.vault), "still awaiting a real decision"


def test_deciding_twice_is_refused(acfg):
    item = _enqueue(acfg)
    approvals.decide(acfg.vault, item["id"], "approve", nonce=item["nonce"])
    with pytest.raises(approvals.ApprovalError, match="already"):
        approvals.decide(acfg.vault, item["id"], "deny", nonce=item["nonce"])


def test_an_unknown_id_is_refused(acfg):
    with pytest.raises(approvals.ApprovalError, match="no approval"):
        approvals.decide(acfg.vault, "ffffffffffff", "approve", nonce="x")


def test_an_unknown_decision_is_refused(acfg):
    item = _enqueue(acfg)
    with pytest.raises(approvals.ApprovalError, match="unknown decision"):
        approvals.decide(acfg.vault, item["id"], "maybe", nonce=item["nonce"])


def test_expiry_marks_and_reports_rather_than_dropping(acfg):
    """Silently dropping a held action is the outcome with no recovery: the
    operator believes it is still waiting."""
    item = approvals.enqueue(acfg.vault, _req(), risk="tracked",
                             summary="s", stem="rec-1", ttl_hours=0.1)
    later = datetime.now(UTC) + timedelta(hours=1)
    expired = approvals.expire_stale(acfg.vault, now=later)
    assert [e["id"] for e in expired] == [item["id"]]
    assert approvals.pending(acfg.vault) == []


def _age(acfg, aid):
    """Push an item's expiry into the past through the LEDGER, the way time
    would. enqueue() clamps ttl to a positive value on purpose — it must not be
    possible to create something already dead."""
    approvals.append(acfg.vault, aid, approvals.PENDING,
                     expires_at=approvals.iso_z(datetime.now(UTC) - timedelta(hours=1)))


def test_deciding_an_expired_item_is_refused(acfg):
    item = _enqueue(acfg)
    _age(acfg, item["id"])
    with pytest.raises(approvals.ApprovalError, match="expired"):
        approvals.decide(acfg.vault, item["id"], "approve", nonce=item["nonce"])


def test_a_torn_ledger_line_does_not_hide_the_rows_around_it(acfg):
    a = _enqueue(acfg)
    with approvals.ledger_path(acfg.vault).open("a") as f:
        f.write('{"id": "zz", "stat')
    b = approvals.enqueue(acfg.vault, _req(_file="002-todo.add.json"),
                          risk="tracked", summary="second", stem="rec-1")
    ids = {x["id"] for x in approvals.pending(acfg.vault)}
    assert {a["id"], b["id"]} <= ids


# ── the outbox branch ───────────────────────────────────────────────────────
def _outbox_dir(tmp_path, verb="todo.add", **body):
    out = tmp_path / "output"
    (out / "outbox").mkdir(parents=True, exist_ok=True)
    (out / "outbox" / f"001-{verb}.json").write_text(
        json.dumps({"verb": verb, "title": "Buy milk", **body}))
    return out


def test_confirm_enqueues_and_announces(acfg, tmp_path, sent):
    out = _outbox_dir(tmp_path)
    acfg.outbox_verbs = {"todo.add": "confirm"}
    summary = outbox.process(out, acfg, log=lambda *_: None, stem="rec-9")
    assert summary["done"] == 0 and summary["refused"] == 1
    receipt = json.loads((out / "outbox-receipt.json").read_text())
    rec = receipt["receipts"][0]
    assert rec["status"] == "held" and rec["approval_id"]
    assert "awaiting approval" in rec["reason"]
    assert len(approvals.pending(acfg.vault)) == 1
    assert sent["actions"], "the operator must be asked"
    assert "Approve" in sent["actions"][0]["actions"]


def test_off_still_means_off_and_queues_nothing(acfg, tmp_path, sent):
    """A global stop must not quietly become a global 'later'."""
    out = _outbox_dir(tmp_path)
    acfg.outbox = "off"
    outbox.process(out, acfg, log=lambda *_: None, stem="rec-9")
    assert approvals.pending(acfg.vault) == []
    assert sent["actions"] == []
    rec = json.loads((out / "outbox-receipt.json").read_text())["receipts"][0]
    assert "ATTICUS_OUTBOX=off" in rec["reason"]


def test_with_no_topic_configured_confirm_behaves_exactly_as_before(acfg, tmp_path, sent):
    """The shipped default. A queue nobody configured must not start accepting
    decisions from a topic nobody chose."""
    out = _outbox_dir(tmp_path)
    acfg.approvals_enabled = False
    acfg.approval_topic_url = ""
    acfg.outbox_verbs = {"todo.add": "confirm"}
    outbox.process(out, acfg, log=lambda *_: None, stem="rec-9")
    assert approvals.pending(acfg.vault) == []
    assert sent["actions"] == []
    rec = json.loads((out / "outbox-receipt.json").read_text())["receipts"][0]
    assert "need confirmation" in rec["reason"]


def test_auto_still_performs_immediately(acfg, tmp_path, sent):
    out = _outbox_dir(tmp_path)
    acfg.outbox_verbs = {"todo.add": "auto"}
    summary = outbox.process(out, acfg, log=lambda *_: None, stem="rec-9")
    assert summary["done"] == 1
    assert approvals.pending(acfg.vault) == []


# ── the drain ───────────────────────────────────────────────────────────────
def _ntfy_feed(*payloads, base_time=1785634416):
    return "\n".join(json.dumps({"event": "message", "time": base_time + i,
                                 "message": json.dumps(p)})
                     for i, p in enumerate(payloads))


def test_a_decision_from_the_topic_is_applied_and_performed(acfg, sent, monkeypatch, tmp_path):
    monkeypatch.setattr(drain.nf, "STATE", tmp_path / "cache")
    item = _enqueue(acfg)
    monkeypatch.setattr(drain, "fetch_decisions", lambda cfg, log=print: [
        {"id": item["id"], "decision": "approve", "nonce": item["nonce"]}])
    res = drain.run(acfg, log=lambda *_: None)
    assert res["decided"] == 1 and res["performed"] == 1
    import todos
    assert [t["title"] for t in todos.open_todos(acfg.vault)] == ["Buy milk"]
    st = approvals.state(acfg.vault)[item["id"]]
    assert st["status"] == approvals.PERFORMED


def test_the_approved_action_runs_the_ORDINARY_handler_path(acfg, sent, monkeypatch, tmp_path):
    """An approval must not become a second, laxer way to reach a credential:
    the same validate() and the same handler as an unattended action."""
    monkeypatch.setattr(drain.nf, "STATE", tmp_path / "cache")
    bad = approvals.enqueue(acfg.vault,
                            {"verb": "todo.add", "_file": "001-todo.add.json"},
                            risk="tracked", summary="no title", stem="rec-1")
    approvals.decide(acfg.vault, bad["id"], "approve", nonce=bad["nonce"])
    res = drain.run(acfg, log=lambda *_: None)
    assert res["failed"] == 1 and res["performed"] == 0
    assert approvals.state(acfg.vault)[bad["id"]]["status"] == approvals.FAILED


def test_a_denied_item_is_never_performed(acfg, sent, monkeypatch, tmp_path):
    monkeypatch.setattr(drain.nf, "STATE", tmp_path / "cache")
    item = _enqueue(acfg)
    monkeypatch.setattr(drain, "fetch_decisions", lambda cfg, log=print: [
        {"id": item["id"], "decision": "deny", "nonce": item["nonce"]}])
    res = drain.run(acfg, log=lambda *_: None)
    assert res["performed"] == 0
    import todos
    assert todos.open_todos(acfg.vault) == []


def test_fetch_parses_the_ntfy_feed_and_ignores_chatter(acfg, monkeypatch, tmp_path):
    monkeypatch.setattr(drain.nf, "STATE", tmp_path / "cache")
    feed = (_ntfy_feed({"id": "abc123abc123", "decision": "approve", "nonce": "n"})
            + "\n" + json.dumps({"event": "keepalive", "time": 1})
            + "\n" + json.dumps({"event": "message", "time": 2,
                                 "message": "somebody typing on the topic"}))

    class R:
        def read(self, *_a):
            return feed.encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(drain.urllib.request, "urlopen", lambda *a, **k: R())
    got = drain.fetch_decisions(acfg, log=lambda *_: None)
    assert got == [{"id": "abc123abc123", "decision": "approve", "nonce": "n"}]


def test_an_unreachable_topic_does_not_fail_the_pass(acfg, monkeypatch, tmp_path):
    monkeypatch.setattr(drain.nf, "STATE", tmp_path / "cache")

    def boom(*a, **k):
        raise OSError("no route to host")
    monkeypatch.setattr(drain.urllib.request, "urlopen", boom)
    assert drain.fetch_decisions(acfg, log=lambda *_: None) == []


def test_poll_url_shape():
    assert drain.poll_url("https://ntfy.sh/t", "") == \
        "https://ntfy.sh/t/json?poll=1&since=12h"
    assert "since=123" in drain.poll_url("https://ntfy.sh/t/json", "123")


def test_expired_items_produce_one_grouped_alert(acfg, sent, monkeypatch, tmp_path):
    monkeypatch.setattr(drain.nf, "STATE", tmp_path / "cache")
    monkeypatch.setattr(drain, "fetch_decisions", lambda cfg, log=print: [])
    for i in range(3):
        it = approvals.enqueue(acfg.vault, _req(_file=f"00{i}-todo.add.json"),
                               risk="tracked", summary=f"item {i}", stem="rec-1")
        _age(acfg, it["id"])
    res = drain.run(acfg, log=lambda *_: None)
    assert res["expired"] == 3
    assert len([p for p in sent["pushes"] if "expired" in p["text"]]) == 1


# ── the drain must persist what it performed ────────────────────────────────

def test_a_performed_approval_is_committed_in_the_same_pass(acfg, monkeypatch):
    """The gap this closes: `approval_drain.run()` writes the ledger and may
    write the vault (image.generate puts a PNG beside its report), and the very
    next branch in main() is `if not todo: return 0`. An approval is usually
    tapped when no new recording has arrived — which is exactly the pass that
    would otherwise commit nothing and leave the deliverable unpushed."""
    import pipeline
    commits = []

    class FakeGit:
        def commit_push(self, msg):
            commits.append(msg)
            return True

    monkeypatch.setattr(pipeline.approval_drain, "run",
                        lambda cfg, log=print: {"decided": 1, "performed": 1,
                                                "expired": 0, "failed": 0})
    pipeline.drain_approvals(acfg, FakeGit(), _Log())
    assert commits and "1 performed" in commits[0]


def test_a_quiet_drain_does_not_make_an_empty_commit(acfg, monkeypatch):
    import pipeline
    commits = []

    class FakeGit:
        def commit_push(self, msg):
            commits.append(msg)
            return True

    monkeypatch.setattr(pipeline.approval_drain, "run",
                        lambda cfg, log=print: {"decided": 0, "performed": 0,
                                                "expired": 0, "failed": 0})
    pipeline.drain_approvals(acfg, FakeGit(), _Log())
    assert commits == []


def test_a_drain_that_raises_does_not_cost_the_pass_its_records(acfg, monkeypatch):
    import pipeline

    class FakeGit:
        def commit_push(self, msg):
            return True

    def boom(cfg, log=print):
        raise RuntimeError("ntfy unreachable")

    monkeypatch.setattr(pipeline.approval_drain, "run", boom)
    res = pipeline.drain_approvals(acfg, FakeGit(), _Log())
    assert res["performed"] == 0


class _Log:
    def info(self, m): pass
    def warn(self, m): pass
