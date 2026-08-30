"""Coverage for ops/heartbeat.py with a FAKE systemctl (subprocess.run is
monkeypatched), so no real systemd, git, or network is touched.

Focus: the two bugs the checks used to miss —
  H5  a crash-looping service updates its exit timestamp on every exit, so
      "ran recently" read as healthy even when every run failed;
  M13 a zone-less ingested_at crashed main() before the alarm could fire,
      suppressing problems the earlier checks had already found.
"""
import importlib.util
import json
import sys
import types

import pytest
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hb = _load("atticus_heartbeat", "ops/heartbeat.py")


def _recent_ts() -> str:
    return datetime.now(UTC).strftime("%a %Y-%m-%d %H:%M:%S UTC")


class FakeSystemctl:
    """Answers the exact `systemctl show` / `git rev-list` calls heartbeat makes.

    `statuses` maps a unit to its ExecMainStatus (default "0" = success).
    """

    def __init__(self, statuses=None, loaded=True, scheduled=True,
                 active=True):
        self.statuses = statuses or {}
        self.loaded = loaded
        self.scheduled = scheduled
        # ActiveState for PATH units only. Deliberately not for services: the
        # heartbeat also calls unit_is_running() on a service to tell "timer
        # parked at infinity" from "timer has no next elapse because its own
        # service is mid-run", and answering `active` there would turn the
        # parked-timer alarm into a benign note — silently disarming the check
        # this file was written around. Services keep the pre-existing answer
        # (empty → not running).
        self.active = active

    def __call__(self, cmd, **kw):
        out = ""
        if cmd and cmd[0] == "systemctl":
            unit = cmd[3]
            if "ActiveState" in cmd:
                if unit.endswith(".path"):
                    out = f"ActiveState={'active' if self.active else 'failed'}\n"
            elif "LoadState" in cmd:
                out = ("loaded" if self.loaded else "not-found") + "\n"
            elif "ExecMainExitTimestamp" in cmd:
                st = self.statuses.get(unit, "0")
                res = "success" if st == "0" else "exit-code"
                out = (f"ExecMainExitTimestamp={_recent_ts()}\n"
                       f"ExecMainStatus={st}\nResult={res}\n")
            elif "NextElapseUSecRealtime" in cmd:
                nxt = _recent_ts() if self.scheduled else "n/a"
                out = (f"NextElapseUSecRealtime={nxt}\n"
                       f"NextElapseUSecMonotonic=n/a\n")
        elif cmd and cmd[0] == "git":
            out = "0\n"            # rev-list --count → level with remote
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)


@pytest.fixture(autouse=True)
def _stub_agent_credential(monkeypatch):
    """Isolate every test here from the MACHINE'S REAL credential state.

    check_agent_credential() reads ~/.claude/.credentials.json and reports a
    problem when the token is expired or expiring within the hour. Without this
    stub, tests that assert an overall rc==0 pass in the morning and fail in the
    evening — which is exactly what happened: two of them started failing the
    moment the real token dropped under an hour of life. A test whose result
    depends on the wall clock is worse than no test.

    Individual tests override this when the credential IS what they are testing.
    """
    import execute
    monkeypatch.setattr(execute, "credential_expiry",
                        lambda: (False, datetime.now(UTC) + timedelta(hours=8)))


@pytest.fixture(autouse=True)
def _stub_plaud_session(monkeypatch, tmp_path):
    """Same isolation, for the OTHER credential that dies on a clock.

    check_plaud_session() reads the machine's real Chromium profile. Left
    unstubbed, every test here would report a different session age depending
    on when Forge was last re-seeded — and on a host whose session is inside
    the warning window, tests would try to send a real push.
    """
    monkeypatch.setenv("PLAUD_SESSION_ROOT", str(tmp_path / "sessions"))
    (tmp_path / "sessions" / "plaud").mkdir(parents=True)
    import importlib
    sys.path.insert(0, str(REPO / "ingest"))
    pw = importlib.import_module("plaud_web")
    monkeypatch.setattr(
        pw, "session_age",
        lambda: (datetime.now(UTC) - timedelta(days=1), 1.0, 29.0, "stamp"))


def _empty_vault(tmp_path) -> Path:
    v = tmp_path / "vault"
    (v / "inbox").mkdir(parents=True)
    return v


def _write_record(vault: Path, stem, *, status, ingested_at):
    d = vault / "inbox" / "2026" / "07"
    d.mkdir(parents=True, exist_ok=True)
    meta = {"plaud_id": "p1", "recorded_at": "2020-01-01T00:00:00Z",
            "ingested_at": ingested_at, "audio_filename": f"{stem}.mp3",
            "status": status}
    (d / f"{stem}.json").write_text(json.dumps(meta, indent=2) + "\n")
    (d / f"{stem}.mp3").write_bytes(b"\xff\xfb\x90\x00" + b"\0" * 512)


# ---- H5: exit status, not just exit timestamp -----------------------------

def test_unit_last_run_flags_nonzero_exit(monkeypatch):
    monkeypatch.setattr(hb.subprocess, "run",
                        FakeSystemctl(statuses={"atticus-processor.service": "1"}))
    when, ok = hb.unit_last_run("atticus-processor.service")
    assert when is not None          # it DID record an exit...
    assert ok is False               # ...but a failed one

    when2, ok2 = hb.unit_last_run("atticus-ingest.service")
    assert ok2 is True


def test_main_alarms_on_crash_loop(monkeypatch, tmp_path, capsys):
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb.subprocess, "run",
                        FakeSystemctl(statuses={"atticus-processor.service": "1"}))
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])

    rc = hb.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "non-zero" in out


# ---- M13: a naive ingested_at must not crash the whole heartbeat ----------

def test_main_survives_naive_ingested_at(monkeypatch, tmp_path, capsys):
    vault = _empty_vault(tmp_path)
    _write_record(vault, "rec1", status="raw",
                  ingested_at="2020-01-01T00:00:00")   # no timezone
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb.subprocess, "run", FakeSystemctl())
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])

    rc = hb.main()                    # must not raise
    out = capsys.readouterr().out
    assert rc == 1
    assert "naive" in out


# ---- M15: only check units this host actually has -------------------------

def test_missing_units_are_skipped_not_alarmed(monkeypatch, tmp_path, capsys):
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    # Nothing loaded → an ingest-only or processor-only host. No unit should
    # be reported missing, because none of them belong to this host.
    monkeypatch.setattr(hb.subprocess, "run", FakeSystemctl(loaded=False))
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])

    rc = hb.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "never fire" not in out
    assert "no record of ever completing" not in out


def test_a_timer_parked_at_infinity_alarms(monkeypatch, tmp_path, capsys):
    """The one failure this whole file was written around, previously untested.

    atticus-vault-site.timer sat enabled AND active with next_elapse=infinity for
    76 minutes on 2026-07-29. A timer that never fires cannot alarm about not
    firing, which makes it the failure that defeats every other safeguard here.
    FakeSystemctl already supported scheduled=False; no test ever used it, so a
    regression in timer_is_scheduled()'s string-slicing would go unnoticed.
    """
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb.subprocess, "run",
                        FakeSystemctl(loaded=True, scheduled=False))
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])

    rc = hb.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "NO next elapse" in out
    assert "atticus-processor.timer" in out


def test_retention_and_site_timers_are_watched(monkeypatch, tmp_path, capsys):
    """Neither was in the watch list. retention enforces a privacy policy whose
    own unit comment says silently not running is the failure to prevent, and the
    site timer is the one that actually broke."""
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb.subprocess, "run",
                        FakeSystemctl(loaded=True, scheduled=False))
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])

    hb.main()
    out = capsys.readouterr().out
    assert "atticus-retention.timer" in out
    assert "atticus-vault-site.timer" in out


def test_a_wrong_vault_path_is_not_reported_healthy(monkeypatch, tmp_path, capsys):
    """load_records() returns [] for a missing inbox and check_sync returns early
    with no .git, so a typo'd ATTICUS_VAULT_PATH used to produce a fully green
    heartbeat while the real vault accumulated unprocessed work."""
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(hb.subprocess, "run", FakeSystemctl())
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])

    rc = hb.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "does not exist" in out


def test_a_timer_that_has_not_fired_yet_is_not_a_problem(monkeypatch, tmp_path, capsys):
    """retention is on a DAILY timer. Installed at noon, it has genuinely never
    triggered until midnight — and reporting that hourly as "no record of ever
    completing" is true, useless, and trains the operator to ignore the alarm that
    matters. Observed in production after retention joined the watch list.
    """
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb, "unit_exists", lambda u: "retention" in u)
    monkeypatch.setattr(hb, "unit_is_running", lambda u: False)
    monkeypatch.setattr(hb, "timer_is_scheduled", lambda t: True)
    monkeypatch.setattr(hb, "timer_has_fired", lambda t: False)
    monkeypatch.setattr(hb, "unit_last_run", lambda u: (None, True))
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])

    rc = hb.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "has not run yet" in out
    assert "no record of ever completing" not in out


def test_a_unit_that_never_ran_and_whose_timer_HAS_fired_is_a_problem(
        monkeypatch, tmp_path, capsys):
    """The real failure must survive the fix above."""
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb, "unit_exists", lambda u: "retention" in u)
    monkeypatch.setattr(hb, "unit_is_running", lambda u: False)
    monkeypatch.setattr(hb, "timer_is_scheduled", lambda t: True)
    monkeypatch.setattr(hb, "timer_has_fired", lambda t: True)
    monkeypatch.setattr(hb, "unit_last_run", lambda u: (None, True))
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])

    assert hb.main() == 1
    assert "no record of ever completing" in capsys.readouterr().out


def test_a_unit_sampled_mid_run_is_not_reported_broken(monkeypatch, tmp_path, capsys):
    """systemd clears ExecMainExitTimestamp during a run and a timer whose service
    is executing reports no next elapse. The 14:05:06 heartbeat and the 14:05:06
    processor run started in the same second, and every timer here fires on a :0X
    boundary — so this collision is structural, not unlucky.
    """
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb, "unit_exists", lambda u: "processor" in u)
    monkeypatch.setattr(hb, "unit_is_running", lambda u: True)
    monkeypatch.setattr(hb, "timer_is_scheduled", lambda t: False)
    monkeypatch.setattr(hb, "unit_last_run", lambda u: (None, True))
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])

    rc = hb.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "will never fire again" not in out
    assert "no record of ever completing" not in out


def test_a_dead_path_watcher_alarms(monkeypatch, tmp_path, capsys):
    """atticus-vault-site.path sat in `failed` for a day and a half unnoticed.

    It rebuilds the site the moment the vault changes, which is what makes a
    result notification's link work when it is tapped. With it dead the site
    still rebuilt on its 5-minute timer, so everything LOOKED healthy — the only
    symptom was a 404 on a fresh notification, found by the operator on
    2026-08-01. Timers were watched here; path units were not.
    """
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb.subprocess, "run",
                        FakeSystemctl(loaded=True, active=False))
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])

    rc = hb.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "atticus-vault-site.path is NOT active" in out
    assert "reset-failed" in out, "the alarm must carry the fix, not just the fact"


def test_a_live_path_watcher_is_quiet(monkeypatch, tmp_path, capsys):
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb.subprocess, "run", FakeSystemctl(loaded=True))
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])

    rc = hb.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT active" not in out
    assert "atticus-vault-site.path watching" in out


# ---- the Plaud session's own clock ---------------------------------------
#
# The 2026-08-27 outage was not a detection failure in the usual sense: every
# check here worked, and the crash-loop check fired correctly. It fired three
# days LATE, because nothing watched the one clock that was always going to run
# out. The fix needs a person at a browser, so the warning has to arrive while
# there is still time to act.

def _plaud_age(monkeypatch, days_left, source="stamp"):
    import importlib
    sys.path.insert(0, str(REPO / "ingest"))
    pw = importlib.import_module("plaud_web")
    seeded = datetime.now(UTC) - timedelta(days=30.0 - days_left)
    monkeypatch.setattr(pw, "session_age",
                        lambda: (seeded, 30.0 - days_left, days_left, source))


def test_a_healthy_session_is_just_a_note(monkeypatch, tmp_path, capsys):
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb.subprocess, "run", FakeSystemctl())
    _plaud_age(monkeypatch, 21.0)
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])
    rc = hb.main()
    out = capsys.readouterr().out
    assert "plaud session good for 21.0 more days" in out
    assert rc == 0


def test_an_expiring_session_warns_without_failing_the_heartbeat(
        monkeypatch, tmp_path, capsys):
    """The whole point of the severity split. Folding this into `problems`
    would fire a CRITICAL every hour for the last five days of every 30-day
    window — the "alarm that always fires" this file warns about twice. The box
    is healthy; a chore is due."""
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb.subprocess, "run", FakeSystemctl())
    _plaud_age(monkeypatch, 3.0)
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])
    rc = hb.main()
    out = capsys.readouterr().out
    assert "plaud session expires in 3.0 days" in out
    assert "BAD" not in out
    assert rc == 0


def test_an_expiring_session_pushes_at_most_once_a_day(monkeypatch, tmp_path):
    """The shared 6h throttle would send four pushes a day for five days —
    twenty messages to convey one chore."""
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb.subprocess, "run", FakeSystemctl())
    monkeypatch.setattr(hb.nf, "STATE", tmp_path / "state")
    (tmp_path / "state").mkdir()
    _plaud_age(monkeypatch, 2.0)
    sent = []
    monkeypatch.setattr(hb.nf, "alarm",
                        lambda *a, **k: (sent.append(k.get("key")),
                                         (tmp_path / "state" /
                                          f"alarm-{k.get('key')}").touch(),
                                         {"ntfy": True, "calendar": False,
                                          "deferred": False})[-1])
    monkeypatch.setattr(sys, "argv", ["heartbeat.py"])
    hb.main()
    hb.main()
    assert sent == ["plaud-session-expiring"]


def test_an_already_expired_session_does_not_double_up_on_ingest(
        monkeypatch, tmp_path, capsys):
    """Ingest's own dead-session alarm owns this case at CRITICAL. Saying it
    again here, in a second severity, is noise."""
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb.subprocess, "run", FakeSystemctl())
    monkeypatch.setattr(hb.nf, "STATE", tmp_path / "state")
    (tmp_path / "state").mkdir()
    _plaud_age(monkeypatch, -2.9)
    sent = []
    monkeypatch.setattr(hb.nf, "alarm", lambda *a, **k: sent.append(k.get("key"))
                        or {"ntfy": True, "calendar": False, "deferred": False})
    monkeypatch.setattr(sys, "argv", ["heartbeat.py"])
    hb.main()
    assert "plaud-session-expiring" not in sent
    assert "plaud session EXPIRED 2.9d ago" in capsys.readouterr().out


def test_an_estimated_age_says_so(monkeypatch, tmp_path, capsys):
    """A profile dated from its creation files is a floor on the age, not a
    measurement. Reporting it as fact would be a small lie in a message whose
    whole job is to be trusted."""
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb.subprocess, "run", FakeSystemctl())
    _plaud_age(monkeypatch, 2.0, source="profile-birth")
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])
    hb.main()
    assert "(estimated from the profile)" in capsys.readouterr().out


def test_a_host_without_ingest_is_not_asked_about_a_session(
        monkeypatch, tmp_path, capsys):
    """Roles are capabilities, not hostnames. A processor-only host has no
    Plaud session and must not be nagged to re-seed one."""
    vault = _empty_vault(tmp_path)
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(hb.subprocess, "run", FakeSystemctl(loaded=False))
    _plaud_age(monkeypatch, 1.0)
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--dry-run"])
    hb.main()
    assert "plaud session" not in capsys.readouterr().out
