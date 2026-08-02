"""Named projects and artifact versions (#84, #88).

The properties worth pinning:

  * a project is found by NAME or ALIAS in the transcript, and two projects
    named at once refuse rather than picking — filing work in the wrong project
    is discovered only by not finding it later;
  * most recordings belong to no project, so "no match" must be silent and
    cheap, never an error;
  * the context block is FENCED and labelled reference-only. It mixes
    operator-written prose with agent-written artifact titles, and a previous
    run may have ingested a hostile page — so the whole thing is data;
  * context goes BEFORE the preamble, so the output contract and the
    act-on-the-first-request rule stay the last framing before the transcript;
  * versions accumulate on the project ARTIFACT, never on the recording, which
    is immutable.
"""
import json

import execute as ex
import projects
import pytest


@pytest.fixture
def pcfg(cfg, tmp_path):
    cfg.vault = tmp_path / "vault"
    cfg.vault.mkdir(parents=True, exist_ok=True)
    cfg.project_context_chars = 2000
    return cfg


def _make(pcfg, name, *, aliases=(), brief="Some context about the work."):
    return projects.create(pcfg.vault, name, aliases=aliases, brief=brief)


# ── creation and loading ────────────────────────────────────────────────────
def test_create_then_load(pcfg):
    p = _make(pcfg, "Consulting Practice", aliases=["consulting"])
    assert p["slug"] == "consulting-practice"
    assert (p["dir"] / "brief.md").is_file()
    assert [x["slug"] for x in projects.load(pcfg.vault)] == ["consulting-practice"]


def test_creating_the_same_project_twice_refuses(pcfg):
    _make(pcfg, "Consulting")
    with pytest.raises(projects.ProjectError, match="already exists"):
        _make(pcfg, "Consulting")


def test_an_unusable_name_refuses(pcfg):
    with pytest.raises(projects.ProjectError, match="usable"):
        projects.create(pcfg.vault, "!!!")


def test_a_malformed_index_does_not_take_the_feature_down(pcfg):
    p = _make(pcfg, "Consulting")
    (p["dir"] / "index.json").write_text("{ not json")
    loaded = projects.load(pcfg.vault)
    assert len(loaded) == 1 and loaded[0]["slug"] == "consulting"


def test_no_projects_directory_is_not_an_error(pcfg):
    assert projects.load(pcfg.vault) == []


# ── resolution ──────────────────────────────────────────────────────────────
def test_resolves_by_name_and_by_alias(pcfg):
    _make(pcfg, "Consulting Practice", aliases=["the consulting work"])
    assert projects.resolve_from_text(
        pcfg.vault, "Atticus, add this to the consulting practice")["slug"] \
        == "consulting-practice"
    assert projects.resolve_from_text(
        pcfg.vault, "more on the consulting work please")["slug"] \
        == "consulting-practice"


def test_a_recording_that_names_no_project_is_the_normal_case(pcfg):
    _make(pcfg, "Consulting")
    assert projects.resolve_from_text(pcfg.vault, "research the best mattress") is None


def test_two_projects_in_one_sentence_refuse_rather_than_pick(pcfg):
    _make(pcfg, "Consulting")
    _make(pcfg, "Homelab")
    with pytest.raises(projects.ProjectError, match="more than one"):
        projects.resolve_from_text(pcfg.vault, "add this to consulting and homelab")


def test_resolution_needs_a_word_boundary(pcfg):
    """A three-letter slug must not match inside another word."""
    _make(pcfg, "DDI")
    assert projects.resolve_from_text(pcfg.vault, "the ddication was odd") is None
    assert projects.resolve_from_text(pcfg.vault, "file it under ddi, please")


# ── the context block ───────────────────────────────────────────────────────
def test_the_context_block_is_fenced_and_labelled_reference(pcfg):
    p = _make(pcfg, "Consulting", brief="Aim: $50k in four months.")
    block = projects.context_block(p)
    assert "BEGIN PROJECT CONTEXT" in block and "END PROJECT CONTEXT" in block
    assert "not an instruction" in block
    assert "Aim: $50k in four months." in block


def test_the_brief_is_capped(pcfg):
    p = _make(pcfg, "Consulting", brief="line\n" * 5000)
    block = projects.context_block(p, cap=200)
    assert "[brief truncated]" in block
    assert len(block) < 1200


def test_artifacts_are_listed_newest_first(pcfg, tmp_path):
    p = _make(pcfg, "Consulting")
    src = tmp_path / "r.html"
    for i in (1, 2):
        src.write_text(f"<title>Report {i}</title>")
        projects.link_artifact(pcfg.vault, p["slug"], source=src,
                               title=f"Report {i}", stem=f"rec{i}")
    block = projects.context_block(projects.get(pcfg.vault, p["slug"]))
    assert block.index("Report 2") < block.index("Report 1")


def test_context_goes_BEFORE_the_preamble(pcfg):
    """The output contract and 'act only on the first request' must be the last
    framing the model reads before the transcript."""
    p = _make(pcfg, "Consulting")
    task = ex.build_task("do the thing", project_context=projects.context_block(p))
    assert task.index("PROJECT CONTEXT") < task.index("Output contract")
    assert task.index("Output contract") < task.index("do the thing")


def test_no_project_leaves_the_prompt_exactly_as_it_was(pcfg):
    assert ex.build_task("do the thing") == ex.build_task("do the thing", "")


# ── versioning ──────────────────────────────────────────────────────────────
def test_linking_produces_v1_then_v2_for_a_revision(pcfg, tmp_path):
    p = _make(pcfg, "Consulting")
    src = tmp_path / "r.html"
    src.write_text("<title>Plan</title>first")
    a = projects.link_artifact(pcfg.vault, p["slug"], source=src,
                               title="Consulting Plan", stem="rec1")
    assert a["version"] == 1
    src.write_text("<title>Plan</title>second")
    b = projects.link_artifact(pcfg.vault, p["slug"], source=src,
                               title="Consulting Plan v2", stem="rec2",
                               revises=a["artifact"])
    assert b["version"] == 2 and b["artifact"] == a["artifact"]
    d = p["dir"] / "artifacts" / a["artifact"]
    assert {f.name for f in d.iterdir()} == {"v1.html", "v2.html"}
    assert (d / "v1.html").read_text().endswith("first"), "v1 is not overwritten"


def test_a_new_title_without_revises_is_a_new_artifact(pcfg, tmp_path):
    p = _make(pcfg, "Consulting")
    src = tmp_path / "r.html"
    src.write_text("x")
    projects.link_artifact(pcfg.vault, p["slug"], source=src, title="One", stem="r1")
    projects.link_artifact(pcfg.vault, p["slug"], source=src, title="Two", stem="r2")
    meta = json.loads((p["dir"] / "index.json").read_text())
    assert {a["name"] for a in meta["artifacts"]} == {"one", "two"}


def test_the_recordings_own_copy_is_never_touched(pcfg, tmp_path):
    """A recording is immutable: it is a thing that was said at a time."""
    p = _make(pcfg, "Consulting")
    src = tmp_path / "processed-copy.html"
    src.write_text("original")
    projects.link_artifact(pcfg.vault, p["slug"], source=src, title="X", stem="r1")
    assert src.read_text() == "original" and src.is_file()


def test_linking_into_a_missing_project_refuses(pcfg, tmp_path):
    src = tmp_path / "r.html"
    src.write_text("x")
    with pytest.raises(projects.ProjectError, match="no project"):
        projects.link_artifact(pcfg.vault, "nope", source=src, title="X", stem="r1")


def test_linking_a_missing_file_refuses(pcfg, tmp_path):
    p = _make(pcfg, "Consulting")
    with pytest.raises(projects.ProjectError, match="nothing to link"):
        projects.link_artifact(pcfg.vault, p["slug"], source=tmp_path / "gone.html",
                               title="X", stem="r1")
