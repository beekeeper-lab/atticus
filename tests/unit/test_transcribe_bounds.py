"""M19, L1, L3 — bounding what reaches the transcription API and the agent.

M19: bounded_audio used to FAIL OPEN when the duration was unknown, yielding the
whole file untouched — so a 40-minute ambient recording with no probe-able
duration was transcribed in full. It must cut blindly to the limit instead.

L1: the char-cap in extract_command mishandled rfind()'s -1 sentinel.
L3: strip_wake_phrase ignored aliases, leaving a misheard name in the prompt.
"""
import types

import pytest

import transcribe as stt


def _cfg(limit=180):
    return types.SimpleNamespace(max_command_seconds=limit)


def _fake_slicer(monkeypatch):
    calls = {}

    def fake_slice(src, dest, start, dur):
        calls["args"] = (start, dur)
        dest.write_bytes(b"cut")

    monkeypatch.setattr(stt, "slice_audio", fake_slice)
    monkeypatch.setattr(stt.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    return calls


# ---- M19: bounded_audio --------------------------------------------------

def test_duration_equal_to_limit_passes_through(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    with stt.bounded_audio(audio, _cfg(180), 180, log=lambda m: None) as (up, meta):
        assert up == audio
        assert meta == {}


def test_duration_over_limit_is_cut(tmp_path, monkeypatch):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    calls = _fake_slicer(monkeypatch)
    with stt.bounded_audio(audio, _cfg(180), 181, log=lambda m: None) as (up, meta):
        assert up != audio and up.read_bytes() == b"cut"
        assert meta["transcribed_seconds"] == 180
        assert meta["truncated_from_seconds"] == 181
    assert calls["args"] == (0, 180)


def test_unknown_duration_cuts_blindly_rather_than_passing_through(tmp_path, monkeypatch):
    """The regression M19 fixes: probe returns None, so the old code yielded the
    whole file. It must now cut to the limit and NOT claim a from-length it does
    not know."""
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    monkeypatch.setattr(stt, "_probe_seconds", lambda a: None)
    calls = _fake_slicer(monkeypatch)
    with stt.bounded_audio(audio, _cfg(180), None, log=lambda m: None) as (up, meta):
        assert up != audio and up.read_bytes() == b"cut"
        assert meta["transcribed_seconds"] == 180
        assert "truncated_from_seconds" not in meta
    assert calls["args"] == (0, 180)


def test_unknown_duration_without_ffmpeg_fails_closed(tmp_path, monkeypatch):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    monkeypatch.setattr(stt, "_probe_seconds", lambda a: None)
    monkeypatch.setattr(stt.shutil, "which", lambda name: None)
    with pytest.raises(stt.TranscriptionError):
        with stt.bounded_audio(audio, _cfg(180), None, log=lambda m: None):
            pass


def test_no_limit_configured_passes_through(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    with stt.bounded_audio(audio, _cfg(0), None, log=lambda m: None) as (up, meta):
        assert up == audio and meta == {}


# ---- L1: char cap with no spaces -----------------------------------------

def test_char_cap_bounds_even_with_no_spaces():
    cfg = types.SimpleNamespace(wake_phrase="", wake_aliases=[],
                                max_command_chars=50, max_command_sentences=0)
    text = "x" * 100                       # no spaces, no sentence ends
    cmd, clip = stt.extract_command(text, cfg)
    assert len(cmd) <= 50, "rfind()'s -1 must not leak through as a slice index"
    assert clip.get("command_clipped")


# ---- L3: strip_wake_phrase honours aliases -------------------------------

def test_strip_wake_phrase_strips_a_matched_alias():
    cfg = types.SimpleNamespace(wake_phrase="atticus",
                                wake_aliases=["advocates"])
    assert stt.strip_wake_phrase("Advocates research cats", cfg) == "research cats"


def test_strip_wake_phrase_still_strips_the_real_phrase():
    cfg = types.SimpleNamespace(wake_phrase="atticus", wake_aliases=["advocates"])
    assert stt.strip_wake_phrase("Atticus do the thing", cfg) == "do the thing"


def test_strip_wake_phrase_takes_the_earliest_trigger():
    cfg = types.SimpleNamespace(wake_phrase="atticus", wake_aliases=["advocates"])
    # "atticus" appears before the alias, so it wins.
    assert stt.strip_wake_phrase("Atticus and advocates", cfg) == "and advocates"
