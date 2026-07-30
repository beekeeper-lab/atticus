"""Shared fixtures. No test here touches a real network, vault, or API."""
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "processor"), str(REPO / "ingest"), str(REPO)]


@pytest.fixture(autouse=True)
def _scrubbed_env(monkeypatch):
    """No ATTICUS_/PLAUD_ variable from the developer's shell may reach a test.

    Autouse, so it cannot be forgotten. Without it, a real Config built below
    would silently inherit this host's settings and the suite would pass or fail
    depending on whose machine it ran on.
    """
    for k in list(os.environ):
        if k.startswith(("ATTICUS_", "PLAUD_")):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def cfg(tmp_path):
    """The REAL Config, built against no env file, with test overrides applied.

    This used to be a hand-written SimpleNamespace mirroring config.py's
    defaults, and the mirror drifted twice — both times invisibly and both times
    in the direction that weakened a test:

      * `wake_adjudicator_threshold` stayed 50 after the shipped default became
        75, so every adjudicator test measured a gate nobody runs.
      * `wake_aliases` carried the three observed mishearings while the shipped
        default is empty, which made test_gate.py assert the gate OPENS for
        "Advocates…" — strictly wider than reality, in the most safety-critical
        test file in the repo.

    Deriving from Config makes both impossible: a renamed attribute raises here,
    and a default can no longer disagree with itself.

    It is built from **ops/.env.example**, not from an absent env file, and that
    distinction matters. Several of config.py's own fallbacks are deliberately
    fail-open for a bare process — `ATTICUS_WAKE_PHRASE` defaults to `""`, which
    disables the gate entirely — while `.env.example` is the configuration a real
    deployment actually starts from and is tracked in git. Testing against it
    means the suite measures a correctly-configured Atticus, and any drift
    between `.env.example` and `config.py` shows up here rather than in
    production. The fail-open default gets its own explicit tests instead.

    Tests override by assignment — `cfg.wake_phrase = ""` — exactly as before.
    """
    from config import Config
    c = Config(env_file=REPO / "ops/.env.example")
    # Only what a test genuinely must not inherit from the real defaults: a
    # scratch vault, no live network, no real key, fast timeouts.
    c.vault = tmp_path / "vault"
    # openai_key is a lazy PROPERTY that reads the environment and then
    # ~/.config/ai/env, and raises if it finds nothing. Priming the private cache
    # keeps the real property logic intact — including its "must start with sk-"
    # check, which a plain attribute override would have bypassed — while
    # guaranteeing the operator's actual key is never loaded by a test.
    c._openai_key = "sk-test"
    c.stt_url = "http://127.0.0.1:1/none"
    c.stt_model = "m"
    c.stt_prompt = "p"
    c.stt_timeout = 1
    c.exec_timeout = 60
    c.push_retries = 1
    c.git_name, c.git_email = "t", "t@t"
    c.notify_url = None
    c.result_notify_url = None
    c.site_base_url = ""
    return c


@pytest.fixture
def partial_cfg(tmp_path):
    """A deliberately incomplete config, for testing getattr() fallbacks.

    A few call sites read settings with a default because they must tolerate a
    caller that predates the setting. Those paths need an object that really is
    missing the attribute, which the real Config never is.
    """
    return types.SimpleNamespace(vault=tmp_path / "vault", wake_phrase="atticus")


@pytest.fixture
def git_vault(tmp_path):
    """A real git repo laid out like a vault, with a bare 'remote'."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   check=True, capture_output=True)
    work = tmp_path / "vault"
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    for d in ("inbox", "processed", "failures", ".state"):
        (work / d).mkdir(parents=True)
        (work / d / ".gitkeep").touch()
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(work), "config", k, v], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "-u", "origin", "main"],
                   check=True, capture_output=True)
    return types.SimpleNamespace(work=work, remote=remote)


def write_record(vault: Path, stem="2026-07-29T120000Z_abc123", **over):
    d = vault / "inbox/2026/07"
    d.mkdir(parents=True, exist_ok=True)
    data = {"plaud_id": "abc123", "recorded_at": "2026-07-29T12:00:00Z",
            "audio_filename": f"{stem}.mp3", "status": "raw",
            "duration_seconds": 12, "attempts": 0}
    data.update(over)
    (d / f"{stem}.json").write_text(json.dumps(data, indent=2))
    (d / f"{stem}.mp3").write_bytes(b"\xff\xfb\x90\x00" + b"\0" * 512)
    return d / f"{stem}.json"
