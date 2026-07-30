"""B2 — `retryable` must actually cause a retry.

Before this, fail() set FAILED, the scan excluded FAILED, and nothing ever tried
again: an API timeout or a transient 503 became a permanent failure with a
`retryable: true` flag recorded beside it, purely decorative.
"""
import json
from datetime import datetime, timedelta, UTC

from conftest import write_record
from vault import FAILED, RETRY_WAIT, Record, load_records


def _rec(vault):
    p = write_record(vault)
    return Record(p, json.loads(p.read_text()))


def test_retryable_failure_waits_rather_than_dying(cfg):
    r = _rec(cfg.vault)
    state = r.fail(cfg.vault, "raw", "upstream 503", retryable=True)
    assert state == RETRY_WAIT
    assert r.data["attempts"] == 1
    assert "next_attempt_at" in r.data


def test_non_retryable_failure_is_permanent(cfg):
    r = _rec(cfg.vault)
    assert r.fail(cfg.vault, "raw", "bad audio", retryable=False) == FAILED
    assert "next_attempt_at" not in r.data


def test_backoff_gives_up_eventually(cfg):
    r = _rec(cfg.vault)
    seen = [r.fail(cfg.vault, "raw", "503", retryable=True) for _ in range(4)]
    assert seen[:3] == [RETRY_WAIT] * 3
    assert seen[3] == FAILED, "must stop retrying, not loop forever"


def test_not_due_before_its_deadline(cfg):
    r = _rec(cfg.vault)
    r.fail(cfg.vault, "raw", "503", retryable=True)
    assert r.due() is False


def test_due_once_the_deadline_passes(cfg):
    r = _rec(cfg.vault)
    r.fail(cfg.vault, "raw", "503", retryable=True)
    past = datetime.now(UTC) - timedelta(minutes=1)
    r.data["next_attempt_at"] = past.isoformat().replace("+00:00", "Z")
    r.save()
    assert r.due() is True


def test_waiting_records_are_excluded_from_the_queue(cfg):
    r = _rec(cfg.vault)
    r.fail(cfg.vault, "raw", "503", retryable=True)
    todo = [x for x in load_records(cfg.vault)
            if x.status not in ("published", FAILED) and x.due()]
    assert todo == []


def test_rearm_forces_it_back_into_the_queue(cfg):
    r = _rec(cfg.vault)
    r.fail(cfg.vault, "raw", "503", retryable=True)
    r.rearm()
    assert r.status == "raw"
    assert "next_attempt_at" not in r.data
