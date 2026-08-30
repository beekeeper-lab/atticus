"""Dating the Plaud session, so it can be re-seeded before it dies.

Plaud's refresh token lasts ~30 days from the interactive login and is NOT
extended by use: the session seeded 2026-07-28 15:06 was exercised every 15
minutes for the whole window and expired 2026-08-27 15:17 regardless. Nothing
was watching that clock, so the first sign was ingest crash-looping — for three
days, ~290 passes, before anyone looked.

The token itself is no help here. It is not in the profile in any decodable
form (the one JWT there is Firebase's installation token), and reverse
engineering Plaud's storage layout would be exactly the fragile coupling
ADR-002 avoids. So we date the login ourselves and count forward.
"""
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def pw(tmp_path, monkeypatch):
    """plaud_web pointed at a throwaway profile, never the real one."""
    monkeypatch.setenv("PLAUD_SESSION_ROOT", str(tmp_path / "sessions"))
    mod = _load("plaud_web_age", "ingest/plaud_web.py")
    mod.SESSION_DIR.mkdir(parents=True)
    return mod


def _stamp(pw, when):
    pw.SEED_STAMP.write_text(when.isoformat())


# ---- the stamp is the measurement ----------------------------------------

def test_a_fresh_login_has_the_whole_window(pw):
    _stamp(pw, datetime.now(UTC))
    seeded, age, left, source = pw.session_age()
    assert source == "stamp"
    assert age < 0.01
    assert 29.9 < left <= 30.0


def test_the_window_counts_down(pw):
    _stamp(pw, datetime.now(UTC) - timedelta(days=26))
    _, age, left, _ = pw.session_age()
    assert 25.9 < age < 26.1
    assert 3.9 < left < 4.1


def test_an_expired_session_reports_negative_days_left(pw):
    """The real 2026-08-27 shape: 32.9 days old, ~3 days past the window."""
    _stamp(pw, datetime.now(UTC) - timedelta(days=32.9))
    _, _, left, _ = pw.session_age()
    assert left < 0


def test_a_naive_stamp_is_read_as_utc_not_crashed_on(pw):
    """A hand-written stamp, or one from an older build, may lack a zone.
    Subtracting a naive datetime from an aware one raises TypeError — which
    would take out the heartbeat check that exists to prevent this outage."""
    pw.SEED_STAMP.write_text(datetime.now().replace(tzinfo=None).isoformat())
    _, age, _, _ = pw.session_age()
    assert age == pytest.approx(0, abs=0.5)


# ---- the fallback, for a profile seeded before the stamp existed ----------

def test_an_unstamped_profile_is_dated_from_its_oldest_creation_file(pw):
    """The situation on Forge on 2026-08-30: a real profile, no stamp.

    Chromium rewrites the profile directory's mtime on every headless pass, so
    that is useless; these files are written once at creation.
    """
    old = datetime.now(UTC) - timedelta(days=33)
    ls = pw.SESSION_DIR / "Default" / "Local Storage" / "leveldb"
    ls.mkdir(parents=True)
    (ls / "CURRENT").write_text("MANIFEST-000001\n")
    import os
    os.utime(ls / "CURRENT", (old.timestamp(), old.timestamp()))
    seeded, age, left, source = pw.session_age()
    assert source == "profile-birth"
    assert 32.9 < age < 33.1
    assert left < 0


def test_the_fallback_takes_the_oldest_candidate_not_the_first(pw):
    """A Chromium upgrade rewrites `Last Version`. Trusting it would make the
    session look younger than it is and push the warning past the expiry —
    silence in exactly the window the warning exists for. Over-estimating the
    age only warns early, which costs nothing."""
    import os
    real = datetime.now(UTC) - timedelta(days=28)
    upgraded = datetime.now(UTC) - timedelta(days=1)
    ls = pw.SESSION_DIR / "Default" / "Local Storage" / "leveldb"
    ls.mkdir(parents=True)
    (ls / "CURRENT").write_text("x")
    os.utime(ls / "CURRENT", (real.timestamp(), real.timestamp()))
    (pw.SESSION_DIR / "Last Version").write_text("140.0.0.0")
    os.utime(pw.SESSION_DIR / "Last Version",
             (upgraded.timestamp(), upgraded.timestamp()))
    _, age, _, _ = pw.session_age()
    assert 27.9 < age < 28.1


def test_the_stamp_beats_the_fallback(pw):
    """A re-seed into an existing profile leaves the creation files at their
    ORIGINAL date. Preferring them would report a freshly re-seeded session as
    a month old and nag forever."""
    import os
    old = datetime.now(UTC) - timedelta(days=40)
    (pw.SESSION_DIR / "Last Version").write_text("x")
    os.utime(pw.SESSION_DIR / "Last Version", (old.timestamp(), old.timestamp()))
    _stamp(pw, datetime.now(UTC))
    _, age, _, source = pw.session_age()
    assert source == "stamp"
    assert age < 0.01


# ---- the two "no answer" states are not the same -------------------------

def test_nothing_seeded_at_all(pw, capsys):
    for p in sorted(pw.SESSION_DIR.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    assert pw.session_age() is None
    rc = pw.cmd_session_age(_Args(json=True))
    assert rc == pw.EXIT_AUTH
    assert json.loads(capsys.readouterr().out)["seeded"] is False


def test_seeded_but_undatable_is_not_an_auth_failure(pw, capsys):
    """Sending Gregg to re-seed a working session because we cannot date it
    would train him to ignore the message that matters."""
    (pw.SESSION_DIR / "something").write_text("x")
    assert pw.session_age() is None
    rc = pw.cmd_session_age(_Args(json=True))
    assert rc == pw.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["seeded"] is True and out["age_known"] is False


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)
