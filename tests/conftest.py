"""Shared fixtures. No test here touches a real network, vault, or API."""
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "processor"), str(REPO / "ingest"), str(REPO)]


@pytest.fixture
def cfg(tmp_path):
    """A config object with the real defaults, nothing live behind it."""
    return types.SimpleNamespace(
        vault=tmp_path / "vault",
        wake_phrase="atticus",
        wake_aliases=["advocates", "abacus", "artemis"],
        min_words=3,
        max_command_chars=600,
        max_command_sentences=6,
        max_command_seconds=180,
        max_ingest_seconds=7200,
        max_budget_usd="2.00",
        sandbox=True,
        claude_bin="claude",
        claude_model=None,
        exec_timeout=60,
        skills_dir=REPO / "skills",
        notify_url=None,
        result_notify_url=None,
        site_base_url="",
        notify_notes=False,
        alarm_throttle_hours=6,
        git_name="t", git_email="t@t", push_retries=1,
        stt_url="http://127.0.0.1:1/none", stt_model="m", stt_prompt="p",
        stt_timeout=1, log_level="INFO",
    )


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
