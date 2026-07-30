"""The agent's Claude Code credential, and the silent failure it caused.

Found by a real recording on 2026-07-30. The agent authenticates with the
operator's own ~/.claude/.credentials.json, whose access token lasts hours — and
execute.py bind-mounts it READ-ONLY, so when it expires the CLI cannot write a
refreshed one back. It exits 1 with empty stdout AND stderr, so the pipeline
reported "agent exited 1: " with no diagnosis, marked the failure RETRYABLE, and
burned every retry against a condition only a human can clear.

For an unattended pipeline that is a recurring hard stop, so it gets named
explicitly, checked before a recording arrives, and never retried.
"""
import json
from datetime import UTC, datetime, timedelta

import execute as ex
import pytest


def _write_cred(home, *, expires_in_hours=None, shape=True):
    d = home / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    if not shape:
        (d / ".credentials.json").write_text("not json at all")
        return
    body = {"claudeAiOauth": {"accessToken": "x", "refreshToken": "y"}}
    if expires_in_hours is not None:
        when = datetime.now(UTC) + timedelta(hours=expires_in_hours)
        body["claudeAiOauth"]["expiresAt"] = int(when.timestamp() * 1000)
    (d / ".credentials.json").write_text(json.dumps(body))


def test_a_live_credential_is_not_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(ex.Path, "home", staticmethod(lambda: tmp_path))
    _write_cred(tmp_path, expires_in_hours=8)
    expired, when = ex.credential_expiry()
    assert expired is False
    assert when is not None


def test_an_expired_credential_is_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(ex.Path, "home", staticmethod(lambda: tmp_path))
    _write_cred(tmp_path, expires_in_hours=-1)
    expired, when = ex.credential_expiry()
    assert expired is True
    assert when < datetime.now(UTC)


def test_the_expiry_message_tells_the_operator_what_to_do(tmp_path, monkeypatch):
    """The whole point: the old message was 'agent exited 1: '."""
    monkeypatch.setattr(ex.Path, "home", staticmethod(lambda: tmp_path))
    _write_cred(tmp_path, expires_in_hours=-2)
    msg = ex._credential_problem()
    assert msg and "expired" in msg
    assert "read-only" in msg, "must explain why the CLI cannot self-heal"
    assert "claude" in msg, "must name the remedy"


def test_a_healthy_credential_reports_no_problem(tmp_path, monkeypatch):
    monkeypatch.setattr(ex.Path, "home", staticmethod(lambda: tmp_path))
    _write_cred(tmp_path, expires_in_hours=5)
    assert ex._credential_problem() is None


@pytest.mark.parametrize("kwargs", [
    {"expires_in_hours": None},     # no expiresAt field
    {"shape": False},               # unparseable
])
def test_an_unreadable_credential_is_not_treated_as_expired(tmp_path, monkeypatch,
                                                            kwargs):
    """Failing closed here would block every run on a credential shape we simply
    do not recognise. The run itself reports whatever actually goes wrong."""
    monkeypatch.setattr(ex.Path, "home", staticmethod(lambda: tmp_path))
    _write_cred(tmp_path, **kwargs)
    assert ex.credential_expiry() == (False, None)
    assert ex._credential_problem() is None


def test_a_missing_credential_file_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(ex.Path, "home", staticmethod(lambda: tmp_path))
    assert ex.credential_expiry() == (False, None)
