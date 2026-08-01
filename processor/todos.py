"""The todo list. It lives in the vault, and that was a decision, not a default.

Issue #51 weighed this against Microsoft To Do — an existing store with a real
phone app — and the operator picked the vault, for reasons worth keeping with the
code (ADR-007 has the long form):

  * **The phone requirement was already met.** The vault browser works on the
    phone over Tailscale; adopting To Do would add a second app for a view that
    exists.
  * **Capture is the pin.** The classic objection to a self-hosted list — adding
    to it is slow — does not apply when adding to it is speaking. To Do would be
    a SECOND input path and a sync question between the two.
  * **No credential.** The To Do path needed `Tasks.ReadWrite` on a token whose
    whole advertised contract is read-only. This file needs nothing.

The honest cost, also from the issue: no offline access. If Forge or Tailscale is
down, the list is unreachable; To Do works on a plane.

## The shape

An append-only JSONL ledger, `.state/todo.jsonl`, exactly like the reminders
ledger one file over: the first event carries the item, later events carry only
what changed, and folding them newest-last gives the current state. Append-only
is what makes THREE writers safe without coordination — the outbox handler
(pipeline side), the vault browser's write API (`site/api.py`, the check-off),
and a human with a text editor — and it means "when did I finish that" is `git
log`, not a feature.

The id is derived from (recording stem, list, title), so re-running one
recording's outbox — `pipeline.py --retry` — cannot double an item, while the
same words spoken in a fresh recording legitimately create a fresh one.

Rendering is the vault site's job (`site/build.py` in the vault repo reads this
same file); delivery needs no drain and no timer, because a list, unlike a
reminder, does not fire.
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

LEDGER = ".state/todo.jsonl"

OPEN, DONE, DROPPED = "open", "done", "dropped"
STATUSES = (OPEN, DONE, DROPPED)

# One line each: the title is a list row, the note is a detail line under it.
# Bounded because both end up inside a rendered page and a git diff.
MAX_TITLE = 255
MAX_NOTE = 1000
MAX_LIST = 60

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TodoError(Exception):
    """A request this store refuses. The message is operator-readable."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_path(vault: Path) -> Path:
    return Path(vault) / LEDGER


def clean_title(text) -> str:
    t = " ".join(str(text or "").split())
    if not t:
        raise TodoError("a todo needs a title")
    return t[:MAX_TITLE]


def clean_note(text) -> str:
    return " ".join(str(text or "").split())[:MAX_NOTE]


def clean_list(text) -> str:
    return " ".join(str(text or "").split())[:MAX_LIST]


def clean_due(raw) -> str:
    """A calendar date or nothing — never a phrase.

    "By Friday" is the agent's to resolve, on the day the words were spoken; a
    phrase reaching this store means that failed, and no due date is the honest
    record of it. A refused due date fails the whole add so the receipt says why,
    rather than quietly filing the item dateless.
    """
    raw = str(raw or "").strip()
    if not raw:
        return ""
    if not _DATE.match(raw):
        raise TodoError(
            f"due must be a calendar date as YYYY-MM-DD, got {raw!r} — resolve a "
            f"spoken phrase like 'by Friday' to a date before writing the request, "
            f"or leave due out and put the wording in the note")
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise TodoError(f"due {raw!r} is not a real date")
    return raw


def todo_id(stem: str, list_name: str, title: str) -> str:
    """Deterministic in (recording, list, title).

    Same reasoning as `reminders.reminder_id`: `pipeline.py --retry <id>` re-runs
    a record's outbox, and nothing may be processed twice — so a retry must land
    on the same id and read as a duplicate. The stem is in the key so that the
    same errand spoken next week (a new recording) is a new item, not a refused
    duplicate of one long since done.
    """
    h = hashlib.sha256(f"{stem}|{list_name}|{title}".encode()).hexdigest()
    return h[:12]


def _events(vault: Path) -> list[dict]:
    p = ledger_path(vault)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            # A torn final line from a concurrent append must not blind the
            # reader to the rows around it — same rule as the other ledgers.
            continue
        if isinstance(d, dict) and d.get("id"):
            out.append(d)
    return out


def state(vault: Path) -> dict[str, dict]:
    """id -> the item as it now stands: first event, updated by later ones."""
    out: dict[str, dict] = {}
    for ev in _events(vault):
        cur = out.setdefault(ev["id"], {})
        cur.update({k: v for k, v in ev.items() if v is not None})
    return out


def open_todos(vault: Path) -> list[dict]:
    """Everything still to do: dated items first (soonest due on top), then the
    dateless in the order they were added."""
    items = [t for t in state(vault).values() if t.get("status") == OPEN]
    return sorted(items, key=lambda t: (not t.get("due"),
                                        str(t.get("due") or ""),
                                        str(t.get("added_at") or "")))


def append(vault: Path, tid: str, status: str, **fields) -> dict:
    if status not in STATUSES:
        raise TodoError(f"unknown status {status!r}")
    ev = {"id": tid, "status": status, "event_at": iso_z(_utcnow())}
    ev.update({k: v for k, v in fields.items() if v is not None})
    p = ledger_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    # If a previous writer died mid-line, the file has no trailing newline and a
    # bare append would glue this event onto the torn one — losing BOTH, since
    # the combined line parses as neither. Start on a fresh line instead: the
    # torn line stays torn (readers already skip it), this event survives.
    lead = ""
    try:
        with p.open("rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                lead = "\n"
    except OSError:
        pass                                    # empty or absent: no lead needed
    with p.open("a") as f:
        f.write(lead + json.dumps(ev, sort_keys=True) + "\n")
    return ev


def add(vault: Path, *, title: str, note: str = "", due: str = "",
        list_name: str = "", said: str = "", source: str = "",
        stem: str = "") -> dict:
    """Store one item. Returns it, with `duplicate` set if it was already there.

    A duplicate is reported whatever its current status: an id can only repeat on
    a re-run of the same recording, and if the operator has since checked the item
    off, resurrecting it would undo their action on a replay.
    """
    title = clean_title(title)
    list_name = clean_list(list_name)
    tid = todo_id(stem, list_name, title)
    existing = state(vault).get(tid)
    if existing is not None:
        return {**existing, "id": tid, "duplicate": True}
    now = iso_z(_utcnow())
    return {**append(vault, tid, OPEN,
                     title=title, added_at=now,
                     note=clean_note(note) or None,
                     due=clean_due(due) or None,
                     list=list_name or None,
                     said=" ".join(str(said or "").split()) or None,
                     source=source or None, stem=stem or None),
            "duplicate": False}


def resolve(vault: Path, status: str, ref: str) -> dict:
    """Mark one OPEN item done/dropped, by id or by unambiguous title match.

    Refuses an ambiguous title the same way the GitHub handler refuses an
    ambiguous repo: acting on a guess is worse than asking again.
    """
    ref = " ".join(str(ref or "").split())
    if not ref:
        raise TodoError("say which item, by id or title")
    items = open_todos(vault)
    hits = [t for t in items if t.get("id") == ref]
    if not hits:
        low = ref.lower()
        hits = [t for t in items if low in str(t.get("title") or "").lower()]
    if not hits:
        raise TodoError(f"no open todo matches {ref!r}")
    if len(hits) > 1:
        titles = "; ".join(str(t.get("title")) for t in hits[:5])
        raise TodoError(f"{ref!r} is ambiguous — it matches: {titles}")
    return append(vault, hits[0]["id"], status, title=hits[0].get("title"))


# ---------------------------------------------------------------------------
#  CLI — a terminal view of the list, and check-off without a browser
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", type=Path, help="alternate ops/.env")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="show open todos (the default)")
    p_add = sub.add_parser("add", help="add an item from the terminal")
    p_add.add_argument("title")
    p_add.add_argument("--due", default="", help="YYYY-MM-DD")
    p_add.add_argument("--note", default="")
    p_add.add_argument("--list", default="", dest="list_name")
    for name, help_ in (("done", "check an item off"), ("drop", "remove one unfinished")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("ref", help="id or part of the title")
    args = ap.parse_args(argv)

    from config import Config
    try:
        cfg = Config(args.env)
    except Exception as e:                          # noqa: BLE001
        print(f"config error: {e}", file=sys.stderr)
        return 2
    vault = Path(cfg.vault)
    if not vault.is_dir():
        print(f"vault not found: {vault}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "add":
            rec = add(vault, title=args.title, note=args.note,
                      due=args.due, list_name=args.list_name)
            print(("already there: " if rec.get("duplicate") else "added: ")
                  + f"{rec['id']}  {rec.get('title')}")
        elif args.cmd in ("done", "drop"):
            ev = resolve(vault, DONE if args.cmd == "done" else DROPPED, args.ref)
            print(f"{ev['status']}: {ev['id']}  {ev.get('title')}")
        else:
            items = open_todos(vault)
            if not items:
                print("nothing to do")
                return 0
            print(f"{len(items)} open item(s)")
            for t in items:
                due = f"due {t['due']}  " if t.get("due") else ""
                where = f"  [{t['list']}]" if t.get("list") else ""
                print(f"  {t['id']}  {due}{t.get('title')}{where}")
    except TodoError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
