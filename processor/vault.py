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
from datetime import UTC, datetime, timedelta
from pathlib import Path

class MalformedRecord(RuntimeError):
    """A record that cannot be trusted. Never skipped silently — success
    criterion S5 says no recording is dropped without being noticed."""


REQUIRED_FIELDS = ("plaud_id", "recorded_at", "audio_filename")


RAW, TRANSCRIBED, ROUTED, EXECUTING, EXECUTED, PUBLISHED, FAILED, RETRY_WAIT = (
    "raw", "transcribed", "routed", "executing", "executed", "published",
    "failed", "retry_wait"
)

# EXECUTING is committed before the agent starts. Status used to stay ROUTED for
# the whole run, so a crash re-ran an agent that may already have had side
# effects — and each retry re-spent the budget. A record found in EXECUTING at
# scan time is therefore a crashed run, not work to redo: see pipeline.py.

# How far along the pipeline each status is. Consulted when two hosts conflict on
# the same record's metadata: the further-advanced side wins, so a rebase can
# never revert a local advance back to `raw` and re-run paid transcription or a
# second agent. Ties keep "first push wins" (upstream). failed/retry_wait sit
# just above raw — they have been worked on, so they must not become raw again,
# but a genuinely published version elsewhere still beats them.
_PROGRESS = {
    RAW: 0, FAILED: 1, RETRY_WAIT: 1, TRANSCRIBED: 2, ROUTED: 3,
    EXECUTING: 4, EXECUTED: 5, PUBLISHED: 6,
}

# Backoff between attempts. `retryable` used to be recorded on the error and
# then ignored: fail() set FAILED, the scan excluded FAILED, and nothing ever
# tried again — so an API timeout or a 503 became a PERMANENT failure. These are
# the delays before attempts 2, 3 and 4; after that it is genuinely failed.
RETRY_BACKOFF_SECONDS = (300, 1200, 7200)


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
        """Resolved and CONTAINED.

        `audio_filename` originates upstream. A value containing `../` — from a
        malformed record or a compromised listing — would otherwise point
        anywhere the process can write. Require a bare filename and prove the
        resolved path stays inside the record's own directory.
        """
        name = self.data.get("audio_filename", "")
        if not name or name != Path(name).name or name in (".", ".."):
            raise MalformedRecord(
                f"{self.meta_path}: audio_filename must be a bare filename, "
                f"got {name!r}")
        base = self.meta_path.parent.resolve()
        candidate = (base / name).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            raise MalformedRecord(
                f"{self.meta_path}: audio_filename escapes its directory: {name!r}")
        return candidate

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
        attempts = self.data.get("attempts", 0) + 1
        self.data["attempts"] = attempts
        self.data["failed_stage"] = stage
        self.data["last_error"] = error[:500]

        if retryable and attempts <= len(RETRY_BACKOFF_SECONDS):
            delay = RETRY_BACKOFF_SECONDS[attempts - 1]
            when = datetime.now(UTC) + timedelta(seconds=delay)
            self.data["status"] = RETRY_WAIT
            self.data["next_attempt_at"] = (
                when.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
            self.data["retryable"] = True
            self.save()
            return RETRY_WAIT

        self.data["status"] = FAILED
        self.data["failed_at"] = utcnow()
        self.data["retryable"] = bool(retryable)
        self.data.pop("next_attempt_at", None)
        self.save()
        write_atomic(self.error_path(vault), json.dumps({
            "plaud_id": self.id, "stage": stage, "error": error,
            "retryable": retryable, "attempts": self.data["attempts"],
            "at": utcnow(),
        }, indent=2) + "\n")
        return FAILED

    def due(self) -> bool:
        """True when a retry_wait record's deadline has passed."""
        if self.status != RETRY_WAIT:
            return True
        when = self.data.get("next_attempt_at")
        if not when:
            return True
        try:
            due = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(UTC) >= due

    def rearm(self):
        """Force a retry now, whatever the deadline said."""
        self.data["status"] = self.data.get("failed_stage") or RAW
        self.data.pop("next_attempt_at", None)
        self.save()

    def save(self):
        write_atomic(self.meta_path, json.dumps(self.data, indent=2) + "\n")


def load_records(vault: Path, status: str | None = None,
                 on_bad=None) -> list[Record]:
    """Load records, REPORTING anything unreadable rather than skipping it.

    This used to `continue` past malformed JSON, which meant a corrupted
    metadata file silently vanished from the queue — directly contradicting
    success criterion S5, "no recording is silently dropped". Bad records are
    now surfaced through `on_bad` so the caller can quarantine, alarm, and exit
    non-zero.
    """
    inbox = vault / "inbox"
    if not inbox.is_dir():
        return []
    out = []
    for p in sorted(inbox.rglob("*.json")):
        try:
            data = json.loads(p.read_text())
            if not isinstance(data, dict):
                raise MalformedRecord(f"{p}: metadata is not an object")
            missing = [f for f in REQUIRED_FIELDS if not data.get(f)]
            if missing:
                raise MalformedRecord(f"{p}: missing required field(s) {missing}")
            rec = Record(p, data)
            _ = rec.audio          # forces the containment check
        except (json.JSONDecodeError, OSError, MalformedRecord) as e:
            if on_bad is not None:
                on_bad(p, e)
            else:
                raise
            continue
        if status is None or rec.status == status:
            out.append(rec)
    out.sort(key=lambda r: r.data.get("recorded_at", ""))
    return out


# ---------------------------------------------------------------------------
#  git
# ---------------------------------------------------------------------------

class VaultSyncError(RuntimeError):
    """Committed locally, but the remote does not have it.

    Raised rather than returned, because every caller ignored the old False and
    let the record advance anyway — reporting success for work the other half of
    the pipeline cannot see. A recording must not move to its next distributed
    stage unless the previous state is durably visible in the remote.
    """


def _tail(proc, n: int = 300) -> str:
    """The last of git's own complaint, for a log line."""
    if proc is None:
        return "(no output)"
    return ((proc.stderr or proc.stdout or "").strip() or "(no output)")[-n:]


class Git:
    def __init__(self, vault: Path, name: str, email: str, retries: int = 3,
                 log=None):
        self.vault, self.retries = vault, retries
        # A failed push used to be entirely silent: commit_push() returned
        # False and every caller ignored it, so work sat committed-but-local
        # while the journal said the pass succeeded. Anything that gives up
        # now says so, with git's own stderr attached.
        self.log = log or (lambda m: print(m, flush=True))
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
        }

    # A hung git — half-open TCP to the remote, a dead VPN — used to block the
    # pass forever. Under the timer systemd eventually reaps the unit, but a
    # manual run hangs indefinitely while holding the processor lock, after
    # which every timed pass exits 0 "skipped" and the whole pipeline is stalled
    # while reporting healthy. atticus_cli.doctor already learned this lesson.
    TIMEOUT_SECONDS = 180

    def _run(self, *args, check=True, timeout=None):
        t = self.TIMEOUT_SECONDS if timeout is None else timeout
        try:
            return subprocess.run(
                ["git", "-C", str(self.vault), *args],
                capture_output=True, text=True, check=check, env=self.env,
                timeout=t,
            )
        except subprocess.TimeoutExpired:
            self.log(f"git {' '.join(args)} exceeded {t}s — treating as failure")
            if check:
                raise
            return subprocess.CompletedProcess(
                list(args), 124, "", f"timed out after {t}s")

    def is_repo(self) -> bool:
        return (self.vault / ".git").exists()

    def has_remote(self) -> bool:
        return bool(self._run("remote", check=False).stdout.strip())

    # Paths where a conflict is benign: both sides describe the same
    # recording and differ only in ingest bookkeeping (ingested_at, which
    # host pulled it). Anything outside this set must never auto-resolve.
    BENIGN = ("inbox/", ".state/seen")

    def _resolve_metadata(self, f: str):
        """Keep whichever side of a conflicted record JSON is further along.

        Stage 2 is ours/upstream, stage 3 is theirs/local. Unparseable on either
        side falls back to upstream, which is the old behaviour and still safe.
        """
        sides = {}
        for stage in (2, 3):
            blob = self._run("show", f":{stage}:{f}", check=False)
            if blob.returncode != 0:
                continue
            try:
                sides[stage] = (json.loads(blob.stdout), blob.stdout)
            except json.JSONDecodeError:
                continue
        if len(sides) != 2:
            self._run("checkout", "--ours", "--", f, check=False)
            return
        ours, theirs = sides[2], sides[3]
        rank = (_PROGRESS.get(theirs[0].get("status", RAW), 0)
                - _PROGRESS.get(ours[0].get("status", RAW), 0))
        if rank > 0:
            self.log(f"  conflict on {f}: keeping local "
                     f"{theirs[0].get('status')!r} over upstream "
                     f"{ours[0].get('status')!r}")
            (self.vault / f).write_text(theirs[1])
        else:
            self._run("checkout", "--ours", "--", f, check=False)

    def _resolve_benign(self) -> bool:
        """Auto-resolve an in-progress rebase iff every conflicted path is one
        where either version is correct.

        This happens when two hosts ingest the same recording before either
        has pushed. The audio is byte-identical; only the metadata differs.
        Resolution is "first push wins" — take the upstream side, which during
        a rebase is --ours.

        Returns False if anything else conflicted, leaving the rebase for a
        human. Silently resolving a real conflict would be far worse than
        stopping.
        """
        r = self._run("diff", "--name-only", "--diff-filter=U", check=False)
        files = [f for f in r.stdout.split() if f]
        if not files or not all(f.startswith(self.BENIGN) for f in files):
            return False
        for f in files:
            if "/seen" in f and f.endswith(".jsonl"):
                # A ledger is an append-only log. Taking one side would DISCARD
                # the other host's entries, which then re-downloads what it
                # already has. Union both sides, preserving order and dropping
                # exact duplicates.
                lines, seen = [], set()
                for stage in (2, 3):        # 2 = ours/upstream, 3 = theirs/local
                    blob = self._run("show", f":{stage}:{f}", check=False).stdout
                    for line in blob.splitlines():
                        if line.strip() and line not in seen:
                            seen.add(line)
                            lines.append(line)
                (self.vault / f).write_text("\n".join(lines) + "\n" if lines else "")
            else:
                # Metadata describing the same recording. Blanket --ours was
                # wrong: the processor writes *status* into inbox/**/*.json, so
                # "ingest owns inbox/" does not hold for this file, and taking
                # upstream could discard a local transcribed/executed advance
                # while keeping its artifacts — sending the record back through
                # paid transcription and a second agent run. Keep the further
                # advanced side instead; ties still go to upstream.
                self._resolve_metadata(f)
            self._run("add", "--", f, check=False)
        env = {**self.env, "GIT_EDITOR": "true"}
        done = subprocess.run(
            ["git", "-C", str(self.vault), "rebase", "--continue"],
            capture_output=True, text=True, env=env)
        return done.returncode == 0

    def pull(self) -> bool:
        if not (self.is_repo() and self.has_remote()):
            return True
        r = self._run("pull", "--rebase", "--autostash", check=False)
        if r.returncode == 0:
            return True
        if self._resolve_benign():
            return True
        self._run("rebase", "--abort", check=False)
        self.log(f"git pull failed: {_tail(r)}")
        return False

    def commit_push(self, message: str) -> bool:
        """Stage everything, commit, push with bounded retry.

        Disjoint paths between hosts mean rebases nearly always apply cleanly;
        the retry is for the rare interleave, not a busy-wait. Returns False
        rather than raising so the caller can quarantine and continue.
        """
        if not self.is_repo():
            return False
        # These two used to run with their return codes discarded, and that was
        # the worst silent failure in the system. If `add` fails — most often
        # because the other role holds .git/index.lock, which is routine now
        # that both run on one host — then `status` fails too, its empty stdout
        # reads as a clean tree, `_ahead()` finds nothing, and this function
        # returned True having committed NOTHING. The caller marked the record
        # done, the ledger was already written, and the work sat uncommitted and
        # invisible to the other stage with no error anywhere.
        add = self._run("add", "-A", check=False)
        if add.returncode != 0:
            raise VaultSyncError(f"git add failed: {_tail(add)}")
        st = self._run("status", "--porcelain", check=False)
        if st.returncode != 0:
            raise VaultSyncError(f"git status failed: {_tail(st)}")
        dirty = bool(st.stdout.strip())
        if not dirty:
            # A clean tree does NOT mean we are in sync. This used to return
            # success without looking at the remote, so a commit stranded by an
            # earlier failed push was never retried by any later pass either —
            # and for a record that reached its terminal state, no further commit
            # would ever come. Push if we are ahead.
            if self.has_remote() and self._ahead():
                self.log("clean tree but local is ahead of the remote — pushing")
                return self._push_with_retry(None)
            return True
        commit = self._run("commit", "-m", message, check=False)
        if commit.returncode != 0:
            # We know the tree was dirty, so "nothing to commit" is not the
            # explanation — a hook, a bad identity, or a read-only vault is.
            raise VaultSyncError(f"git commit failed: {_tail(commit)}")
        if not self.has_remote():
            return True  # local-only vault (tests) — commit is enough
        return self._push_with_retry(message)

    def _ahead(self) -> bool:
        """True when the local branch has commits the remote does not."""
        r = self._run("rev-list", "--count", "@{u}..HEAD", check=False)
        try:
            return int((r.stdout or "0").strip()) > 0
        except ValueError:
            return False        # no upstream configured; nothing to be ahead of

    def _push_with_retry(self, message):
        last = None
        for attempt in range(1, self.retries + 1):
            last = self._run("push", check=False)
            if last.returncode == 0:
                return True
            # Another host pushed between our pull and our push. Rebase onto
            # it, auto-resolving only the benign ingest collisions.
            if not self.pull():
                raise VaultSyncError(
                    f"push abandoned — unresolved conflict: {_tail(last)}")
            time.sleep(min(2 ** attempt, 8))
        raise VaultSyncError(
            f"push failed after {self.retries} attempt(s); "
            f"{'the commit is' if message else 'commits are'} local only: {_tail(last)}")
