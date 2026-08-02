"""Skill metadata (#89): the declarations and the handlers must not drift.

The bug this exists to prevent happened on 2026-08-02. `github.close` shipped
with the verb implemented, the skill body documenting it, and the frontmatter
description still saying "Do NOT use it to … close anything". The agent read
the description, obeyed it, and refused — correctly. **Routing reads the
description; a capability the description denies does not exist.**

One hand-written test now guards that specific sentence. These guard the shape
structurally, in both directions, for every skill and every verb.
"""
import sys
from pathlib import Path

import outbox
import pytest
import skillmeta
from config import Config

REPO = Path(__file__).resolve().parents[2]
SKILLS = sorted(p for p in (REPO / "skills").iterdir()
                if p.is_dir() and (p / "SKILL.md").is_file())


@pytest.fixture(autouse=True, scope="module")
def _handlers_imported():
    """A verb exists only if handlers/__init__ imports its module."""
    sys.path.insert(0, str(REPO / "processor"))
    import handlers  # noqa: F401


def _meta(d):
    return skillmeta.read(d)


# ── the parser ──────────────────────────────────────────────────────────────
def test_parse_reads_lists_scalars_and_ignores_prose():
    meta = skillmeta.parse(
        "---\n"
        "name: demo\n"
        "description: |\n"
        "  A long prose block that mentions verbs: [not, real] on purpose.\n"
        "  requires: ATTICUS_NOT_REAL\n"
        "verbs: [a.one, a.two]\n"
        "requires: [ATTICUS_X]\n"
        "risk: tracked\n"
        "cost: low   # a trailing comment\n"
        "---\n\n# body\nverbs: [also, not, real]\n")
    assert meta["name"] == "demo"
    assert meta["verbs"] == ["a.one", "a.two"]
    assert meta["requires"] == ["ATTICUS_X"]
    assert meta["risk"] == "tracked"
    assert meta["cost"] == "low", "a trailing YAML comment must not become part of the value"


def test_a_malformed_block_fails_OPEN_rather_than_hiding_a_skill():
    """This parser feeds a filter that can hide a capability. A parser that
    threw on a stray character would disable a working skill over a typo."""
    assert skillmeta.parse("no frontmatter at all") == {}
    assert skillmeta.parse("---\n: : :\nverbs: [a.one]\n---\n")["verbs"] == ["a.one"]


def test_an_empty_verbs_list_is_not_a_missing_key():
    meta = skillmeta.parse("---\nname: d\nverbs: []\n---\n")
    assert meta["verbs"] == []


# ── every skill declares itself ─────────────────────────────────────────────
@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.name)
def test_every_skill_declares_the_metadata_block(skill):
    meta = _meta(skill)
    assert meta.get("name") == skill.name, "name must match the directory"
    assert "verbs" in meta, "declare verbs, even as an empty list"
    assert meta.get("risk") in ("internal", "tracked", "outward")
    assert meta.get("cost") in ("low", "medium", "high")
    assert meta.get("outputs"), "declare what the skill produces"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.name)
def test_declared_verbs_are_registered_by_a_handler(skill):
    known = set(outbox.known_verbs())
    for verb in _meta(skill).get("verbs") or []:
        assert verb in known, (
            f"{skill.name} declares {verb!r}, which no handler registers — "
            f"the agent would write an intent file that is refused as unknown")


def test_every_registered_verb_is_declared_by_at_least_one_skill():
    """The other direction, which is the one that bit us: a verb can exist,
    work, and be unreachable because no skill tells the agent about it.

    At least one, not exactly one. Sharing is legitimate and real — `meeting`
    files the operator's action items with `todo.add`, the same verb the `todo`
    skill owns. What must never happen is a verb no skill mentions, because
    then nothing routes to it however well it works.
    """
    declared: dict[str, list[str]] = {}
    for d in SKILLS:
        for verb in _meta(d).get("verbs") or []:
            declared.setdefault(verb, []).append(d.name)
    for verb in outbox.known_verbs():
        assert declared.get(verb), f"{verb} is registered but no skill declares it"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.name)
def test_the_declared_risk_matches_the_riskiest_verb(skill):
    order = {"internal": 0, "tracked": 1, "outward": 2}
    verbs = _meta(skill).get("verbs") or []
    if not verbs:
        return
    highest = max((outbox.handler_for(v)["risk"] for v in verbs
                   if outbox.handler_for(v)), key=lambda r: order[r])
    assert _meta(skill)["risk"] == highest, (
        f"{skill.name} declares risk={_meta(skill)['risk']} but its verbs are "
        f"{highest} — the frontmatter is what an operator reads before enabling it")


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.name)
def test_the_description_does_not_prohibit_a_verb_the_skill_declares(skill):
    """THE regression test for 2026-08-02.

    A description saying "Do NOT use it to close anything" beside a declared
    `github.close` is not a documentation nit — the agent obeys the description
    and the verb becomes dead code.
    """
    text = (skill / "SKILL.md").read_text()
    front = text.split("---")[1].lower()
    # The action word of each declared verb: github.close -> "close".
    for verb in _meta(skill).get("verbs") or []:
        action = verb.split(".", 1)[1].replace("_", " ")
        for phrase in (f"do not use it to {action}", f"do not use this to {action}",
                       f"not use it to {action}"):
            assert phrase not in front, (
                f"{skill.name} declares {verb} and its description forbids "
                f"{action!r}")


# ── the routing filter ──────────────────────────────────────────────────────
def test_a_skill_with_unmet_requirements_is_not_offered(cfg):
    """A fresh install has every credential blank. Offering a skill that cannot
    work makes the agent write a confident report about a refused action."""
    cfg.slack_bot_token = ""
    cfg.slack_channels = []
    keep, skipped = skillmeta.offerable(REPO / "skills", cfg)
    names = [d.name for d in keep]
    assert "slack" not in names
    assert any(n == "slack" and "ATTICUS_SLACK_BOT_TOKEN" in gaps
               for n, gaps in skipped), "the skip must name what is missing"


def test_a_configured_skill_is_offered(cfg):
    cfg.slack_bot_token = "xoxb-test"
    cfg.slack_channels = ["test-atticus"]
    keep, _ = skillmeta.offerable(REPO / "skills", cfg)
    assert "slack" in [d.name for d in keep]


def test_a_skill_that_requires_nothing_is_always_offered(cfg):
    keep, _ = skillmeta.offerable(REPO / "skills", cfg)
    names = [d.name for d in keep]
    for always in ("todo", "reminders", "deep-research"):
        assert always in names, f"{always} needs no credential and must always be offered"


def test_requirements_map_env_names_to_config_attributes(cfg):
    """ATTICUS_GITHUB_REPOS -> cfg.github_repos, the convention config.py
    follows without exception. A typo'd requires: silently hides a skill, so
    this pins the mapping rather than trusting it."""
    cfg.github_repos = []
    assert skillmeta.missing_requirements(
        {"requires": ["ATTICUS_GITHUB_REPOS"]}, cfg) == ["ATTICUS_GITHUB_REPOS"]
    cfg.github_repos = ["beekeeper-lab/atticus"]
    assert skillmeta.missing_requirements(
        {"requires": ["ATTICUS_GITHUB_REPOS"]}, cfg) == []


def test_every_declared_requirement_names_a_real_config_attribute():
    """A `requires:` naming a setting that does not exist would hide the skill
    forever, and look like the credential was never set."""
    c = Config(env_file=REPO / "ops/.env.example")
    for d in SKILLS:
        for env in _meta(d).get("requires") or []:
            attr = env.lower().removeprefix("atticus_")
            assert hasattr(c, attr), (
                f"{d.name} requires {env}, but config.py has no {attr}")
