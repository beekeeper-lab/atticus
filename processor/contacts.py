"""Contact resolution: turn a spoken name into candidate people, ranked.

Resolves issue #43.

## Why this is pipeline-side infrastructure and NOT a skill

The obvious reading of #43 is "give the agent a `contacts` skill". That cannot
work today, and the reason is the one `outbox.py` spells out: the agent holds no
credentials, and a resolver is a **read**. `wrap_sandbox()` binds the `claude`
binary and two named skill directories — no `~/.secrets/m365*.json`, so no
`m365`, so nothing for a skill to call. The outbox does not help either: it moves
*intents* out to a credentialed process after the agent has exited, which is
exactly the wrong shape for a lookup the agent needs an answer to *during* its
run.

So this module sits on the credentialed side and is called by **outbox handlers**,
not by the agent:

    agent, sandboxed        writes {"verb": "signal.send", "to": "Robbie", …}
    pipeline, credentialed  contacts.resolve("Robbie", channel="signal")
                            → one confident match: send
                            → zero, or several: refuse, and say so in the receipt

That works with the architecture that exists. An agent-facing lookup needs a
credential-holding loopback broker the agent can query, which does not exist and
which `outbox.py` explicitly defers as "a separate decision with a worse risk
profile". Naming a person is also a much narrower need than arbitrary reads: the
agent already knows it wants to message "Robbie" — it does not need to *see* the
address book to say so. See ADR-006.

## Why it returns a list and never a string

Two Robbies is the normal case. And the input is a transcript, so the name itself
may be wrong — this project has logged "Atticus" arriving as "Advocates",
"Abacus" and "Artemis", and a mangled *person's* name has no wake-word
adjudicator behind it. A resolver that returns `"+15551234567"` has silently made
a decision whose failure mode is a private message to the wrong person, which is
the highest-consequence failure on the roadmap.

`resolve()` therefore returns a ranked `list[Match]` with a confidence, a match
tier and a source, and the caller decides. `unambiguous()` is the decision most
callers want, and it **refuses** rather than guessing: it yields a match only
when the top candidate is an exact-tier hit AND is clear of the runner-up by a
margin. Refusing is correct here; guessing is not.

## Scoring: tiers are bands, so phonetics can never beat spelling

Every candidate lands in exactly one tier, and the tiers occupy **disjoint
confidence bands**:

    exact     0.75 – 0.99   the spoken name IS the person's name, or one token of it
    partial   0.45 – 0.65   a prefix, or close spelling
    phonetic  0.20 – 0.39   sounds the same (metaphone), spelled differently

Within a band, quality signals (name similarity, source priority, the source's
own relevance rank, recency of interaction) only move a candidate *inside* its
band. So a phonetic hit cannot outrank an exact match no matter how well the
source likes it, and — because the default confidence floor equals the bottom of
the exact band — nothing below exact tier is ever auto-chosen for a send.

## Sources

In order of value, and pluggable via `register_source()`:

  `m365:people`    Graph `/me/people`, relevance-ranked by real interaction —
                   better than an address book, and the only ranked source
  `m365:contacts`  Outlook address book, unranked
  `git:log`        `git log --format='%aN <%aE>'`, off unless repos are configured

Only the m365 pair is on by default, because m365 is the one source that works
today. Adding a source is a function plus a name in `ATTICUS_CONTACTS_SOURCES`.

## Cache

Resolutions are cached in `~/.cache/atticus/contacts.json` **with the winning
source recorded**, so a bad resolution is diagnosable after the fact: the entry
says what was asked, what came back, which source produced the winner and which
tier it matched on. Diagnose with `python -m contacts resolve "Robbie" --json`.
"""
import json
import re
import shutil
import subprocess
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

CACHE = Path.home() / ".cache/atticus/contacts.json"

# Tier bands. Disjoint by construction: base + span never reaches the next base.
EXACT, PARTIAL, PHONETIC = "exact", "partial", "phonetic"
_BAND = {EXACT: (0.75, 0.24), PARTIAL: (0.45, 0.20), PHONETIC: (0.20, 0.19)}
_TIER_ORDER = {EXACT: 0, PARTIAL: 1, PHONETIC: 2}

# Which handle kind reaches a person on a given channel. A channel we cannot
# produce a handle for is not an error — "we know who Robbie is but cannot reach
# him on Signal" is a different diagnosis from "no Robbie", and callers need to
# tell them apart, so such candidates are returned unaddressable rather than
# dropped.
CHANNEL_HANDLE = {
    "email": "email", "mail": "email", "outlook": "email", "teams": "email",
    "signal": "phone", "sms": "phone", "whatsapp": "phone", "text": "phone",
    "slack": "slack",
}

_DEFAULTS = {
    "sources": "m365:people,m365:contacts",
    "m365_accounts": "default,organservices",
    "m365_limit": 25,
    "timeout": 20,
    "cache_ttl_hours": 168,
    "min_confidence": 0.75,
    "ambiguity_margin": 0.15,
    "phonetic": True,
    "max_results": 8,
    "git_repos": "",
    "git_max_commits": 2000,
}

_VOWELS = "AEIOU"
_SOURCES: dict[str, object] = {}


def _in(ch: str, group: str) -> bool:
    """Membership for a possibly-absent neighbouring letter.

    `"" in "AEIOU"` is True in Python, and metaphone asks about the letter
    before/after the current one constantly. Written naively, that made every
    word-final Y a vowel neighbour ("Robby" → RBY, so it no longer rhymed with
    "Robbie" → RB) and every word-initial H silent ("Harry" → R). Every such
    test goes through here.
    """
    return bool(ch) and ch in group


class ContactError(Exception):
    """A source could not be consulted. Never fatal — resolution degrades."""


@dataclass
class Match:
    """One candidate person, with everything needed to audit the decision."""
    name: str
    handle: str = ""                    # the handle for the requested channel, if any
    channel: str = ""                   # the channel that handle serves ("" = none asked)
    source: str = ""                    # provenance: which source produced this
    confidence: float = 0.0
    tier: str = ""                      # exact | partial | phonetic
    matched_on: str = ""                # the stored token the query matched
    handles: dict = field(default_factory=dict)
    rank: int | None = None             # the source's own relevance position, if ranked
    last_interaction: str | None = None
    company: str = ""
    also_seen: tuple = ()               # other sources that reported the same person
    note: str = ""

    @property
    def addressable(self) -> bool:
        return bool(self.handle)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["also_seen"] = list(self.also_seen)
        d["addressable"] = self.addressable
        return d

    def describe(self) -> str:
        who = f"{self.name} <{self.handle}>" if self.handle else self.name
        return f"{who} — {self.tier} match from {self.source}, confidence {self.confidence:.2f}"


# ---------------------------------------------------------------- settings ---
def _settings(cfg) -> dict:
    """Read `contacts_*` off cfg, tolerating a cfg that predates every setting."""
    out = {}
    for k, dflt in _DEFAULTS.items():
        v = getattr(cfg, f"contacts_{k}", None) if cfg is not None else None
        if v is None or v == "":
            out[k] = dflt
            continue
        if isinstance(dflt, bool):
            out[k] = str(v).strip().lower() not in ("0", "off", "no", "false")
        elif isinstance(dflt, int):
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                out[k] = dflt
        elif isinstance(dflt, float):
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                out[k] = dflt
        elif isinstance(v, (list, tuple, set)):
            # config.py parses comma settings into a LIST; the defaults here are
            # comma strings that _csv() splits later. Without this join, str(v)
            # produced "['m365:people', 'm365:contacts']" and _csv split THAT into
            # "['m365:people'" — so every source name was garbage and the resolver
            # silently returned nothing. Silently, because a source that cannot be
            # found is indistinguishable from a source that found no one.
            out[k] = ",".join(str(x) for x in v)
        else:
            out[k] = str(v)
    return out


def _csv(s) -> list[str]:
    return [p.strip() for p in re.split(r"[,;]", str(s or "")) if p.strip()]


# --------------------------------------------------------------- phonetics ---
def _ascii(s: str) -> str:
    """Fold accents, so 'Zoë' and 'Zoe' compare as the same word."""
    return "".join(c for c in unicodedata.normalize("NFKD", str(s or ""))
                   if not unicodedata.combining(c))


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", _ascii(s).lower()).strip()


def metaphone(word: str) -> str:
    """A compact Metaphone. Stdlib only — no dependency for one algorithm.

    Not a full Double Metaphone; it is a *sound key* whose only job is to make
    transcription damage comparable ("Robby"→"Robbie", "Catherine"→"Kathryn").
    Because phonetic hits score in their own band, an imperfect key can never
    promote a wrong person above a correctly spelled one — the worst it can do is
    add or miss a low-confidence candidate the caller must already disambiguate.
    """
    w = re.sub(r"[^A-Z]", "", _ascii(word).upper())
    if not w:
        return ""
    if w[:2] in ("AE", "GN", "KN", "PN", "WR"):
        w = w[1:]
    elif w[0] == "X":
        w = "S" + w[1:]
    elif w[:2] == "WH":
        w = "W" + w[2:]

    out, i, n = [], 0, len(w)
    while i < n:
        c = w[i]
        prev = w[i - 1] if i else ""
        nxt = w[i + 1] if i + 1 < n else ""
        nxt2 = w[i + 2] if i + 2 < n else ""
        if c == prev and c != "C":
            i += 1
            continue
        if c in _VOWELS:
            if i == 0:
                out.append(c)
        elif c == "B":
            if not (i == n - 1 and prev == "M"):
                out.append("B")
        elif c == "C":
            if nxt == "I" and nxt2 == "A":
                out.append("X")
            elif nxt == "H":
                out.append("K" if prev == "S" else "X")
                i += 1
            elif _in(nxt, "IEY"):
                out.append("S")
            else:
                out.append("K")
        elif c == "D":
            if nxt == "G" and _in(nxt2, "IEY"):
                out.append("J")
                i += 2
            else:
                out.append("T")
        elif c == "G":
            if nxt == "H":
                if _in(nxt2, _VOWELS):
                    out.append("K")
                i += 1
            elif nxt == "N":
                pass                        # GN: the G is silent
            elif _in(nxt, "IEY"):
                out.append("J")
            else:
                out.append("K")
        elif c == "H":
            if _in(prev, _VOWELS) and not _in(nxt, _VOWELS):
                pass
            elif _in(prev, "CSPTG"):
                pass
            else:
                out.append("H")
        elif c in "FJLMNR":
            out.append(c)
        elif c == "K":
            if prev != "C":
                out.append("K")
        elif c == "P":
            out.append("F" if nxt == "H" else "P")
            if nxt == "H":
                i += 1
        elif c == "Q":
            out.append("K")
        elif c == "S":
            if nxt == "H":
                out.append("X")
                i += 1
            elif nxt == "I" and _in(nxt2, "OA"):
                out.append("X")
            else:
                out.append("S")
        elif c == "T":
            if nxt == "H":
                out.append("0")
                i += 1
            elif nxt == "I" and _in(nxt2, "OA"):
                out.append("X")
            else:
                out.append("T")
        elif c == "V":
            out.append("F")
        elif c == "W":
            if _in(nxt, _VOWELS):
                out.append("W")
        elif c == "X":
            out.append("KS")
        elif c == "Y":
            if _in(nxt, _VOWELS):
                out.append("Y")
        elif c == "Z":
            out.append("S")
        i += 1
    return "".join(out)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


# ----------------------------------------------------------------- sources ---
def register_source(name: str, fn) -> None:
    """Register a source. `fn(query, settings) -> list[dict]`, best first.

    Each dict is a raw person: `name`, optional `emails`, `phones`, `slack`,
    `company`, `last_interaction`, and `ranked` (True when the source's own order
    means relevance). Sources raise `ContactError` when unavailable; resolution
    continues with whatever else answered.
    """
    _SOURCES[name] = fn


def _run(cmd: list[str], timeout: int) -> str:
    exe = shutil.which(cmd[0])
    if not exe:
        raise ContactError(f"{cmd[0]} is not on PATH")
    try:
        p = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True,
                           timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise ContactError(f"{cmd[0]} timed out after {timeout}s")
    except OSError as e:
        raise ContactError(f"{cmd[0]} failed to start: {e}")
    if p.returncode != 0:
        why = (p.stderr or p.stdout or "").strip().splitlines()
        raise ContactError(f"{cmd[0]} exited {p.returncode}: {why[-1] if why else 'no output'}")
    return p.stdout


_UNAVAILABLE = ("not signed in", "re-run m365-auth", "not available on this tenant")


def _parse_m365_contacts(text: str) -> list[dict]:
    """Parse `m365 contacts` output: `Name<2+ spaces>emails<2+ spaces>company`.

    `m365 contacts` has no `--json` (unlike its mail/cal commands), so this is
    text parsing by necessity. It is defensive about field order rather than
    positional: any field containing '@' is email, the first line field is the
    name, whatever is left is the company. An empty middle field collapses the
    separator, which a positional split would silently misread as the company
    being an address.
    """
    people = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "usage:")):
            continue
        low = line.lower()
        if any(s in low for s in _UNAVAILABLE):
            raise ContactError(line)
        parts = [p for p in re.split(r"\s{2,}", line) if p.strip()]
        if not parts or not re.search(r"[A-Za-z]", parts[0]):
            continue
        name, emails, company = parts[0].strip(), [], ""
        for p in parts[1:]:
            if "@" in p:
                emails += [e.strip() for e in p.split(",") if "@" in e]
            elif not company:
                company = p.strip()
        people.append({"name": name, "emails": emails, "company": company})
    return people


def _m365(query: str, S: dict, *, people: bool) -> list[dict]:
    """`m365 contacts "<q>"` hits Graph /me/people; bare it lists the address book.

    Both configured accounts are consulted, because a colleague may exist in one
    tenant and not the other, and the caller has no way to know which.
    """
    out, errors = [], []
    for account in _csv(S["m365_accounts"]) or ["default"]:
        cmd = ["m365"]
        if account and account != "default":
            cmd += ["--account", account]
        cmd += ["contacts"]
        if people:
            cmd += [query]
        cmd += ["-n", str(S["m365_limit"])]
        try:
            rows = _parse_m365_contacts(_run(cmd, int(S["timeout"])))
        except ContactError as e:
            errors.append(f"{account}: {e}")
            continue
        for i, r in enumerate(rows):
            out.append({**r, "ranked": people, "rank": i if people else None,
                        "account": account})
    if not out and errors:
        raise ContactError("; ".join(errors))
    return out


def m365_people(query: str, S: dict) -> list[dict]:
    return _m365(query, S, people=True)


def m365_contacts(query: str, S: dict) -> list[dict]:
    return _m365(query, S, people=False)


def git_log(query: str, S: dict) -> list[dict]:
    """Authors from configured repos, ranked by commit count, newest date kept.

    Off unless `ATTICUS_CONTACTS_GIT_REPOS` names repos. It only ever yields
    email handles, so it can identify a colleague but never reach them on Signal.
    """
    repos = [p for p in re.split(r"[,:;]", str(S["git_repos"])) if p.strip()]
    if not repos:
        raise ContactError("no repos configured (ATTICUS_CONTACTS_GIT_REPOS)")
    seen: dict[str, dict] = {}
    errors = []
    for repo in repos:
        cmd = ["git", "-C", str(Path(repo).expanduser()), "log",
               f"-n{int(S['git_max_commits'])}", "--format=%aN\t%aE\t%aI"]
        try:
            text = _run(cmd, int(S["timeout"]))
        except ContactError as e:
            errors.append(str(e))
            continue
        for line in text.splitlines():
            bits = line.split("\t")
            if len(bits) < 3 or not bits[0].strip():
                continue
            name, email, when = bits[0].strip(), bits[1].strip(), bits[2].strip()
            key = email.lower() or name.lower()
            rec = seen.setdefault(key, {"name": name, "emails": [email] if email else [],
                                        "company": "", "commits": 0,
                                        "last_interaction": None})
            rec["commits"] += 1
            if not rec["last_interaction"] or when > rec["last_interaction"]:
                rec["last_interaction"] = when[:10]
    if not seen and errors:
        raise ContactError("; ".join(errors))
    rows = sorted(seen.values(), key=lambda r: -r["commits"])
    for i, r in enumerate(rows):
        r["ranked"], r["rank"] = True, i
    return rows


register_source("m365:people", m365_people)
register_source("m365:contacts", m365_contacts)
register_source("git:log", git_log)


# ----------------------------------------------------------------- scoring ---
def _tier(query: str, person: dict) -> tuple[str, str, float] | None:
    """(tier, matched_on, similarity) for the best way `query` hits `person`.

    Similarity is only ever used to position a candidate *inside* its band.
    """
    q = normalize(query)
    if not q:
        return None
    full = normalize(person.get("name") or "")
    tokens = [t for t in full.split() if t]
    locals_ = [normalize((e or "").split("@")[0].replace(".", " "))
               for e in person.get("emails") or []]

    # --- exact: what was said IS the name, or one whole token of it ----------
    if q == full:
        return EXACT, full, 1.0
    if q in tokens:
        return EXACT, q, 0.9
    for lp in locals_:
        if lp and (q == lp or q in lp.split()):
            return EXACT, lp, 0.8
    # A multi-word query matching every token in order, e.g. "robbie page"
    qt = q.split()
    if len(qt) > 1 and all(t in tokens for t in qt):
        return EXACT, " ".join(qt), 0.95

    # --- partial: prefix or near-spelling -----------------------------------
    best = (0.0, "")
    for t in tokens:
        if len(q) >= 3 and (t.startswith(q) or q.startswith(t)):
            score = len(min(q, t, key=len)) / max(len(q), len(t))
            if score > best[0]:
                best = (max(score, 0.6), t)
        r = _ratio(q, t)
        if r >= 0.82 and r > best[0]:
            best = (r, t)
    if best[0]:
        return PARTIAL, best[1], best[0]

    return None


def _phonetic_tier(query: str, person: dict) -> tuple[str, str, float] | None:
    qk = metaphone(query)
    if len(qk) < 2:
        return None
    best = (0.0, "")
    for t in normalize(person.get("name") or "").split():
        tk = metaphone(t)
        if not tk:
            continue
        if tk == qk:
            r = max(0.9, _ratio(query.lower(), t))
        else:
            r = _ratio(qk, tk)
            if r < 0.9:
                continue
            r *= 0.8
        if r > best[0]:
            best = (r, t)
    return (PHONETIC, best[1], best[0]) if best[0] else None


def _confidence(tier: str, similarity: float, src_weight: float,
                rank: int | None, limit: int) -> float:
    base, span = _BAND[tier]
    if rank is None:
        rank_score = 0.5                        # unranked source: neither reward nor punish
    else:
        rank_score = max(0.0, 1.0 - rank / float(max(limit, 1)))
    q = 0.45 * min(1.0, similarity) + 0.35 * src_weight + 0.20 * rank_score
    return round(base + span * min(1.0, max(0.0, q)), 3)


def _person_name(person: dict) -> str:
    """The display name, repaired when the source had none.

    Graph really does return `displayName` == the address for directory entries
    with no display name, which arrived as a third "candidate" for the same human
    alongside their two real rows — an ambiguity invented by the source, not by
    reality. Deriving a name from the localpart makes it dedupe against the row it
    is a duplicate of.
    """
    name = str(person.get("name") or "").strip()
    if name and " " not in name and "@" in name:
        local = name.split("@")[0]
        pretty = " ".join(w.capitalize() for w in re.split(r"[._\-+]+", local) if w)
        return pretty or name
    return name


def _handles(person: dict) -> dict:
    h = {}
    emails = [e for e in (person.get("emails") or []) if e]
    if emails:
        h["email"] = emails[0]
    phones = [p for p in (person.get("phones") or []) if p]
    if phones:
        h["phone"] = phones[0]
    if person.get("slack"):
        h["slack"] = str(person["slack"])
    return h


# --------------------------------------------------------------- resolution ---
def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cache_path(cfg) -> Path:
    p = getattr(cfg, "contacts_cache_path", None) if cfg is not None else None
    return Path(p).expanduser() if p else CACHE


def _load_cache(cfg) -> dict:
    try:
        d = json.loads(_cache_path(cfg).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cfg, d: dict) -> None:
    p = _cache_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=2, sort_keys=True))
    except OSError:
        pass                                # a cache that cannot be written is not an error


def _fresh(entry, ttl_hours: float) -> bool:
    if not isinstance(entry, dict) or "at" not in entry:
        return False
    if ttl_hours <= 0:
        return False                        # 0 disables the cache for reads too
    try:
        at = datetime.fromisoformat(str(entry["at"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(UTC) - at < timedelta(hours=ttl_hours)


def resolve_detail(name: str, channel: str | None = None, cfg=None, *, log=None,
                   use_cache: bool = True) -> dict:
    """`resolve()` plus the diagnostics: per-source status, cache state, winner.

    This is what the CLI prints and what the cache stores. Handlers want
    `resolve()`; anyone asking "why did it pick him?" wants this.
    """
    say = log or (lambda *_a, **_k: None)
    S = _settings(cfg)
    query = (name or "").strip()
    chan = (channel or "").strip().lower() or None
    key = f"{normalize(query)}|{chan or 'any'}"
    detail = {"query": query, "channel": chan, "at": _now(), "cached": False,
              "sources": {}, "matches": [], "winner": None}
    if not query:
        detail["sources"]["*"] = "empty query"
        return detail

    cache = _load_cache(cfg)
    if use_cache and _fresh(cache.get(key), float(S["cache_ttl_hours"])):
        hit = dict(cache[key])
        hit["cached"] = True
        say(f"    contacts: '{query}' from cache ({len(hit.get('matches') or [])} candidate(s))")
        return hit

    order = _csv(S["sources"])
    raw: list[tuple[str, dict, float]] = []
    for i, src in enumerate(order):
        fn = _SOURCES.get(src)
        if fn is None:
            detail["sources"][src] = "unknown source"
            continue
        weight = 1.0 - (i / float(max(len(order), 1)))       # earlier source, better
        try:
            rows = fn(query, S) or []
        except ContactError as e:
            detail["sources"][src] = f"unavailable: {e}"
            say(f"    contacts: {src} unavailable: {e}")
            continue
        except Exception as e:                              # noqa: BLE001 - a source bug
            detail["sources"][src] = f"error: {type(e).__name__}: {e}"
            say(f"    contacts: {src} errored: {type(e).__name__}: {e}")
            continue
        detail["sources"][src] = f"ok: {len(rows)} row(s)"
        for r in rows:
            raw.append((src, r, weight))

    want = CHANNEL_HANDLE.get(chan) if chan else None
    merged: dict[str, Match] = {}
    for src, person, weight in raw:
        person = {**person, "name": _person_name(person)}
        hit = _tier(query, person)
        if hit is None and S["phonetic"]:
            hit = _phonetic_tier(query, person)
        if hit is None:
            continue
        tier, matched_on, sim = hit
        handles = _handles(person)
        conf = _confidence(tier, sim, weight, person.get("rank"), int(S["m365_limit"]))
        m = Match(name=person.get("name") or matched_on,
                  handle=handles.get(want, "") if want else "",
                  channel=chan or "", source=src, confidence=conf, tier=tier,
                  matched_on=matched_on, handles=handles,
                  rank=person.get("rank"),
                  last_interaction=person.get("last_interaction"),
                  company=person.get("company") or "")
        if chan and want and not m.handle:
            m.note = f"no {want} handle known for {chan}"
        # Identity = name + handles. Two people who share a first name stay two
        # candidates; one person seen by two sources becomes one, keeping the
        # better score and remembering both provenances.
        ident = f"{normalize(m.name)}|{sorted(handles.items())}"
        prev = merged.get(ident)
        if prev is None:
            merged[ident] = m
        elif (m.confidence, -_TIER_ORDER[m.tier]) > (prev.confidence, -_TIER_ORDER[prev.tier]):
            m.also_seen = tuple(sorted({*prev.also_seen, prev.source}))
            merged[ident] = m
        else:
            prev.also_seen = tuple(sorted({*prev.also_seen, m.source}))

    ranked = sorted(merged.values(),
                    key=lambda m: (-m.confidence, _TIER_ORDER[m.tier], m.name.lower()))
    ranked = ranked[:int(S["max_results"])]
    detail["matches"] = [m.to_dict() for m in ranked]
    if ranked:
        top = ranked[0]
        # The winning source is recorded separately and deliberately: when a
        # message reaches the wrong person, the first question is which source
        # said so, and reconstructing it from a ranked list after the fact is
        # guesswork.
        detail["winner"] = {"name": top.name, "handle": top.handle,
                            "source": top.source, "tier": top.tier,
                            "confidence": top.confidence,
                            "also_seen": list(top.also_seen)}
    say(f"    contacts: '{query}' → {len(ranked)} candidate(s)"
        + (f", top {ranked[0].describe()}" if ranked else ""))

    if float(S["cache_ttl_hours"]) > 0:
        cache[key] = detail
        _save_cache(cfg, cache)
    return detail


def resolve(name: str, channel: str | None = None, cfg=None, *, log=None,
            use_cache: bool = True) -> list[Match]:
    """Candidates for a spoken name, best first. Never a bare handle.

    An empty list means "nobody found", which is a normal answer and not an
    error — sources are unavailable more often than they are broken, and a
    resolver that raised would take a whole outbox pass down with it.
    """
    d = resolve_detail(name, channel, cfg, log=log, use_cache=use_cache)
    return [_match_from_dict(x) for x in d.get("matches") or []]


def _match_from_dict(d: dict) -> Match:
    fields = {f for f in Match.__dataclass_fields__}
    m = Match(**{k: v for k, v in d.items() if k in fields})
    m.also_seen = tuple(m.also_seen or ())
    return m


def unambiguous(matches, cfg=None) -> tuple[Match | None, str]:
    """The one match a caller may act on, or None and the reason it refused.

    This is the decision `signal.send` needs, and its bias is explicit: it
    returns a match only when

      * the top candidate is at or above `min_confidence` (default 0.75, which is
        the floor of the exact band — so a partial or phonetic hit alone is never
        enough to deliver a message), and
      * it is clear of the runner-up by `ambiguity_margin`, and
      * it actually carries a handle for the requested channel.

    Two Robbies therefore refuse, and refusing is the correct outcome: nobody is
    present to disambiguate, and the failure it avoids is a private message to
    the wrong person.
    """
    S = _settings(cfg)
    ms = [_match_from_dict(m) if isinstance(m, dict) else m for m in (matches or [])]
    if not ms:
        return None, "no candidates"
    top = ms[0]
    if top.confidence < float(S["min_confidence"]):
        return None, (f"best candidate {top.name} is a {top.tier} match at "
                      f"{top.confidence:.2f}, below the {float(S['min_confidence']):.2f} floor")
    rivals = [m for m in ms[1:]
              if top.confidence - m.confidence < float(S["ambiguity_margin"])]
    if rivals:
        tied = [top, *rivals]
        margin = float(S["ambiguity_margin"])
        if top.channel and not any(m.handle for m in tied):
            # Nobody in the tie is reachable on this channel, so which of them was
            # meant is not yet the interesting question — the missing source is.
            return None, f"no {top.channel} handle known for any of {len(tied)} candidates"
        if len({normalize(m.name) for m in tied}) == 1:
            # One human, several handles — a real and common case (the same person
            # in two tenants). Still a refusal, because "which address" is a real
            # question, but a very different one from "which person", and an
            # operator reading a receipt needs to be told which they are looking at.
            where = ", ".join((m.handle or f"[{m.source}]") for m in tied[:4])
            return None, (f"{top.name} matched {len(tied)} handles — same name, "
                          f"different addresses: {where}")
        names = ", ".join(m.name for m in tied[:4])
        return None, f"{len(tied)} candidates within {margin:.2f}: {names}"
    if top.channel and not top.handle:
        return None, f"{top.name} has no handle for {top.channel}"
    return top, top.describe()


# --------------------------------------------------------------------- CLI ---
def main(argv=None) -> int:
    """`python -m contacts resolve "Robbie" [--channel signal] [--json]`."""
    import argparse
    p = argparse.ArgumentParser(prog="contacts", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve", help="resolve a spoken name to candidates")
    r.add_argument("name")
    r.add_argument("--channel", default=None)
    r.add_argument("--json", action="store_true")
    r.add_argument("--no-cache", action="store_true")
    a = p.parse_args(argv)

    try:
        from config import Config
        cfg = Config()
    except Exception:                                       # noqa: BLE001 - CLI must run bare
        cfg = None
    d = resolve_detail(a.name, a.channel, cfg, use_cache=not a.no_cache)
    if a.json:
        print(json.dumps(d, indent=2))
        return 0
    for src, status in d["sources"].items():
        print(f"{src:16} {status}")
    if not d["matches"]:
        print(f"no candidates for {a.name!r}")
        return 1
    for m in d["matches"]:
        print(f"  {m['confidence']:.2f}  {m['tier']:8} {m['name']}"
              f"  {m['handle'] or '(no handle)'}  [{m['source']}]"
              + (f"  {m['note']}" if m.get("note") else ""))
    chosen, why = unambiguous(d["matches"], cfg)
    print(("→ " if chosen else "→ refused: ") + why)
    return 0 if chosen else 2


if __name__ == "__main__":
    raise SystemExit(main())
