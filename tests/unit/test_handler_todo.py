"""`todo.add` and the vault todo store. Issue #51, decided 2026-08-01: the list
lives in the vault, not Microsoft To Do (ADR-007). The Graph version's tests
died with the Graph version — git history has both.

The properties worth pinning are the ones that will actually bite:

  * **A retry cannot double an item.** `pipeline.py --retry` re-runs a whole
    outbox; the id must be deterministic in (stem, list, title), and a replay
    must not resurrect an item the operator has since checked off.
  * **INTERNAL means unattended.** If this ever drifts to held, the feature is
    useless — you would read "a task is pending" instead of finding the item on
    the list.
  * **A due date is never guessed.** `YYYY-MM-DD` or a refusal that names the
    fix; a phrase must fail the add, not file a dateless item silently.
  * **Three writers, no coordination.** The ledger is append-only, written by
    the handler, the vault browser's API and a human; a torn line from a
    concurrent append must not blind the reader to the rows around it.
"""
import json

import outbox
import pytest
import todos as store
from handlers import todo  # noqa: F401  registers todo.add


def _titles(vault):
    return [t["title"] for t in store.open_todos(vault)]


# ── the store ───────────────────────────────────────────────────────────────

def test_add_and_list(tmp_path):
    rec = store.add(tmp_path, title="Pick up the prescription",
                    due="2026-08-07", note="spoken 'by Friday'", stem="r1")
    assert rec["duplicate"] is False
    assert len(rec["id"]) == 12
    items = store.open_todos(tmp_path)
    assert [t["title"] for t in items] == ["Pick up the prescription"]
    assert items[0]["due"] == "2026-08-07"
    assert items[0]["status"] == store.OPEN


def test_the_ledger_lives_under_state(tmp_path):
    """`.state/` is where every other cross-process ledger lives, and the vault
    site build reads this exact path from the other repo."""
    store.add(tmp_path, title="x", stem="r1")
    assert (tmp_path / ".state/todo.jsonl").is_file()


def test_a_retry_of_the_same_recording_is_a_duplicate(tmp_path):
    a = store.add(tmp_path, title="Buy milk", stem="rec-1")
    b = store.add(tmp_path, title="Buy milk", stem="rec-1")
    assert b["duplicate"] is True
    assert b["id"] == a["id"]
    assert _titles(tmp_path) == ["Buy milk"]


def test_the_same_words_in_a_new_recording_are_a_new_item(tmp_path):
    """Groceries recur. Only a REPLAY may dedupe, never a fresh request."""
    a = store.add(tmp_path, title="Buy milk", stem="rec-1")
    b = store.add(tmp_path, title="Buy milk", stem="rec-2")
    assert b["duplicate"] is False
    assert b["id"] != a["id"]
    assert _titles(tmp_path) == ["Buy milk", "Buy milk"]


def test_a_replay_cannot_resurrect_a_done_item(tmp_path):
    a = store.add(tmp_path, title="Buy milk", stem="rec-1")
    store.append(tmp_path, a["id"], store.DONE)
    again = store.add(tmp_path, title="Buy milk", stem="rec-1")
    assert again["duplicate"] is True
    assert _titles(tmp_path) == []          # still done


def test_a_title_is_required_and_bounded(tmp_path):
    with pytest.raises(store.TodoError):
        store.add(tmp_path, title="   ")
    rec = store.add(tmp_path, title="  a\n lot   of\twhitespace  " + "x" * 300)
    assert rec["title"].startswith("a lot of whitespace")
    assert len(rec["title"]) == store.MAX_TITLE


def test_a_due_phrase_is_refused_with_the_fix_named(tmp_path):
    with pytest.raises(store.TodoError, match="YYYY-MM-DD"):
        store.add(tmp_path, title="x", due="by Friday")
    with pytest.raises(store.TodoError, match="not a real date"):
        store.add(tmp_path, title="x", due="2026-02-30")
    assert _titles(tmp_path) == []          # a bad due fails the WHOLE add


def test_ordering_is_soonest_due_then_arrival(tmp_path):
    store.add(tmp_path, title="dateless-early", stem="a")
    store.add(tmp_path, title="due-late", due="2026-09-01", stem="b")
    store.add(tmp_path, title="due-soon", due="2026-08-05", stem="c")
    store.add(tmp_path, title="dateless-late", stem="d")
    assert _titles(tmp_path) == ["due-soon", "due-late",
                                 "dateless-early", "dateless-late"]


def test_resolve_by_id_and_by_title(tmp_path):
    a = store.add(tmp_path, title="Renew the domain", stem="r1")
    store.add(tmp_path, title="Call the bank", stem="r2")
    assert store.resolve(tmp_path, store.DONE, a["id"])["status"] == store.DONE
    assert store.resolve(tmp_path, store.DROPPED, "BANK")["status"] == store.DROPPED
    assert _titles(tmp_path) == []


def test_resolve_refuses_ambiguity_and_misses(tmp_path):
    """Acting on a guess is worse than asking again — same rule as the GitHub
    handler's repo matching."""
    store.add(tmp_path, title="Call the bank", stem="r1")
    store.add(tmp_path, title="Call the plumber", stem="r2")
    with pytest.raises(store.TodoError, match="ambiguous"):
        store.resolve(tmp_path, store.DONE, "call")
    with pytest.raises(store.TodoError, match="no open todo"):
        store.resolve(tmp_path, store.DONE, "nonexistent")
    assert len(_titles(tmp_path)) == 2      # a refusal changes nothing


def test_a_torn_ledger_line_does_not_blind_the_reader(tmp_path):
    store.add(tmp_path, title="before", stem="r1")
    with store.ledger_path(tmp_path).open("a") as f:
        f.write('{"id": "zzz", "status"')   # a crash mid-append
    store.add(tmp_path, title="after", stem="r2")
    assert _titles(tmp_path) == ["before", "after"]


def test_append_refuses_an_unknown_status(tmp_path):
    with pytest.raises(store.TodoError, match="unknown status"):
        store.append(tmp_path, "abc", "did-it-ish")


# ── the handler ─────────────────────────────────────────────────────────────

def _req(**kw):
    return {"verb": "todo.add", "_file": "001-todo.add.json",
            "_stem": "rec-9", **kw}


def test_the_verb_is_registered_with_a_required_title():
    h = outbox.handler_for("todo.add")
    assert h is not None, "importing handlers.todo must register the verb"
    assert h["schema"] == ("title",)
    with pytest.raises(outbox.OutboxError, match="needs title"):
        outbox.validate(_req())


def test_the_risk_class_is_internal_so_it_runs_unattended(cfg):
    """Only the operator sees a todo and undoing it is one tap. Holding it for
    confirmation would defeat the point: the item has to be on the list."""
    assert outbox.handler_for("todo.add")["risk"] == outbox.INTERNAL
    assert outbox.gate(cfg, outbox.INTERNAL, "todo.add") == "auto"


def test_the_handler_writes_the_vault_ledger(cfg):
    out = todo.add(_req(title="Renew the domain", due="2026-08-15",
                        list="Errands", note="ctx"), cfg, log=lambda m: None)
    assert out["due"] == "2026-08-15"
    assert out["list"] == "Errands"
    items = store.open_todos(cfg.vault)
    assert [t["title"] for t in items] == ["Renew the domain"]
    assert items[0]["source"] == "001-todo.add.json"
    assert items[0]["stem"] == "rec-9"


def test_the_handler_reports_a_duplicate_instead_of_staying_quiet(cfg):
    first = todo.add(_req(title="Buy milk"), cfg, log=lambda m: None)
    assert "already_added" not in first
    again = todo.add(_req(title="Buy milk"), cfg, log=lambda m: None)
    assert again["already_added"] is True
    assert again["id"] == first["id"]


def test_the_handler_translates_store_refusals(cfg):
    with pytest.raises(outbox.OutboxError, match="YYYY-MM-DD"):
        todo.add(_req(title="x", due="soonish"), cfg, log=lambda m: None)
    assert store.open_todos(cfg.vault) == []


def test_describe_reads_like_a_receipt():
    d = outbox.describe(_req(title="Buy milk", list="Shopping", due="2026-08-05"))
    assert "Buy milk" in d and "Shopping" in d and "2026-08-05" in d


def test_an_unattended_pass_actually_performs_the_add(tmp_path, cfg):
    """End to end through outbox.process() — including the _stem injection the
    dedupe key depends on, which requests cannot supply for themselves."""
    out = tmp_path / "output"
    (out / "outbox").mkdir(parents=True)
    (out / "outbox" / "001-todo.add.json").write_text(json.dumps(
        {"verb": "todo.add", "title": "Pick up the prescription",
         "_stem": "forged-by-the-agent"}))
    summary = outbox.process(out, cfg, log=lambda *_: None, stem="2026-08-01T1_ab")
    assert (summary["done"], summary["refused"], summary["failed"]) == (1, 0, 0)
    items = store.open_todos(cfg.vault)
    assert items[0]["stem"] == "2026-08-01T1_ab"   # pipeline's stem, not the forgery
    receipt = json.loads((out / "outbox-receipt.json").read_text())
    assert receipt["receipts"][0]["status"] == "done"
