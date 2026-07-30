"""The adjudicator can only WIDEN the gate protecting an autonomous agent, so
its failure modes matter more than its successes. Every test here is about
refusing, except the two that prove it recovers.

No live API calls: the transport is stubbed so these are deterministic and free.
"""
import types
import pytest
import wake


@pytest.fixture
def wcfg(cfg):
    cfg.wake_adjudicator = True
    cfg.wake_adjudicator_model = "gpt-4o-mini"
    cfg.wake_adjudicator_threshold = 50
    cfg.wake_adjudicator_timeout = 5
    cfg.openai_key = "sk-test"
    return cfg


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(wake, "CACHE", tmp_path / "verdicts.json")


def _reply(monkeypatch, content, status=200):
    def fake_post(*a, **k):
        return types.SimpleNamespace(
            status_code=status,
            json=lambda: {"choices": [{"message": {"content": content}}]})
    import requests
    monkeypatch.setattr(requests, "post", fake_post)


# ---- recovery ------------------------------------------------------------

def test_high_score_admits(wcfg, monkeypatch):
    _reply(monkeypatch, "85")
    ok, why = wake.adjudicate("Advocates", wcfg, following="research Android phones")
    assert ok and "85" in why


def test_low_score_holds(wcfg, monkeypatch):
    _reply(monkeypatch, "10")
    ok, _ = wake.adjudicate("Marcus", wcfg, following="pass me the milk")
    assert not ok


def test_threshold_is_a_boundary_not_a_suggestion(wcfg, monkeypatch):
    _reply(monkeypatch, "50")
    assert wake.adjudicate("Atticas", wcfg)[0] is True
    wake.CACHE.unlink(missing_ok=True)
    _reply(monkeypatch, "49")
    assert wake.adjudicate("Atticos", wcfg)[0] is False


# ---- must fail closed ----------------------------------------------------

def test_disabled_never_admits(wcfg, monkeypatch):
    _reply(monkeypatch, "100")
    wcfg.wake_adjudicator = False
    assert wake.adjudicate("Advocates", wcfg)[0] is False


def test_network_failure_fails_closed(wcfg, monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "post", boom)
    ok, why = wake.adjudicate("Advocates", wcfg)
    assert not ok and "failing closed" in why


def test_http_error_fails_closed(wcfg, monkeypatch):
    _reply(monkeypatch, "100", status=500)
    assert wake.adjudicate("Advocates", wcfg)[0] is False


@pytest.mark.parametrize("reply", ["YES", "definitely", "", "yes 90", "101", "-5"])
def test_non_numeric_or_out_of_range_fails_closed(wcfg, monkeypatch, reply):
    """Prose, hedging, or a score outside 0-100 must never open the gate."""
    _reply(monkeypatch, reply)
    assert wake.adjudicate("Advocates", wcfg)[0] is False


@pytest.mark.parametrize("tok", [
    "", "the", "a",                      # too short / not a name
    "supercalifragilisticexpialidocious",  # too long
    "12345", "!!!", "run-the-thing now",   # not name-shaped
])
def test_non_name_first_words_are_refused_without_a_call(wcfg, monkeypatch, tok):
    """A transcript that opens with an imperative and no name is either a
    dropped wake word or ambient speech — indistinguishable. Refuse."""
    def explode(*a, **k):
        raise AssertionError("must not call the API for a non-name token")
    import requests
    monkeypatch.setattr(requests, "post", explode)
    assert wake.adjudicate(tok, wcfg)[0] is False


def test_no_wake_phrase_configured_never_admits(wcfg, monkeypatch):
    _reply(monkeypatch, "100")
    wcfg.wake_phrase = ""
    assert wake.adjudicate("anything", wcfg)[0] is False


# ---- caching -------------------------------------------------------------

def test_verdict_is_cached_so_a_recurring_mishearing_costs_one_call(wcfg, monkeypatch):
    _reply(monkeypatch, "85")
    assert wake.adjudicate("Advocates", wcfg, following="research phones")[0]

    def explode(*a, **k):
        raise AssertionError("should have been served from cache")
    import requests
    monkeypatch.setattr(requests, "post", explode)
    ok, why = wake.adjudicate("Advocates", wcfg, following="research phones")
    assert ok and "cached" in why


def test_context_is_part_of_the_cache_key(wcfg, monkeypatch):
    """'Marcus, pass the milk' must not poison 'Marcus, research X'."""
    _reply(monkeypatch, "10")
    assert not wake.adjudicate("Marcus", wcfg, following="pass me the milk")[0]
    _reply(monkeypatch, "85")
    assert wake.adjudicate("Marcus", wcfg, following="research Android phones")[0]


def test_first_token_strips_punctuation():
    assert wake.first_token("Atticus, do the thing") == "Atticus"
    assert wake.first_token("  Advocates research this") == "Advocates"
    assert wake.first_token("") == ""
