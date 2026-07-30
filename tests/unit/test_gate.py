"""The wake-phrase gate. This is the only thing standing between ambient speech
and an autonomous agent, so it gets the most tests.

These tests describe the gate AS SHIPPED: exact match on a word boundary, with
`wake_aliases` empty by default. Recovery of a misheard wake word is the
adjudicator's job and is tested in test_wake.py — asserting it here (by
pre-loading aliases the shipped config does not carry) documented a wider gate
than the one that actually runs.
"""
import pytest
import transcribe as stt


@pytest.mark.parametrize("text", [
    "Atticus, research the top five AI security vulnerabilities please",
    "Okay, Atticus, do the thing now",
    "Um, Atticus, write this up for me",
    "atticus write this up",                         # case-insensitive
])
def test_gate_opens(cfg, text):
    ok, why = stt.sanity_check(text, cfg)
    assert ok, why


@pytest.mark.parametrize("text", [
    "So Atticus is a thing I built yesterday",      # description, not address
    "Well Atticus is what I call it",
    "The abacus is an old counting tool",
    "This is a test of the recording device",
    "Test, test",                                    # under min_words
])
def test_gate_holds(cfg, text):
    ok, _ = stt.sanity_check(text, cfg)
    assert not ok


@pytest.mark.parametrize("text", [
    "Advocates research the best Android phone options",   # observed mishearing
    "Abacus, can you do a stock analysis on Meta today",   # observed mishearing
    "Artemis, can you research job options please",        # observed mishearing
])
def test_observed_mishearings_do_not_pass_the_strict_gate(cfg, text):
    """The strict gate must REJECT these, because that is what ships.

    All three were really transcribed from someone saying "Atticus", and all
    three are recovered by the adjudicator — but recovery is a separate,
    fail-closed, network-dependent step. If this test ever goes green because
    sanity_check started admitting them, the gate widened without anyone saying
    so.
    """
    ok, why = stt.sanity_check(text, cfg)
    assert not ok
    assert "no wake phrase" in why, why


@pytest.mark.parametrize("text", [
    "Advocates research the best Android phone options",
    "Abacus, can you do a stock analysis on Meta today",
])
def test_aliases_open_the_gate_when_deliberately_configured(cfg, text):
    """`wake_aliases` is the deterministic escape hatch — off by default."""
    cfg.wake_aliases = ["advocates", "abacus"]
    assert stt.sanity_check(text, cfg)[0]


@pytest.mark.parametrize("text", [
    "Atticusville is where I grew up honestly",
    "Atticus's report was the one I meant",
])
def test_wake_phrase_must_land_on_a_word_boundary(cfg, text):
    """startswith() admitted any word merely BEGINNING with the phrase.

    Low impact for a long distinctive phrase, a real false accept for a short
    alias — and this is the control the credential posture leans on.
    """
    assert not stt.sanity_check(text, cfg)[0]


def test_no_wake_phrase_configured_executes_everything(cfg):
    cfg.wake_phrase = ""
    assert stt.sanity_check("just do the thing", cfg)[0]


def test_leading_words_strips_filler_for_the_adjudicator_path(cfg):
    """The adjudicator used to be handed "Okay" as the misheard wake word.

    sanity_check strips filler before matching; the adjudicator path did not, so
    "Okay, Artemis, research…" asked whether "Okay" was a mishearing of
    "atticus" while passing "artemis research…" as context — a nonsense question
    with a near-certain hold, on exactly the case it exists to recover.
    """
    words = stt.leading_words("Okay, Artemis, research the best options", 13)
    assert words[0] == "artemis"
    assert "research" in words
