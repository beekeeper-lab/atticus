"""Notification plumbing — including the header bug that killed an alarm."""
import types
import notify as n


def test_ascii_folding_survives_typography():
    """An em-dash in a Title header raised UnicodeEncodeError and took the whole
    alarm down. An alarm must not be defeated by punctuation."""
    folded = n._ascii("Atticus ingest — session dead … “quoted”")
    folded.encode("latin-1")            # would raise before the fix
    assert "—" not in folded and "…" not in folded


def test_disabled_when_no_url():
    assert n.notify(types.SimpleNamespace(notify_url=None), "x") is False


def test_throttle_suppresses_repeats(tmp_path, monkeypatch):
    monkeypatch.setattr(n, "STATE", tmp_path)
    assert n.throttled("k", 6) is False
    (tmp_path).mkdir(exist_ok=True)
    n._stamp("k").touch()
    assert n.throttled("k", 6) is True
    n.clear("k")
    assert n.throttled("k", 6) is False
