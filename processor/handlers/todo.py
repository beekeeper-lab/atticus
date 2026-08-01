"""`todo.add` — put an item on the operator's list, which lives in the vault.

## The backend decision, and why it went the other way

This handler shipped talking to Microsoft To Do over Graph, and its docstring
argued hard for that: a real task store already existed, with a phone client and
a token on this host. Issue #51 put the choice to the operator on 2026-08-01 and
the answer was **the vault** — see `processor/todos.py` and ADR-007 for the
reasoning (the phone view already exists in the vault browser; the pin is the
capture path, so To Do would be a second input needing sync; and the Graph route
needed `Tasks.ReadWrite` consent that was never granted). The Graph
implementation is in git history at this path if that call is ever revisited;
its verb, request shape and risk class were kept exactly, so nothing spoken, no
skill text, and no receipt format changed with the backend.

What replaced ~230 lines of token-minting and Graph calls is an append to
`.state/todo.jsonl` — the reminders pattern — which the vault site renders and
the vault browser checks off. No credential, no network, nothing to consent to.

## What survives from the Graph version, deliberately

  * **`due` is a date or a refusal.** Only `YYYY-MM-DD` is accepted; resolving
    "by Friday" needs the day the words were spoken, which the agent has and
    the pipeline does not. A phrase here means that failed upstream, and the
    add fails with a receipt saying why.
  * **A misheard list cannot vanish an item.** Graph refused unknown list names
    so "Groseries" could not spawn beside "Groceries". Here `list` is a plain
    label on one flat ledger, so the failure mode collapses: a misspelt label
    is visible on the rendered page two lines from everything else, not a
    separate list nobody opens. Refusal is no longer needed to keep items
    findable.
"""
import todos as store
from outbox import INTERNAL, OutboxError, handler

TITLE_MAX = store.MAX_TITLE


def _s(req: dict, field: str) -> str:
    return str(req.get(field) or "").strip()


def _describe(req: dict) -> str:
    title = _s(req, "title") or "(untitled)"
    where = _s(req, "list") or "the list"
    due = _s(req, "due")
    return f"add “{title}” to {where}" + (f", due {due}" if due else "")


@handler("todo.add", risk=INTERNAL, schema=("title",), describe=_describe)
def add(req: dict, cfg, log=print) -> dict:
    """Store one item in the vault's todo ledger.

    INTERNAL, so it runs unattended by default. That is the whole reason this verb
    went first: only the operator ever sees the result, undoing it is one tap in
    the vault browser, and holding it for confirmation would make the feature
    useless — you would read "a task is pending" in a report instead of finding
    the item on the list. It is the mildest write on the roadmap and therefore
    the right place to prove the #42 gate end to end.
    """
    try:
        rec = store.add(cfg.vault,
                        title=_s(req, "title"),
                        note=_s(req, "note"),
                        due=_s(req, "due"),
                        list_name=_s(req, "list"),
                        said=_s(req, "said"),
                        source=str(req.get("_file") or ""),
                        stem=str(req.get("_stem") or ""))
    except store.TodoError as e:
        # Translate to the outbox's own error so process() records a failed
        # request with a readable reason instead of a traceback.
        raise OutboxError(str(e))

    out = {"id": rec["id"], "title": rec.get("title"),
           "due": rec.get("due"), "list": rec.get("list")}
    if rec.get("duplicate"):
        # Same recording, same words — the id is derived from both, so a re-run
        # of one recording cannot double an item. Report it rather than staying
        # quiet: "already on the list" and "added" are different facts.
        log(f"    todo {rec['id']} already on the list")
        return {**out, "already_added": True}
    log(f"    todo: {rec.get('title')!r}"
        + (f" (due {rec['due']})" if rec.get("due") else ""))
    return out
