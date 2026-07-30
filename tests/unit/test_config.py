"""The real Config, and the contract between it and ops/.env.example.

Nothing instantiated Config before this file: every test used a hand-written
SimpleNamespace mirroring its defaults. That mirror drifted twice — an adjudicator
threshold stuck at 50 after the shipped default became 75, and wake_aliases
carrying three aliases production does not ship, which made the gate tests assert
a wider gate than the one that runs.

The dead-knob test below is the one that matters most: ATTICUS_BACKLOG_ALARM_MINUTES
was documented in ops/.env, ops/.env.example AND docs/configuration.md while being
read by no code at all, so the operator believed an alarm was armed that did not
exist. A documented setting nothing reads is worse than an undocumented one.
"""
import re
from pathlib import Path

import pytest
from config import Config

REPO = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO / "ops/.env.example"

# Names that are not settings, so neither direction of the contract applies.
_ELSEWHERE = {
    "ATTICUS_FIXED_NONCE",   # the vault site's build script, not this repo
    # SET for the agent subprocess rather than read as configuration — it tells
    # the agent where its output directory is.
    "ATTICUS_OUTPUT_DIR",
    # A prefix used in a startswith() filter in gen-config-docs.py, not a var.
    "PLAUD_EMBEDDED",
}
_KNOWN_INERT = {
    # Commented-out placeholders for the contingent W8 iOS work.
    "PLAUD_EMBEDDED_CLIENT_SECRET", "PLAUD_EMBEDDED_API_KEY",
}


def _documented() -> set[str]:
    names = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        st = line.strip()
        if not st or st.startswith("#") or "=" not in st:
            continue
        names.add(st.split("=", 1)[0].strip())
    return names - _KNOWN_INERT


def _read_by_code() -> set[str]:
    """Every ATTICUS_/PLAUD_ name any Python file in the repo actually reads."""
    found = set()
    for py in list(REPO.glob("processor/*.py")) + list(REPO.glob("ingest/*.py")) \
            + list(REPO.glob("ops/*.py")) + [REPO / "atticus_cli.py"]:
        found |= set(re.findall(r'["\']((?:ATTICUS|PLAUD)_[A-Z_]+)["\']', py.read_text()))
    return found


def test_no_documented_setting_is_dead():
    """Every knob in .env.example must be read by code."""
    dead = _documented() - _read_by_code() - _ELSEWHERE
    assert not dead, (
        f"documented but read by nothing: {sorted(dead)} — either wire it up or "
        f"delete it from ops/.env.example")


def test_no_setting_is_undocumented():
    """And every knob the code reads must be documented, or it is invisible.

    _KNOWN_INERT is subtracted from BOTH directions, not just the documented one:
    those names are commented-out placeholders for the contingent W8 iOS work, and
    redact.py names them so it can scrub their values if they ever get set. A name
    that is not an active setting should not be demanded in either direction.
    """
    undocumented = _read_by_code() - _documented() - _ELSEWHERE - _KNOWN_INERT
    assert not undocumented, (
        f"read by code but absent from ops/.env.example: {sorted(undocumented)} "
        f"— add it, or docs/configuration.md will never mention it")


def test_config_parses_the_shipped_example():
    """.env.example must be loadable. It is what a real deployment starts from,
    and it is the basis of the `cfg` test fixture."""
    c = Config(env_file=ENV_EXAMPLE)
    assert c.stt_model
    assert c.max_command_seconds > 0


def test_the_shipped_example_is_configured_safely():
    """Spot-check the settings whose defaults are load-bearing for safety."""
    c = Config(env_file=ENV_EXAMPLE)
    assert c.wake_phrase, "the shipped example must configure a wake phrase"
    assert c.sandbox is True, "the shipped example must enable the sandbox"
    assert c.wake_adjudicator_threshold >= 70, (
        "the adjudicator widens the one control between ambient speech and an "
        "autonomous agent; a bare majority is not enough")
    assert c.wake_aliases == [], (
        "aliases are a deterministic escape hatch and must ship empty")
    assert c.max_output_files > 0 and c.max_output_bytes > 0


def test_the_bare_default_is_fail_open_and_that_is_deliberate():
    """Documented explicitly, because it is surprising and it is real.

    With no configuration at all the wake gate is DISABLED — sanity_check passes
    everything. ops/.env.example sets a phrase, and pipeline.main() now warns at
    startup when it is empty, but the code default itself does not fail closed.
    If that ever changes, this test should be the thing that notices.
    """
    c = Config(env_file=REPO / "ops/.does-not-exist.env")
    assert c.wake_phrase == ""


@pytest.mark.parametrize("raw,expected", [
    ('ATTICUS_WAKE_PHRASE=atticus', "atticus"),
    ('ATTICUS_WAKE_PHRASE="atticus"', "atticus"),
    ("ATTICUS_WAKE_PHRASE='atticus'", "atticus"),
    ('ATTICUS_WAKE_PHRASE=  atticus  ', "atticus"),
])
def test_env_parsing_handles_quoting_and_whitespace(tmp_path, raw, expected):
    """_parse_env's quote handling had no coverage, and the ""-vs-unset rule it
    implements was already broken once (see the comment in config.py)."""
    f = tmp_path / "e.env"
    f.write_text(raw + "\n")
    assert Config(env_file=f).wake_phrase == expected


def test_an_empty_value_is_preserved_not_treated_as_unset(tmp_path):
    """ATTICUS_SKILLS_DIR ships BLANK, and collapsing "" into the default would
    turn Path("") into the current working directory."""
    f = tmp_path / "e.env"
    f.write_text("ATTICUS_WAKE_PHRASE=\n")
    assert Config(env_file=f).wake_phrase == ""


def test_redacted_never_leaks_a_credential():
    c = Config(env_file=ENV_EXAMPLE)
    blob = repr(c.redacted()).lower()
    for forbidden in ("sk-", "openai_api_key", "ntfy.sh", "token"):
        assert forbidden not in blob, f"redacted() leaked {forbidden!r}"
