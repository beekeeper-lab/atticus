"""Vault access: records, status transitions, and safe git.

The status field in each recording's metadata JSON *is* the pipeline state.
Every stage advances it and commits, so a crash leaves a known state and the
next run resumes rather than redoing work.

Two hosts push to this repo (WarDog writes inbox/, Forge writes processed/),
so every push is pull-rebase-retry. See SPEC §4.3.
"""
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

RAW, TRANSCRIBED, ROUTED, EXECUTED, PUBLISHED, FAILED = (
    "raw", "transcribed", "routed", "executed", "published", "failed"
)


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_atomic(path: Path, data: str | bytes):
    """Write via temp + rename so a crash can never leave a half-file that
    the next run mistakes for complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, mode) as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class Record:
    meta_path: Path
    data: dict

    # ---- identity -------------------------------------------------------
    @property
    def id(self) -> str:
        return self.data["plaud_id"]

    @property
    def stem(self) -> str:
        """e.g. 2026-07-28T142211Z_abc123 — shared by every artifact."""
        return self.meta_path.stem

    @property
    def status(self) -> str:
        return self.data.get("status", RAW)

    @property
    def ym(self) -> str:
        return "/".join(self.meta_path.parts[-3:-1])  # YYYY/MM

    # ---- artifact paths -------------------------------------------------
    @property
    def audio(self) -> Path:
        return self.meta_path.parent / self.data["audio_filename"]

    def _processed(self, vault: Path) -> Path:
        return vault / "processed" / self.ym

    def transcript_path(self, vault): return self._processed(vault) / f"{self.stem}.transcript.txt"
    def task_path(self, vault):       return self._processed(vault) / f"{self.stem}.task.md"
    def outdir(self, vault):          return self._processed(vault) / self.stem
    def error_path(self, vault):      return vault / "failures" / self.ym / f"{self.stem}.error.json"

    # ---- transitions ----------------------------------------------------
    def advance(self, status: str, **fields):
        self.data["status"] = status
        self.data[f"{status}_at"] = utcnow()
        self.data.update(fields)
        self.save()

    def fail(self, vault: Path, stage: str, error: str, retryable: bool):
        self.data["status"] = FAILED
        self.data["failed_at"] = utcnow()
        self.data["failed_stage"] = stage
        self.data["attempts"] = self.data.get("attempts", 0) + 1
        self.save()
        write_atomic(self.error_path(vault), json.dumps({
            "plaud_id": self.id, "stage": stage, "error": error,
            "retryable": retryable, "attempts": self.data["attempts"],
            "at": utcnow(),
        }, indent=2) + "\n")

    def save(self):
        write_atomic(self.meta_path, json.dumps(self.data, indent=2) + "\n")


def load_records(vault: Path, status: str | None = None) -> list[Record]:
    inbox = vault / "inbox"
    if not inbox.is_dir():
        return []
    out = []
    for p in sorted(inbox.rglob("*.json")):
        try:
            rec = Record(p, json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
        if status is None or rec.status == status:
            out.append(rec)
    out.sort(key=lambda r: r.data.get("recorded_at", ""))
    return out


# ---------------------------------------------------------------------------
#  git
# ---------------------------------------------------------------------------

class Git:
    def __init__(self, vault: Path, name: str, email: str, retries: int = 3):
        self.vault, self.retries = vault, retries
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
        }

    def _run(self, *args, check=True):
        return subprocess.run(
            ["git", "-C", str(self.vault), *args],
            capture_output=True, text=True, check=check, env=self.env,
        )

    def is_repo(self) -> bool:
        return (self.vault / ".git").exists()

    def has_remote(self) -> bool:
        return bool(self._run("remote", check=False).stdout.strip())

    def pull(self):
        if not (self.is_repo() and self.has_remote()):
            return
        self._run("pull", "--rebase", "--autostash", check=False)

    def commit_push(self, message: str) -> bool:
        """Stage everything, commit, push with bounded retry.

        Disjoint paths between hosts mean rebases nearly always apply cleanly;
        the retry is for the rare interleave, not a busy-wait. Returns False
        rather than raising so the caller can quarantine and continue.
        """
        if not self.is_repo():
            return False
        self._run("add", "-A", check=False)
        if not self._run("status", "--porcelain", check=False).stdout.strip():
            return True  # nothing to do
        self._run("commit", "-m", message, check=False)
        if not self.has_remote():
            return True  # local-only vault (tests) — commit is enough
        for attempt in range(1, self.retries + 1):
            if self._run("push", check=False).returncode == 0:
                return True
            self._run("pull", "--rebase", "--autostash", check=False)
            time.sleep(min(2 ** attempt, 8))
        return False
