"""Contact resolution (#43).

No network and no real subprocess: `contacts._run` is the single choke point for
both, and every test here replaces it. The cache is redirected at a tmp path by
an autouse fixture, so a test can never read or poison the operator's real
`~/.cache/atticus/contacts.json`.
"""
import json
from pathlib import Path

import pytest

import contacts

# What `m365 contacts "<query>"` actually prints — Graph /me/people, relevance
# ordered, `Name<2+ spaces>emails<2+ spaces>company`. Copied in this shape
# deliberately: the command has no --json, so the parser is load-bearing and a
# tidied-up fixture would test a format the CLI never emits.
PEOPLE = (
    "Robbie Page  robbie.page@acme.example  Acme Ltd\n"
    "Robby Pace  rpace@other.example  Other Inc\n"
)
BOOK = (
    "Robbie Page  robbie.page@acme.example  Acme Ltd\n"
    "Kathryn Vance  kv@acme.example  Acme Ltd\n"
    "Dave Nomail    Acme Ltd\n"
)


@pytest.fixture(autouse=True)
def _no_real_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts, "CACHE", tmp_path / "contacts-cache.json")


@pytest.fixture
def ccfg(cfg, tmp_path):
    """The real Config, with the resolver pointed at a scratch cache + one account."""
    cfg.contacts_cache_path = str(tmp_path / "contacts.json")
    cfg.contacts_m365_accounts = "default"
    return cfg


def fake_run(people=PEOPLE, book=BOOK, calls=None):
    """Stand in for `contacts._run`, dispatching on the command it is handed.

    The dispatch strips `--account NAME` and `-n N` before deciding, rather than
    scanning for "a word that isn't a flag" — the naive version read the account
    name as the search query and handed back the people fixture for address-book
    calls, i.e. the fixture would have agreed with a resolver that ignored the
    `people` flag entirely.
    """
    def _run(cmd, timeout):
        if calls is not None:
            calls.append(list(cmd))
        assert cmd[0] == "m365", cmd
        rest, i = [], 1
        while i < len(cmd):
            if cmd[i] in ("--account", "-n"):
                i += 2
                continue
            if cmd[i] != "contacts":
                rest.append(cmd[i])
            i += 1
        # `m365 contacts <query>` = Graph /me/people; bare = the address book.
        return people if rest else book
    return _run


def resolve(name, ccfg, monkeypatch, channel=None, **kw):
    monkeypatch.setattr(contacts, "_run", fake_run(**kw))
    return contacts.resolve(name, channel, ccfg)


# ------------------------------------------------------------------ parsing ---
def test_parses_missing_email_without_mistaking_the_company_for_one():
    """`Name<4 spaces>Company` (no email) must not land the company in `emails`.

    A positional split would: the empty middle field collapses the separator.
    """
    rows = contacts._parse_m365_contacts(BOOK)
    by_name = {r["name"]: r for r in rows}
    assert by_name["Dave Nomail"]["emails"] == []
    assert by_name["Dave Nomail"]["company"] == "Acme Ltd"
    assert by_name["Robbie Page"]["emails"] == ["robbie.page@acme.example"]


def test_a_signed_out_account_is_unavailable_not_a_person():
    with pytest.raises(contacts.ContactError):
        contacts._parse_m365_contacts("not signed in — re-run m365-auth\n")


# ------------------------------------------------------------------ ranking ---
def test_exact_match_beats_phonetic(ccfg, monkeypatch):
    ms = resolve("Robbie", ccfg, monkeypatch)
    assert [m.name for m in ms][:2] == ["Robbie Page", "Robby Pace"]
    assert ms[0].tier == contacts.EXACT
    assert ms[1].tier == contacts.PHONETIC
    assert ms[0].confidence > ms[1].confidence


def test_phonetic_cannot_outrank_exact_even_from_the_better_source(ccfg, monkeypatch):
    """The band, not the source weighting, decides tier order.

    Here the phonetic candidate comes from the top-priority ranked source at
    position 0, and the exact one from the unranked address book — every quality
    signal favours the wrong person. Bands must still win.
    """
    ms = resolve("Robbie", ccfg, monkeypatch,
                 people="Robby Pace  rpace@other.example  Other Inc\n",
                 book="Robbie Page  robbie.page@acme.example  Acme Ltd\n")
    assert ms[0].name == "Robbie Page"
    assert ms[0].tier == contacts.EXACT
    assert ms[0].confidence >= contacts._BAND[contacts.EXACT][0]
    assert ms[1].confidence <= sum(contacts._BAND[contacts.PHONETIC])


def test_bands_are_disjoint():
    """Structural: no tier's ceiling may reach the next tier's floor."""
    for lo, hi in ((contacts.PHONETIC, contacts.PARTIAL), (contacts.PARTIAL, contacts.EXACT)):
        assert sum(contacts._BAND[lo]) < contacts._BAND[hi][0]


# ---------------------------------------------------------------- ambiguity ---
def test_two_robbies_return_two_candidates_and_refuse(ccfg, monkeypatch):
    """The normal case. Two candidates must not collapse into one confident answer."""
    two = ("Robbie Page  robbie.page@acme.example  Acme Ltd\n"
           "Robbie Chen  rchen@acme.example  Acme Ltd\n")
    ms = resolve("Robbie", ccfg, monkeypatch, channel="email", people=two, book="")
    assert len(ms) == 2
    assert {m.name for m in ms} == {"Robbie Page", "Robbie Chen"}
    assert all(m.tier == contacts.EXACT for m in ms)

    chosen, why = contacts.unambiguous(ms, ccfg)
    assert chosen is None
    assert "2 candidates" in why
    assert "Robbie Page" in why and "Robbie Chen" in why


def test_same_person_two_mailboxes_refuses_but_says_so_differently(ccfg, monkeypatch):
    """Found against live data: one human in two tenants.

    Still a refusal — "which address" is a real question — but an operator reading
    a receipt must be able to tell it from "which person", because the fixes are
    different.
    """
    both = ("Gregg Reed  greed@organservices.example  Organ Services\n"
            "Gregg Reed  gregg.reed@stonewaters.example  Stonewaters\n")
    ms = resolve("Gregg", ccfg, monkeypatch, channel="email", people=both, book="")
    assert len(ms) == 2
    chosen, why = contacts.unambiguous(ms, ccfg)
    assert chosen is None
    assert "same name, different addresses" in why
    assert "greed@organservices.example" in why


def test_a_tie_nobody_is_reachable_on_reports_the_missing_handle(ccfg, monkeypatch):
    both = ("Gregg Reed  greed@organservices.example  Organ Services\n"
            "Gregg Reed  gregg.reed@stonewaters.example  Stonewaters\n")
    ms = resolve("Gregg", ccfg, monkeypatch, channel="signal", people=both, book="")
    chosen, why = contacts.unambiguous(ms, ccfg)
    assert chosen is None
    assert why == "no signal handle known for any of 2 candidates"


def test_a_display_name_that_is_an_email_address_is_repaired_not_counted_twice(ccfg, monkeypatch):
    """Found against live data: Graph returns displayName == the address.

    That arrived as a third candidate for the same human — an ambiguity invented
    by the source. It must dedupe against the row it duplicates.
    """
    rows = ("Gregg Reed  gregg.reed@stonewaters.example  Stonewaters\n"
            "gregg.reed@stonewaters.example  gregg.reed@stonewaters.example  Stonewaters\n")
    ms = resolve("Gregg", ccfg, monkeypatch, channel="email", people=rows, book="")
    assert [m.name for m in ms] == ["Gregg Reed"]
    chosen, why = contacts.unambiguous(ms, ccfg)
    assert chosen is not None and chosen.handle == "gregg.reed@stonewaters.example"


def test_one_confident_match_is_distinguishable_from_several(ccfg, monkeypatch):
    ms = resolve("Kathryn", ccfg, monkeypatch, channel="email", people="", book=BOOK)
    chosen, why = contacts.unambiguous(ms, ccfg)
    assert chosen is not None
    assert chosen.name == "Kathryn Vance"
    assert chosen.handle == "kv@acme.example"
    assert "kv@acme.example" in why


def test_empty_result_is_not_an_error(ccfg, monkeypatch):
    ms = resolve("Zebediah", ccfg, monkeypatch, people="", book=BOOK)
    assert ms == []
    chosen, why = contacts.unambiguous(ms, ccfg)
    assert chosen is None
    assert why == "no candidates"


def test_an_unavailable_source_degrades_instead_of_raising(ccfg, monkeypatch):
    def boom(cmd, timeout):
        raise contacts.ContactError("m365 is not on PATH")
    monkeypatch.setattr(contacts, "_run", boom)
    d = contacts.resolve_detail("Robbie", "email", ccfg)
    assert d["matches"] == []
    assert all("unavailable" in v for v in d["sources"].values())


def test_a_source_that_raises_something_unexpected_is_contained(ccfg, monkeypatch):
    def boom(cmd, timeout):
        raise ValueError("bug in a source")
    monkeypatch.setattr(contacts, "_run", boom)
    d = contacts.resolve_detail("Robbie", None, ccfg)
    assert d["matches"] == []
    assert any("ValueError" in v for v in d["sources"].values())


# ---------------------------------------------- transcription damage (#43) ---
def test_a_mangled_name_still_finds_its_person_but_with_lower_confidence(ccfg, monkeypatch):
    """'Robby' for 'Robbie': found, phonetic, and NOT good enough to send to.

    This is the case the issue is really about — a transcript mangles a name and
    there is no wake-word adjudicator behind a person's name.
    """
    ms = resolve("Robby", ccfg, monkeypatch, channel="email",
                 people="", book="Robbie Page  robbie.page@acme.example  Acme Ltd\n")
    assert [m.name for m in ms] == ["Robbie Page"]
    assert ms[0].tier == contacts.PHONETIC
    assert ms[0].confidence < contacts._DEFAULTS["min_confidence"]

    chosen, why = contacts.unambiguous(ms, ccfg)
    assert chosen is None
    assert "phonetic" in why and "floor" in why


def test_a_differently_spelled_homophone_is_found(ccfg, monkeypatch):
    """'Catherine' heard for 'Kathryn' shares no prefix — only a sound."""
    ms = resolve("Catherine", ccfg, monkeypatch, people="", book=BOOK)
    assert [m.name for m in ms] == ["Kathryn Vance"]
    assert ms[0].tier == contacts.PHONETIC


def test_phonetic_matching_can_be_turned_off(ccfg, monkeypatch):
    ccfg.contacts_phonetic = "off"
    ms = resolve("Robby", ccfg, monkeypatch, people="", book=BOOK)
    assert ms == []


def test_metaphone_survives_word_boundaries():
    """Regression: `"" in "AEIOU"` is True, which broke final Y and initial H."""
    assert contacts.metaphone("Robby") == contacts.metaphone("Robbie")
    assert contacts.metaphone("Harry").startswith("H")
    assert contacts.metaphone("Paige") == contacts.metaphone("Page")
    assert contacts.metaphone("") == ""


# --------------------------------------------------------------- provenance ---
def test_provenance_is_recorded_on_every_match_and_in_the_cache(ccfg, monkeypatch):
    monkeypatch.setattr(contacts, "_run", fake_run())
    d = contacts.resolve_detail("Robbie", "email", ccfg)

    top = d["matches"][0]
    assert top["source"] == "m365:people"
    assert top["tier"] == "exact"
    assert top["matched_on"] == "robbie"
    assert top["rank"] == 0
    assert d["winner"] == {"name": "Robbie Page", "handle": "robbie.page@acme.example",
                           "source": "m365:people", "tier": "exact",
                           "confidence": top["confidence"], "also_seen": ["m365:contacts"]}
    assert d["sources"] == {"m365:people": "ok: 2 row(s)", "m365:contacts": "ok: 3 row(s)"}

    stored = json.loads(Path(ccfg.contacts_cache_path).read_text())
    entry = stored["robbie|email"]
    assert entry["winner"]["source"] == "m365:people"
    assert entry["query"] == "Robbie" and entry["at"]


def test_one_person_seen_by_two_sources_is_one_candidate_that_remembers_both(ccfg, monkeypatch):
    ms = resolve("Robbie", ccfg, monkeypatch, channel="email")
    page = [m for m in ms if m.name == "Robbie Page"]
    assert len(page) == 1
    assert page[0].source == "m365:people"          # the better-scoring provenance wins
    assert page[0].also_seen == ("m365:contacts",)  # and the other is still recorded


def test_a_cached_resolution_is_reused_without_touching_a_source(ccfg, monkeypatch):
    calls = []
    monkeypatch.setattr(contacts, "_run", fake_run(calls=calls))
    first = contacts.resolve_detail("Robbie", "email", ccfg)
    n = len(calls)
    assert n and first["cached"] is False

    second = contacts.resolve_detail("Robbie", "email", ccfg)
    assert second["cached"] is True
    assert len(calls) == n
    assert [m["name"] for m in second["matches"]] == [m["name"] for m in first["matches"]]
    assert second["winner"]["source"] == "m365:people"


def test_the_cache_can_be_bypassed_and_disabled(ccfg, monkeypatch):
    calls = []
    monkeypatch.setattr(contacts, "_run", fake_run(calls=calls))
    contacts.resolve_detail("Robbie", None, ccfg)
    contacts.resolve_detail("Robbie", None, ccfg, use_cache=False)
    assert len(calls) == 4                       # two sources, twice — no cache hit

    ccfg.contacts_cache_ttl_hours = 0
    contacts.resolve_detail("Robbie", None, ccfg)
    contacts.resolve_detail("Robbie", None, ccfg)
    assert len(calls) == 8


# ------------------------------------------------------------------ channel ---
def test_a_person_we_cannot_reach_on_the_channel_is_returned_unaddressable(ccfg, monkeypatch):
    """m365 gives no phone number, so Signal has no handle.

    That must not read as "no such person": "we know who Robbie is but cannot
    reach him on Signal" is a different diagnosis and a different fix.
    """
    ms = resolve("Robbie", ccfg, monkeypatch, channel="signal", book="")
    assert ms and ms[0].name == "Robbie Page"
    assert ms[0].handle == "" and ms[0].addressable is False
    assert "no phone handle" in ms[0].note

    chosen, why = contacts.unambiguous(ms, ccfg)
    assert chosen is None
    assert "no handle for signal" in why


def test_no_channel_asked_means_no_handle_chosen_but_all_are_offered(ccfg, monkeypatch):
    ms = resolve("Kathryn", ccfg, monkeypatch, people="", book=BOOK)
    assert ms[0].handle == "" and ms[0].channel == ""
    assert ms[0].handles == {"email": "kv@acme.example"}


def test_an_empty_name_resolves_to_nothing(ccfg, monkeypatch):
    monkeypatch.setattr(contacts, "_run", fake_run())
    assert contacts.resolve("   ", "email", ccfg) == []


# ------------------------------------------------------------------ sources ---
def test_git_history_is_a_source_but_off_until_repos_are_configured(ccfg, monkeypatch):
    ccfg.contacts_sources = "git:log"
    monkeypatch.setattr(contacts, "_run", fake_run())
    d = contacts.resolve_detail("Robbie", None, ccfg)
    assert d["matches"] == []
    assert "no repos configured" in d["sources"]["git:log"]


def test_git_history_ranks_by_commit_count_and_keeps_the_last_date(ccfg, monkeypatch, tmp_path):
    ccfg.contacts_sources = "git:log"
    ccfg.contacts_git_repos = str(tmp_path / "repo")
    log = ("Robbie Page\trobbie@acme.example\t2026-07-28T10:00:00+00:00\n"
           "Robbie Page\trobbie@acme.example\t2026-06-01T10:00:00+00:00\n"
           "Rob Otherguy\trob@acme.example\t2026-01-01T10:00:00+00:00\n")

    def _run(cmd, timeout):
        assert cmd[0] == "git" and "--format=%aN\t%aE\t%aI" in cmd
        return log
    monkeypatch.setattr(contacts, "_run", _run)

    ms = contacts.resolve("Robbie", "email", ccfg)
    assert ms[0].name == "Robbie Page"
    assert ms[0].source == "git:log"
    assert ms[0].handle == "robbie@acme.example"
    assert ms[0].last_interaction == "2026-07-28"
    assert ms[0].rank == 0


def test_adding_a_source_is_registering_one_function(ccfg, monkeypatch):
    monkeypatch.setitem(contacts._SOURCES, "test:src", lambda q, S: [
        {"name": "Robbie Page", "phones": ["+15550000000"], "ranked": True, "rank": 0,
         "last_interaction": "2026-07-30"}])
    ccfg.contacts_sources = "test:src"
    ms = contacts.resolve("Robbie", "signal", ccfg)
    assert len(ms) == 1
    assert ms[0].handle == "+15550000000"        # a source that knows phones reaches Signal
    assert ms[0].source == "test:src"
    chosen, why = contacts.unambiguous(ms, ccfg)
    assert chosen is not None and "+15550000000" in why


def test_an_unknown_source_name_is_reported_not_ignored(ccfg, monkeypatch):
    ccfg.contacts_sources = "m365:peeple"
    monkeypatch.setattr(contacts, "_run", fake_run())
    d = contacts.resolve_detail("Robbie", None, ccfg)
    assert d["sources"] == {"m365:peeple": "unknown source"}


def test_both_configured_accounts_are_consulted(cfg, tmp_path, monkeypatch):
    cfg.contacts_cache_path = str(tmp_path / "c.json")
    calls = []
    monkeypatch.setattr(contacts, "_run", fake_run(calls=calls))
    contacts.resolve("Robbie", "email", cfg)
    accounts = {c[c.index("--account") + 1] for c in calls if "--account" in c}
    assert accounts == {"organservices"}         # 'default' is passed by omission
    assert len(calls) == 4                       # 2 accounts x 2 m365 sources


# -------------------------------------------------------------- thresholds ---
def test_the_confidence_floor_is_the_bottom_of_the_exact_band(ccfg):
    """A partial or phonetic match can never be auto-chosen at the defaults.

    This is the property that keeps 'confidence high enough to draft' apart from
    'confidence high enough to deliver'.
    """
    assert contacts._DEFAULTS["min_confidence"] == contacts._BAND[contacts.EXACT][0]
    partial = contacts.Match(name="Rob Otherguy", handle="r@x", channel="email",
                             source="s", confidence=sum(contacts._BAND[contacts.PARTIAL]),
                             tier=contacts.PARTIAL)
    chosen, why = contacts.unambiguous([partial], ccfg)
    assert chosen is None and "below the 0.75 floor" in why


def test_settings_are_read_off_cfg_with_defaults_for_an_older_config(partial_cfg):
    S = contacts._settings(partial_cfg)
    assert S == contacts._DEFAULTS
    assert contacts._settings(None) == contacts._DEFAULTS
    partial_cfg.contacts_min_confidence = "0.9"
    partial_cfg.contacts_max_results = "3"
    partial_cfg.contacts_phonetic = "0"
    S = contacts._settings(partial_cfg)
    assert (S["min_confidence"], S["max_results"], S["phonetic"]) == (0.9, 3, False)


def test_max_results_bounds_the_list(ccfg, monkeypatch):
    ccfg.contacts_max_results = 2
    many = "".join(f"Robbie N{i}  r{i}@x.example  Acme\n" for i in range(6))
    ms = resolve("Robbie", ccfg, monkeypatch, people=many, book="")
    assert len(ms) == 2
