"""Turn "the research I started this morning" into one recording. Issue #82.

Every lifecycle verb — status, cancel, retry — needs a *referent*, and the agent
cannot look one up: it has no reads. The pattern that solves this is already
established twice over. `contacts.py` resolves "Robbie" to an address
pipeline-side (ADR-006), and `github.close` resolves words to an issue number
the same way. Both work because a lookup the AGENT cannot do is perfectly fine
for a HANDLER, which runs where the data already is.

So the agent emits the words it heard, and this resolves them after the agent
has exited.

## The rules, which are the same rules used everywhere else here

  * **Refuse rather than guess.** Nobody is present to disambiguate, and
    cancelling the wrong recording destroys work the operator asked for. No
    match and several matches both fail, naming what was found.
  * **A bounded window.** Only the last `within_days` (default 7). "That thing"
    means something recent; searching all history makes ambiguity certain and
    makes an old recording reachable by a stray phrase.
  * **Never the recording that is asking.** A command spoken into recording X
    must not be able to act on X — see `exclude_stem`. Without it, "cancel that"
    kills the run performing the cancellation, which then never records it: the
    operator sees nothing happen and cannot tell why.

## What it matches on

The transcript, the deliverable's title, and a small set of time words, scored
so that a specific phrase beats a vague one. Deliberately not fuzzy: an
edit-distance match on speech would make "the report" reach anything.
"""
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from vault import CANCELLED, SUPERSEDED, TERMINAL, load_records

# What a lookup skips unless told otherwise. Callers vary, and the
# variation is meaningful rather than incidental:
#
#   status  skips nothing — 'what happened to that' is most often asked
#           about something already finished;
#   cancel  skips only DONE_WITH, because cancelling a PUBLISHED record
#           is the supersede path and must stay reachable;
#   retry   the same — re-running a published recording is legitimate.
#
# Only cancelled and superseded are truly off-limits: the operator has
# already said stop, and a second stop is not a thing to act on.
DONE_WITH = (CANCELLED, SUPERSEDED)

# "the last one", "that one", "it" — a request with no distinguishing words at
# all. Answered by the most recent candidate rather than refused, because it is
# what the phrase actually means and there is no ambiguity to resolve.
_LATEST = re.compile(
    r"^\s*(the\s+)?(last|latest|most\s+recent|previous|that|it|this)"
    r"(\s+(one|thing|task|request|command|recording))?\s*$", re.I)

_STOPWORDS = {
    "the", "a", "an", "that", "this", "it", "one", "thing", "my", "for", "me",
    "about", "on", "of", "to", "and", "please", "atticus", "you", "your",
    "from", "with", "was", "were", "is", "are", "did", "do", "just", "some",
}


class ResolveError(Exception):
    """No single recording matched. The message names what was found."""


def _title_of(rec, vault: Path) -> str:
    """The deliverable's title if there is one, else the first words spoken."""
    outdir = rec.outdir(vault)
    try:
        for p in sorted(outdir.glob("*.html")):
            head = p.read_text(errors="replace")[:4000]
            m = re.search(r"(?is)<title[^>]*>(.*?)</title>", head)
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()
    except OSError:
        pass
    return ""


def _transcript_of(rec, vault: Path) -> str:
    try:
        p = rec.transcript_path(vault)
        return p.read_text(errors="replace") if p.is_file() else ""
    except OSError:
        return ""


def _age_hours(rec, now: datetime) -> float:
    raw = str(rec.data.get("recorded_at") or "")
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    return (now - when).total_seconds() / 3600.0


def candidates(vault: Path, *, within_days: int = 7, exclude_stem: str = "",
               skip_status=TERMINAL, now: datetime | None = None) -> list:
    now = now or datetime.now(UTC)
    out = []
    for rec in load_records(Path(vault)):
        if rec.stem == exclude_stem:
            continue
        if skip_status and rec.data.get("status") in skip_status:
            continue
        if within_days and _age_hours(rec, now) > within_days * 24:
            continue
        out.append(rec)
    out.sort(key=lambda r: str(r.data.get("recorded_at") or ""), reverse=True)
    return out


def _score(phrase: str, rec, vault: Path) -> int:
    """How well this recording answers the phrase. 0 means "not a match".

    Scored rather than boolean so a phrase appearing in a TITLE outranks the
    same phrase buried in a transcript — the title is what the operator saw in
    a notification, so it is what they are most likely quoting back.
    """
    low = phrase.lower()
    title = _title_of(rec, vault).lower()
    text = _transcript_of(rec, vault).lower()
    words = [w for w in re.split(r"\W+", low) if w and w not in _STOPWORDS]
    if not words:
        return 0

    score = 0
    if low in title:
        score += 100
    if low in text:
        score += 60
    if words and all(w in title for w in words):
        score += 40
    if words and all(w in text for w in words):
        score += 20
    # Partial word coverage, so "consulting research" still finds a title that
    # says "consulting work" — but only enough to break a tie, never enough to
    # win on its own.
    hits = sum(1 for w in words if w in title or w in text)
    if hits == len(words):
        score += 10
    elif hits >= max(2, len(words) - 1):
        score += 3
    return score


def resolve(vault: Path, phrase: str, *, within_days: int = 7,
            exclude_stem: str = "", skip_status=TERMINAL,
            now: datetime | None = None):
    """One recording, or ResolveError. Never guesses between two."""
    vault = Path(vault)
    phrase = " ".join(str(phrase or "").split())
    pool = candidates(vault, within_days=within_days, exclude_stem=exclude_stem,
                      skip_status=skip_status, now=now)
    if not pool:
        raise ResolveError(
            f"no recording in the last {within_days} days to act on")

    if not phrase or _LATEST.match(phrase):
        return pool[0]

    scored = [(s, r) for r in pool if (s := _score(phrase, r, vault)) > 0]
    if not scored:
        recent = ", ".join(f"{_label(r, vault)!r}" for r in pool[:3])
        raise ResolveError(
            f"nothing in the last {within_days} days matches {phrase!r}. "
            f"Most recent: {recent}")
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].data.get("recorded_at") or "")))
    best = scored[0][0]
    tied = [r for s, r in scored if s == best]
    if len(tied) > 1:
        names = "; ".join(_label(r, vault) for r in tied[:4])
        raise ResolveError(
            f"{phrase!r} matches {len(tied)} recordings, so nothing was done: "
            f"{names}. Be more specific.")
    return tied[0]


def _label(rec, vault: Path) -> str:
    """What to call a recording when talking to the operator."""
    title = _title_of(rec, vault)
    if title:
        return title[:70]
    text = " ".join(_transcript_of(rec, vault).split())
    return (text[:60] + "…") if text else rec.stem


def describe(rec, vault: Path) -> str:
    """A one-line status line, for `atticus.status` and for error messages."""
    d = rec.data
    status = str(d.get("status") or "?")
    when = str(d.get("recorded_at") or "?")
    line = f"{_label(rec, vault)} — {status}, recorded {when}"
    if status == "published" and d.get("output_files"):
        line += f", {d['output_files']} file(s)"
    if d.get("gate_reason"):
        line += f" (not executed: {d['gate_reason']})"
    return line


def load_by_stem(vault: Path, stem: str):
    for rec in load_records(Path(vault)):
        if rec.stem == stem:
            return rec
    return None


def summary_json(rec) -> str:
    return json.dumps({k: rec.data.get(k) for k in
                       ("status", "recorded_at", "executed", "output_files")},
                      sort_keys=True)
