"""First tests for the ingest half, which had none.

Not a single test imported poller.py or plaud_web.py, so the side the design
calls "must be always-on" was entirely unverified — including the session-dead
alarm the docs call the one failure that must never be quiet, and safe_id(),
which is the only thing stopping a hostile upstream id from escaping inbox/.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


poller = _load("atticus_poller", "ingest/poller.py")


# --- safe_id: the path-traversal guard ------------------------------------

@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "a/b/c",
    "x\x00y",
    "abc; rm -rf /",
])
def test_safe_id_strips_everything_dangerous(bad):
    """The id is spliced into the audio path, the .json path and the fetcher's
    -o argument. Only [A-Za-z0-9] may survive."""
    out = poller.safe_id({"id": bad})
    assert out.isalnum()
    assert "/" not in out and ".." not in out


@pytest.mark.parametrize("hostile", ["../../", "..", "///", "", "!!!"])
def test_safe_id_rejects_an_id_with_nothing_usable(hostile):
    """Rejecting loudly is right: silently inventing a stem would file the
    recording under a name the ledger cannot match."""
    with pytest.raises(poller.FetcherError):
        poller.safe_id({"id": hostile})


def test_safe_id_is_truncated_to_twelve_chars():
    assert poller.safe_id({"id": "a" * 40}) == "a" * 12


# --- the fetcher contract: malformed stdout -------------------------------

class _Fetcher(poller.Fetcher):
    """A Fetcher whose subprocess layer is replaced by canned stdout."""

    def __init__(self, out):
        super().__init__(Path(__file__))       # a path that exists
        self._out = out

    def _run(self, *args, timeout=None):
        return self._out


def test_non_json_fetcher_output_becomes_a_FetcherError():
    """json.loads raised JSONDecodeError OUTSIDE the FetcherError handler, so a
    fetcher printing a stray warning produced a traceback and exit 1 instead of
    the documented partial-failure path."""
    with pytest.raises(poller.FetcherError) as e:
        _Fetcher("Traceback (most recent call last): boom").list(2)
    assert e.value.code == poller.F_CHANGED


def test_a_bare_json_list_is_accepted_not_an_AttributeError():
    """`data.get(...)` was evaluated before the isinstance() fallback could help,
    so a top-level JSON array raised AttributeError and killed the pass."""
    recs = _Fetcher('[{"id": "abc", "created_at": "2026-07-29T00:00:00Z"}]').list(2)
    assert [r["id"] for r in recs] == ["abc"]


def test_a_json_scalar_is_refused_clearly():
    with pytest.raises(poller.FetcherError):
        _Fetcher("42").list(2)


def test_recordings_of_the_wrong_type_is_refused():
    with pytest.raises(poller.FetcherError):
        _Fetcher('{"recordings": "not-a-list"}').list(2)


def test_a_record_missing_required_fields_is_refused():
    with pytest.raises(poller.FetcherError):
        _Fetcher('{"recordings": [{"id": "abc"}]}').list(2)


# --- the dirty-tree sweep -------------------------------------------------

def test_sweep_commits_work_stranded_by_an_interrupted_pass(tmp_path):
    """A crash between append_seen() and commit_push() left the id in the local
    ledger, so `fresh` was empty forever after and the pass returned EXIT_OK
    without ever committing. The audio was invisible downstream until some future
    recording's `add -A` happened to sweep it in — days, given bursty arrivals."""
    from vault import Git

    vault = tmp_path / "vault"
    (vault / "inbox").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    subprocess.run(["git", "-C", str(vault), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(vault), "config", "user.name", "t"], check=True)

    # Exactly the stranded state: audio + metadata on disk, never committed.
    (vault / "inbox" / "stranded.mp3").write_bytes(b"\xff\xfb\x90\x00")
    (vault / "inbox" / "stranded.json").write_text(json.dumps({"status": "raw"}))

    git = Git(vault, "t", "t@t", 1, log=lambda m: None)
    cfg = type("C", (), {"notify_url": None, "alarm_throttle_hours": 6})()
    poller._sweep_dirty(git, lambda m: None, cfg)

    tracked = subprocess.run(["git", "-C", str(vault), "ls-files"],
                             capture_output=True, text=True).stdout
    assert "stranded.mp3" in tracked
    assert "stranded.json" in tracked
