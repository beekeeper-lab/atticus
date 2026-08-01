"""Reminders (#52): a delivery at a time.

The properties worth testing here are almost all about TIME, because that is the
only part of this feature that can be wrong while looking right. A reminder stored
with the wrong instant does not fail — it arrives four hours late and reads as a
broken feature rather than a misconfigured one. So:

  * a naive "at" is LOCAL wall clock, never UTC;
  * the stored value is UTC, and the notification carries the local time;
  * a zone that DST would move is resolved by NAME, not by today's offset;
  * an unusable ATTICUS_LOCAL_TZ is refused, never silently treated as UTC.

The rest is the ledger contract: append-only, marked and not deleted, and a
missed push still diagnosable afterwards.

No network. notify() is monkeypatched at the module boundary, and the cfg fixture
points every real endpoint at a closed port anyway.
"""
import json
from datetime import UTC, datetime, timedelta

import outbox
import pytest
import reminders as store
from handlers import reminders as handler   # noqa: F401  registers reminders.set

ZONE = "America/New_York"        # UTC-5 in January, UTC-4 in July: DST matters


@pytest.fixture
def rcfg(cfg):
    """cfg with an EXPLICIT zone. Never inherit the developer's /etc/localtime —
    that is how a timezone test passes on one machine and fails on CI.

    The sanity bounds are also off here, so a test asserting that 16:00 local on a
    FIXED date becomes a particular UTC instant keeps meaning that next year. The
    bounds have their own tests, which set them explicitly.
    """
    cfg.local_tz = ZONE
    cfg.reminder_max_days = 0
    cfg.reminder_max_late_hours = 0
    cfg.vault.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def sent(monkeypatch):
    """Every notify() call the drain makes. Returns True unless told otherwise."""
    calls = []

    def fake(target, text, log=None, key=None, title="Atticus",
             tags="warning", priority="high"):
        calls.append({"text": text, "title": title, "tags": tags,
                      "priority": priority, "url": getattr(target, "notify_url", None)})
        if fake.fail:
            if log:
                log("notification failed: URLError: Connection refused")
            return False
        return True
    fake.fail = False
    monkeypatch.setattr(store.nf, "notify", fake)
    return calls


def _req(**over):
    req = {"verb": "reminders.set", "text": "Call the bank",
           "at": "2026-08-01T16:00", "said": "at four", "_file": "001-reminders.set.json"}
    req.update(over)
    return req


def _set(cfg, **over):
    return outbox.handler_for("reminders.set")["fn"](_req(**over), cfg, log=lambda m: None)


def _rows(vault):
    return [json.loads(ln) for ln in
            store.ledger_path(vault).read_text().splitlines() if ln.strip()]


# ── the verb is registered, and registered as internal ─────────────────────
def test_the_verb_exists_and_is_internal():
    """INTERNAL, so it runs unattended. Holding a reminder for confirmation until
    a human is present defeats the entire point of setting one by voice."""
    h = outbox.handler_for("reminders.set")
    assert h is not None, "importing handlers must register reminders.set"
    assert h["risk"] == outbox.INTERNAL
    assert h["schema"] == ("text",)


def test_an_internal_reminder_is_performed_unattended(rcfg, tmp_path):
    out = tmp_path / "output"
    (out / "outbox").mkdir(parents=True)
    (out / "outbox" / "001-reminders.set.json").write_text(json.dumps(_req()))
    res = outbox.process(out, rcfg, log=lambda m: None)
    assert res["done"] == 1, res["receipts"]
    assert res["receipts"][0]["tz"] == ZONE
    assert store.ledger_path(rcfg.vault).is_file()


def test_the_receipt_summary_echoes_what_was_HEARD(rcfg):
    """The receipt is committed beside the report. When a push arrives at the wrong
    moment, the useful question is what the words were, not what UTC instant they
    became — that is in the ledger."""
    s = outbox.describe(_req())
    assert "Call the bank" in s and "at four" in s


# ── timezone: the whole point of the feature ───────────────────────────────
def test_a_naive_at_is_LOCAL_wall_clock_not_UTC(rcfg):
    """THE test. 16:00 in New York on 1 August is 20:00Z. Reading the naive
    timestamp as UTC stores 16:00Z and delivers the push four hours early — which
    presents as a broken reminder, not as a timezone bug."""
    res = _set(rcfg, at="2026-08-01T16:00")
    assert res["at"] == "2026-08-01T20:00:00Z"


def test_the_stored_instant_is_utc_with_a_Z(rcfg):
    """Vault convention. Everything else here is UTC ISO-8601 and the drain
    compares against it."""
    res = _set(rcfg, at="2026-12-01T09:30")
    assert res["at"] == "2026-12-01T14:30:00Z", "December is EST, UTC-5"
    assert _rows(rcfg.vault)[0]["at"] == res["at"]


def test_the_zone_is_resolved_by_name_so_dst_applies(rcfg):
    """Same wall-clock hour, six months apart, different UTC instants. A fixed
    offset captured 'now' — which datetime.now().astimezone() gives you — would be
    an hour wrong for half the year."""
    summer = _set(rcfg, at="2026-07-01T16:00")["at"]
    winter = _set(rcfg, at="2026-01-01T16:00")["at"]
    assert summer == "2026-07-01T20:00:00Z"     # EDT, UTC-4
    assert winter == "2026-01-01T21:00:00Z"     # EST, UTC-5


def test_an_explicit_zone_in_the_request_is_honoured(rcfg):
    """For the rare request that really was absolute — "at 16:00 UTC"."""
    assert _set(rcfg, at="2026-08-01T16:00:00Z")["at"] == "2026-08-01T16:00:00Z"
    assert _set(rcfg, at="2026-08-01T16:00:00+01:00")["at"] == "2026-08-01T15:00:00Z"


def test_an_unknown_local_tz_is_refused_not_silently_utc(rcfg):
    """Silently falling back to UTC is precisely the four-hours-late bug. It has to
    fail loudly and name the variable."""
    rcfg.local_tz = "America/Nowhere_At_All"
    with pytest.raises(outbox.OutboxError, match="ATTICUS_LOCAL_TZ"):
        _set(rcfg)


def test_an_unknown_local_tz_stops_the_drain_even_with_nothing_due(rcfg):
    """Checked BEFORE the "nothing due" exit. Otherwise a broken zone hides all
    afternoon: the drain exits 0 every minute and heartbeat calls it healthy, while
    every reminder on the host is mistimed."""
    rcfg.local_tz = "America/Nowhere_At_All"
    with pytest.raises(store.ReminderError, match="ATTICUS_LOCAL_TZ"):
        store.drain(rcfg, log=lambda m: None)


def test_a_blank_local_tz_falls_back_to_a_usable_host_zone(cfg):
    """Blank must work — refusing to set a reminder without a config line would
    kill the feature on arrival. The label is what the operator reads in the push,
    so it has to be non-empty whatever the host looks like."""
    cfg.local_tz = ""
    cfg.vault.mkdir(parents=True, exist_ok=True)
    zone, label = store.local_zone(cfg)
    assert zone is not None and label.strip()
    assert store.resolve_when({"in_minutes": 5}, cfg)[0].tzinfo is not None


def test_in_minutes_needs_no_timezone_at_all(rcfg):
    """Why relative requests are preferred: a misconfigured zone cannot move them."""
    now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    when, _ = store.resolve_when({"in_minutes": 20}, rcfg, now=now)
    assert when == now + timedelta(minutes=20)
    rcfg.local_tz = "Asia/Tokyo"
    assert store.resolve_when({"in_minutes": 20}, rcfg, now=now)[0] == when


def test_a_bare_time_of_day_means_the_NEXT_such_local_time(rcfg):
    """"At four" spoken at 2pm is today; spoken at 6pm it is tomorrow. Nobody sets
    a reminder for a moment that has gone."""
    afternoon = datetime(2026, 8, 1, 18, 0, tzinfo=UTC)      # 14:00 EDT
    when, _ = store.resolve_when({"at": "16:00"}, rcfg, now=afternoon)
    assert store.iso_z(when) == "2026-08-01T20:00:00Z"       # today, 16:00 EDT

    evening = datetime(2026, 8, 1, 22, 0, tzinfo=UTC)        # 18:00 EDT
    when, _ = store.resolve_when({"at": "16:00"}, rcfg, now=evening)
    assert store.iso_z(when) == "2026-08-02T20:00:00Z"       # tomorrow


# ── refusing what cannot be honoured ───────────────────────────────────────
def test_a_request_with_no_time_is_refused_by_name(rcfg):
    with pytest.raises(outbox.OutboxError, match="in_minutes"):
        _set(rcfg, at="")


def test_an_unreadable_time_is_refused_with_the_wanted_shape(rcfg):
    with pytest.raises(outbox.OutboxError, match="wall-clock"):
        _set(rcfg, at="next Tuesdayish")


def test_an_absurdly_distant_date_is_refused_as_a_misparse(rcfg):
    """A mangled year would otherwise be stored as a reminder that never fires,
    with a JSONL line as the only evidence."""
    rcfg.reminder_max_days = 365
    with pytest.raises(outbox.OutboxError, match="days away"):
        _set(rcfg, at="2126-08-01T16:00")


def test_a_long_past_date_is_refused_as_a_misparse(rcfg):
    rcfg.reminder_max_late_hours = 24
    with pytest.raises(outbox.OutboxError, match="in the past"):
        _set(rcfg, at="2019-08-01T16:00")


def test_empty_text_is_refused(rcfg):
    with pytest.raises(outbox.OutboxError, match="text"):
        _set(rcfg, text="   ")


def test_text_is_one_bounded_line_because_it_lands_on_a_lock_screen(rcfg):
    _set(rcfg, text="  call\nthe   bank  " + "x" * 400)
    row = _rows(rcfg.vault)[0]
    assert "\n" not in row["text"] and len(row["text"]) <= store.MAX_TEXT
    assert row["text"].startswith("call the bank")


# ── the ledger: append-only, marked not deleted ────────────────────────────
def test_setting_a_reminder_appends_a_pending_row_to_the_state_ledger(rcfg):
    _set(rcfg)
    assert store.ledger_path(rcfg.vault) == rcfg.vault / ".state/reminders.jsonl"
    rows = _rows(rcfg.vault)
    assert len(rows) == 1
    assert rows[0]["status"] == store.PENDING
    assert rows[0]["at"] == "2026-08-01T20:00:00Z"
    assert rows[0]["said"] == "at four", "what was heard is kept for diagnosis"
    assert rows[0]["source"] == "001-reminders.set.json"


def test_the_same_reminder_set_twice_is_one_reminder(rcfg):
    """--retry re-runs a record's outbox, and a duplicate push is
    indistinguishable from a bug in the drain."""
    first = _set(rcfg)
    again = _set(rcfg)
    assert again["id"] == first["id"] and again.get("already_set")
    assert len(_rows(rcfg.vault)) == 1


def test_different_reminders_get_different_ids(rcfg):
    a = _set(rcfg, text="Call the bank")
    b = _set(rcfg, text="Call the dentist")
    c = _set(rcfg, at="2026-08-01T17:00")
    assert len({a["id"], b["id"], c["id"]}) == 3


def test_a_truncated_line_does_not_blind_the_ledger_to_the_rows_around_it(rcfg):
    _set(rcfg)
    with store.ledger_path(rcfg.vault).open("a") as f:
        f.write('{"id": "half-writ\n')
    _set(rcfg, text="Second thing")
    assert len(store.open_reminders(rcfg.vault)) == 2


# ── the drain ──────────────────────────────────────────────────────────────
def _due_now(cfg, minutes_ago=0, **over):
    when = store._utcnow() - timedelta(minutes=minutes_ago)
    text = over.pop("text", "Call the bank")
    return store.add(cfg.vault, when=when, text=text, zone_label=ZONE, **over)


def test_nothing_due_is_a_silent_no_op(rcfg, sent):
    rcfg.result_notify_url = "https://ntfy.example/atticus"
    store.add(rcfg.vault, when=store._utcnow() + timedelta(hours=3),
              text="Later", zone_label=ZONE)
    res = store.drain(rcfg, log=lambda m: None)
    assert res == {"due": 0, "fired": 0, "expired": 0, "failed": 0, "open": 1}
    assert not sent


def test_a_due_reminder_fires_and_is_MARKED_not_deleted(rcfg, sent):
    rcfg.result_notify_url = "https://ntfy.example/atticus"
    rec = _due_now(rcfg)
    res = store.drain(rcfg, log=lambda m: None)
    assert res["fired"] == 1
    rows = _rows(rcfg.vault)
    assert [r["status"] for r in rows] == [store.PENDING, store.DELIVERED]
    assert rows[0]["id"] == rows[1]["id"] == rec["id"]
    assert not store.open_reminders(rcfg.vault), "delivered is no longer owed"


def test_the_notification_carries_the_LOCAL_time_and_the_zone(rcfg, sent):
    """The single line that makes a timezone mistake visible the first time it
    happens instead of after weeks of vaguely mistimed pushes."""
    rcfg.result_notify_url = "https://ntfy.example/atticus"
    when = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)          # 16:00 EDT
    store.add(rcfg.vault, when=when, text="Call the bank", zone_label=ZONE)
    store.drain(rcfg, log=lambda m: None, now=when)
    body = sent[0]["text"]
    assert "Call the bank" in body
    assert "4:00 PM" in body and "Sat 1 Aug" in body
    assert ZONE in body


def test_a_reminder_pushes_at_high_priority(rcfg, sent):
    """The operator asked to be interrupted at this moment. A reminder that loses
    to a notification summary has not been delivered."""
    rcfg.result_notify_url = "https://ntfy.example/atticus"
    _due_now(rcfg)
    store.drain(rcfg, log=lambda m: None)
    assert sent[0]["priority"] == "high"
    assert sent[0]["url"] == "https://ntfy.example/atticus", "the RESULT topic"


def test_a_reminder_missed_while_the_box_was_down_fires_LATE_WITH_A_NOTE(rcfg, sent):
    """Firing late is the decision: a late reminder is usually still worth having,
    and dropping one silently is the only outcome with no recovery. But it must SAY
    it was late, or it reads as a reminder set for the wrong time."""
    rcfg.result_notify_url = "https://ntfy.example/atticus"
    _due_now(rcfg, minutes_ago=192)
    res = store.drain(rcfg, log=lambda m: None)
    assert res["fired"] == 1
    body = sent[0]["text"]
    assert "was due at" in body and "3h 12m ago" in body
    assert _rows(rcfg.vault)[-1]["late_seconds"] >= 192 * 60


def test_an_on_time_reminder_does_not_claim_to_be_late(rcfg, sent):
    rcfg.result_notify_url = "https://ntfy.example/atticus"
    _due_now(rcfg)
    store.drain(rcfg, log=lambda m: None)
    assert "was due" not in sent[0]["text"]
    assert "Set for" in sent[0]["text"]


def test_a_reminder_too_old_to_fire_is_expired_and_REPORTED_not_dropped(rcfg, sent):
    """Past the window, one grouped push. A machine down for a week must not fire
    nine days of stale errands at once — but silence would mean the operator never
    learns something was owed to them."""
    rcfg.result_notify_url = "https://ntfy.example/atticus"
    rcfg.reminder_max_late_hours = 24
    _due_now(rcfg, minutes_ago=60 * 40, text="Call the bank")
    _due_now(rcfg, minutes_ago=60 * 50, text="Move the car")
    res = store.drain(rcfg, log=lambda m: None)
    assert res["expired"] == 2 and res["fired"] == 0
    assert [r["status"] for r in _rows(rcfg.vault)[-2:]] == [store.EXPIRED, store.EXPIRED]
    assert len(sent) == 1, "grouped into one message, not one per reminder"
    assert "Call the bank" in sent[0]["text"] and "Move the car" in sent[0]["text"]
    assert "missed" in sent[0]["title"].lower()


def test_max_late_hours_zero_fires_everything_however_old(rcfg, sent):
    rcfg.result_notify_url = "https://ntfy.example/atticus"
    rcfg.reminder_max_late_hours = 0
    _due_now(rcfg, minutes_ago=60 * 24 * 9)
    assert store.drain(rcfg, log=lambda m: None)["fired"] == 1


def test_a_failed_push_stays_owed_and_records_the_failure_ONCE(rcfg, sent):
    """A push that did not arrive must be diagnosable and must be retried. But the
    drain runs every minute, so a dead endpoint recording every attempt would write
    1,440 lines a day into a ledger that is committed to git — so it records the
    TRANSITION into failing, not each try."""
    rcfg.result_notify_url = "https://ntfy.example/atticus"
    _due_now(rcfg)
    store.nf.notify.fail = True
    assert store.drain(rcfg, log=lambda m: None)["failed"] == 1
    assert store.drain(rcfg, log=lambda m: None)["failed"] == 1
    deferred = [r for r in _rows(rcfg.vault) if r["status"] == store.DEFERRED]
    assert len(deferred) == 1
    assert "Connection refused" in deferred[0]["reason"]
    assert len(store.open_reminders(rcfg.vault)) == 1, "still owed"

    # …and it delivers when the endpoint comes back.
    store.nf.notify.fail = False
    assert store.drain(rcfg, log=lambda m: None)["fired"] == 1
    assert not store.open_reminders(rcfg.vault)


def test_with_no_notify_url_configured_nothing_is_marked(rcfg, sent):
    """Marking them delivered would mean configuring the URL later silently
    delivers nothing at all."""
    rcfg.notify_url = rcfg.result_notify_url = None
    _due_now(rcfg)
    res = store.drain(rcfg, log=lambda m: None)
    assert res["held"] and res["fired"] == 0
    assert not sent
    assert len(store.open_reminders(rcfg.vault)) == 1
    assert [r["status"] for r in _rows(rcfg.vault)] == [store.PENDING]


def test_an_unreadable_at_in_the_ledger_does_not_stop_the_reminder_beside_it(rcfg, sent):
    rcfg.result_notify_url = "https://ntfy.example/atticus"
    store.append(rcfg.vault, "deadbeef0000", store.PENDING, at="whenever",
                 text="Broken row")
    _due_now(rcfg)
    assert store.drain(rcfg, log=lambda m: None)["fired"] == 1


def test_drain_never_touches_git(rcfg, sent, monkeypatch):
    """It writes the vault working tree and the next processor pass commits it —
    `.state` is in OWNED_PROCESSOR. A one-minute timer doing its own pull/push
    would be ~1,440 git round trips a day contending the vault git lock."""
    import vault as v

    def boom(*a, **k):
        raise AssertionError("the drain must not run git")
    monkeypatch.setattr(v.Git, "commit_push", boom)
    rcfg.result_notify_url = "https://ntfy.example/atticus"
    _due_now(rcfg)
    assert store.drain(rcfg, log=lambda m: None)["fired"] == 1


# ── formatting helpers, because they are what the operator actually reads ──
@pytest.mark.parametrize("seconds,want", [
    (30, "30s"), (600, "10m"), (3600, "1h"), (11520, "3h 12m"),
    (60 * 60 * 26, "1d 2h"), (60 * 60 * 48, "2d"),
])
def test_human_delta(seconds, want):
    assert store.human_delta(seconds) == want


@pytest.mark.parametrize("hour,minute,want", [
    (0, 5, "12:05 AM"), (9, 0, "9:00 AM"), (12, 0, "12:00 PM"), (16, 30, "4:30 PM"),
])
def test_local_times_read_the_way_a_person_says_them(hour, minute, want):
    """%-I is glibc-only, so this is built by hand — and therefore worth testing."""
    from zoneinfo import ZoneInfo
    dt = datetime(2026, 8, 1, hour, minute, tzinfo=ZoneInfo(ZONE))
    assert store.fmt_local(dt, ZoneInfo(ZONE)).startswith(want)


# ── the skill and the handler are two halves of one feature ────────────────
def test_the_skill_pastes_the_outbox_contract_verbatim():
    """One source of truth for the contract, or ten skills drift into ten dialects
    of it."""
    from pathlib import Path
    md = (Path(__file__).resolve().parents[2] / "skills/reminders/SKILL.md").read_text()
    assert outbox.CONTRACT.strip() in md


def test_the_skill_declares_the_verb_the_handler_registers():
    from pathlib import Path
    md = (Path(__file__).resolve().parents[2] / "skills/reminders/SKILL.md").read_text()
    assert "reminders.set" in md
    # The routing distinction issue #52 exists to make: a reminder is a delivery at
    # a time; a todo has no deadline behaviour. If the description stops saying so,
    # the model will route "remind me at four" to the todo skill.
    front = md.split("---")[1]
    assert "todo" in front, "the description must route AWAY from todo"
    for word in ("at four", "in twenty minutes"):
        assert word in front, f"the description needs a time-bearing example: {word}"
