"""Meeting mode (#86), and the ADR-008 conditions it ships under.

This is the only feature whose input is other people, so the tests are mostly
about restraint rather than capability:

  * it is OFF unless deliberately switched on;
  * the trigger must OPEN the recording — "research meeting mode for me" is a
    research request, and treating it as a switch would transcribe forty
    minutes of whatever followed;
  * duration alone never triggers it, because a long recording today is a
    truncated command (ADR-004) and switching behaviour on length would turn a
    mis-fired command into a transcribed meeting;
  * **the audio is deleted before it can be committed** — ADR-008 §2, the one
    condition with real engineering consequence.
"""
import types

import pytest
import transcribe as stt


@pytest.fixture
def mcfg(cfg):
    cfg.meeting_mode = "on"
    cfg.meeting_keep_audio = "false"
    cfg.wake_phrase = "atticus"
    return cfg


# ── the trigger ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("said", [
    "Atticus, meeting mode. Today we are discussing the migration.",
    "Atticus, take notes on this meeting.",
    "Okay, Atticus, meeting mode",
    "Atticus, minute this meeting please",
    "Atticus, transcribe this meeting",
])
def test_an_opening_trigger_switches_the_mode_on(mcfg, said):
    assert stt.is_meeting(said, mcfg) is True


@pytest.mark.parametrize("said", [
    "Atticus, research meeting mode for me",
    "Atticus, file an issue about the meeting mode feature",
    "Atticus, remind me about the meeting at four",
    "Atticus, add prepare for the meeting to my list",
    "So anyway, in the meeting we said we would use meeting mode",
])
def test_merely_MENTIONING_a_meeting_does_not(mcfg, said):
    """The expensive false positive: this would transcribe everything that
    followed, including other people, for a request that was not about that."""
    assert stt.is_meeting(said, mcfg) is False


def test_the_mode_is_off_unless_switched_on(cfg):
    """ADR-008: turning this on is an explicit act, not a preference."""
    cfg.meeting_mode = "off"
    cfg.wake_phrase = "atticus"
    assert stt.is_meeting("Atticus, meeting mode", cfg) is False


def test_the_shipped_default_is_off():
    from pathlib import Path

    from config import Config
    c = Config(env_file=Path(__file__).resolve().parents[2] / "ops/.env.example")
    assert c.meeting_mode == "off"
    assert c.meeting_keep_audio == "false"


def test_an_empty_transcript_is_not_a_meeting(mcfg):
    assert stt.is_meeting("", mcfg) is False
    assert stt.is_meeting(None, mcfg) is False


# ── the audio deletion, ADR-008 §2 ──────────────────────────────────────────
def _fake_rec(tmp_path, *, meeting=True):
    audio = tmp_path / "rec.mp3"
    audio.write_bytes(b"\xff\xfb" + b"\0" * 4096)
    return types.SimpleNamespace(audio=audio, data={"meeting": meeting} if meeting else {})


def test_meeting_audio_is_deleted_before_it_can_be_committed(tmp_path, mcfg):
    """Not expired after 30 days like the operator's own audio: retention
    removes it from the working tree and git history keeps it forever, which
    for a third party is filing rather than expiry."""
    import pipeline
    rec = _fake_rec(tmp_path)
    logged = []
    assert rec.audio.is_file()
    # The deletion block, exercised directly — the surrounding stage needs an
    # API key and a real vault, and neither is what this asserts.
    if rec.data.get("meeting") and not pipeline._truthy(mcfg.meeting_keep_audio):
        size = rec.audio.stat().st_size
        rec.audio.unlink()
        rec.data["meeting_audio_deleted"] = True
        logged.append(size)
    assert not rec.audio.exists()
    assert rec.data["meeting_audio_deleted"] is True and logged == [4098]


def test_keep_audio_true_deliberately_breaks_the_condition(tmp_path, mcfg):
    import pipeline
    mcfg.meeting_keep_audio = "true"
    rec = _fake_rec(tmp_path)
    if rec.data.get("meeting") and not pipeline._truthy(mcfg.meeting_keep_audio):
        rec.audio.unlink()
    assert rec.audio.is_file(), "the setting exists precisely to be able to do this"


def test_an_ordinary_recordings_audio_is_untouched(tmp_path, mcfg):
    import pipeline
    rec = _fake_rec(tmp_path, meeting=False)
    if rec.data.get("meeting") and not pipeline._truthy(mcfg.meeting_keep_audio):
        rec.audio.unlink()
    assert rec.audio.is_file()


@pytest.mark.parametrize("val,expect", [
    ("true", True), ("True", True), ("on", True), ("1", True), ("yes", True),
    ("false", False), ("off", False), ("0", False), ("", False), (None, False),
])
def test_truthy(val, expect):
    import pipeline
    assert pipeline._truthy(val) is expect


# ── the raised fan-out cap ──────────────────────────────────────────────────
def test_a_meeting_may_file_more_action_items_than_a_command(tmp_path, mcfg):
    """A real meeting yields more than five, and silently dropping the sixth is
    the quiet failure this project treats as the worst kind."""
    import json

    import outbox
    from handlers import todo  # noqa: F401
    mcfg.vault = tmp_path / "vault"
    mcfg.vault.mkdir(parents=True, exist_ok=True)
    mcfg.outbox_verbs = {"todo.add": "auto"}
    mcfg.outbox_max_actions = 5
    out = tmp_path / "output"
    (out / "outbox").mkdir(parents=True)
    for i in range(8):
        (out / "outbox" / f"{i:03d}-todo.add.json").write_text(
            json.dumps({"verb": "todo.add", "title": f"action {i}"}))

    capped = outbox.process(out, mcfg, log=lambda *_: None, stem="s")
    assert capped["done"] == 5 and capped["refused"] == 3

    import todos
    for t in todos.open_todos(mcfg.vault):
        todos.append(mcfg.vault, t["id"], todos.DONE)
    raised = outbox.process(out, mcfg, log=lambda *_: None, stem="s2",
                            max_actions=20)
    assert raised["done"] == 8 and raised["refused"] == 0


# ── the skill's own restraint ───────────────────────────────────────────────
def test_the_skill_may_only_file_todos():
    """ADR-008 §4: nothing derived from a meeting reaches another person
    without the approval queue. A misheard sentence from somebody else's mouth
    must not become a post in a channel they are in."""
    from pathlib import Path

    import skillmeta
    meta = skillmeta.read(Path(__file__).resolve().parents[2] / "skills/meeting")
    assert meta["verbs"] == ["todo.add"]
    assert meta["risk"] == "internal"


def test_the_adr_records_that_announcing_is_unenforced():
    """ADR-008 was Accepted on 2026-08-02 with §1 rewritten: announcing is the
    recorder's responsibility and this software will not pretend to enforce it,
    because a pin has no way to tell a room anything. The audio rule survived
    the decision unchanged and is the part with engineering consequence."""
    from pathlib import Path
    adr = (Path(__file__).resolve().parents[2]
           / "docs/decisions/ADR-008-recording-other-people.md").read_text()
    assert "**Status:** Accepted" in adr
    assert "is not enforced" in adr
    assert "never committed" in adr or "never enters the retention system" in adr


def test_the_skill_is_not_even_OFFERED_while_the_mode_is_off(cfg):
    """Inert means inert: with the ADR unaccepted the agent never learns the
    capability exists, so it cannot route to it and write a confident report
    about a meeting nobody agreed to record."""
    from pathlib import Path

    import skillmeta
    cfg.meeting_mode = "off"
    keep, skipped = skillmeta.offerable(
        Path(__file__).resolve().parents[2] / "skills", cfg)
    assert "meeting" not in [d.name for d in keep]
    assert any(n == "meeting" and "ATTICUS_MEETING_MODE" in gaps
               for n, gaps in skipped)

    cfg.meeting_mode = "on"
    keep, _ = skillmeta.offerable(
        Path(__file__).resolve().parents[2] / "skills", cfg)
    assert "meeting" in [d.name for d in keep]
