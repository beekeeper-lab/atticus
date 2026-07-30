"""The wake-phrase gate. This is the only thing standing between ambient speech
and an autonomous agent, so it gets the most tests."""
import pytest
import transcribe as stt


@pytest.mark.parametrize("text", [
    "Atticus, research the top five AI security vulnerabilities please",
    "Okay, Atticus, do the thing now",
    "Um, Atticus, write this up for me",
    "Advocates research the best Android phone options",   # observed mishearing
    "Abacus, can you do a stock analysis on Meta today",    # observed mishearing
    "Artemis, can you research job options please",         # observed mishearing
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


def test_no_wake_phrase_configured_executes_everything(cfg):
    cfg.wake_phrase = ""
    assert stt.sanity_check("just do the thing", cfg)[0]
