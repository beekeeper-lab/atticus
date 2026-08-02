"""Vault access: records, status transitions, and safe git.

The status field in each recording's metadata JSON *is* the pipeline state.
Every stage advances it and commits, so a crash leaves a known state and the
next run resumes rather than redoing work.

Two hosts push to this repo (WarDog writes inbox/, Forge writes processed/),
so every push is pull-rebase-retry. See SPEC §4.3.
"""
import fcntl
import json
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
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

# Terminal states the OPERATOR causes, not the pipeline (issue #82).
#
#   CANCELLED   "stop that" — the work is abandoned. Reachable from any stage
#               before publish, and from EXECUTING by killing the run.
#   SUPERSEDED  "that one is wrong / replaced" — applied to work that already
#               PUBLISHED. The artifact stays, because it is committed and may
#               already have been read; the status is how the vault marks it as
#               no longer the answer. Unpublishing is not a thing this system
#               can honestly offer.
CANCELLED, SUPERSEDED = "cancelled", "superseded"

# Statuses that mean "do not pick this up again". The scan already skips
# PUBLISHED and FAILED; these join them, and nothing may advance out of one.
TERMINAL = (PUBLISHED, CANCELLED, SUPERSEDED)

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
    # Above everything, deliberately. If two hosts disagree because one of them
    # cancelled a record, the cancellation must win — the operator said stop,
    # and a rebase that resurrected the run would be the worst possible reading
    # of "the further-advanced side wins".
    CANCELLED: 7, SUPERSEDED: 7,
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

    def clear_error(self, vault: Path) -> bool:
        """Remove a stale failures/ entry once the record has succeeded.

        Nothing did this, so a record that failed and then succeeded on a later
        attempt kept its error file forever. Observed 2026-07-30:
        ...T184315Z_ac02959a455c failed at 19:00, was interrupted at 19:10 (which
        wrote the error file), then PUBLISHED a 41 KB report at 19:15 — and left a
        failures/ entry behind that made the heartbeat and the failures/ count
        overreport permanently.

        The error file is a live signal, not an archive: the record's own metadata
        keeps `attempts`, `failed_stage` and `last_error`, so the history survives
        deleting it. Best-effort — a published record must not be un-published by
        a filesystem problem here.
        """
        p = self.error_path(vault)
        try:
            if p.is_file():
                p.unlink()
                return True
        except OSError:
            pass
        return False

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
        """Force a retry now, whatever the deadline said.

        EXECUTING is never a rearm target. It is an in-progress marker committed
        before the agent starts, not a re-entrant stage — so restoring it made the
        pipeline's own crash guard reject the record it had just re-armed:

            agent fails (retryable) -> failed_stage="executing", status=retry_wait
            deadline passes         -> rearm() restores status="executing"
            next pass               -> "interrupted mid-execution", NOT retried

        which turned every retryable execution failure into a terminal one.
        Retrying the execute stage means re-entering it from ROUTED. A record
        genuinely abandoned mid-run — SIGKILL, reboot — still sits in EXECUTING
        with no recorded failure, and the guard still catches that.
        """
        stage = self.data.get("failed_stage") or RAW
        self.data["status"] = ROUTED if stage == EXECUTING else stage
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


# Who owns what, in one place, because CLAUDE.md asserts this boundary and until
# 2026-07-31 nothing enforced it. Pass the matching constant as Git(paths=...).
#
# The processor updates a record's metadata in inbox/ as it advances (status,
# transcribed_at, failed_stage), so inbox/ is shared with ingest — they collide on
# a file, not on a directory, and `pull --rebase` plus the union merge driver on
# the ledgers is what handles that. What neither role owns is reports/ and site/,
# and those are exactly what got swept.
OWNED_INGEST = ["inbox", ".state"]
OWNED_PROCESSOR = ["inbox", "processed", "failures", ".state"]
OWNED_BRIEF = ["reports", ".state"]
OWNED_RETENTION = ["inbox"]


class Git:
    def __init__(self, vault: Path, name: str, email: str, retries: int = 3,
                 log=None, paths: list[str] | None = None):
        self.vault, self.retries = vault, retries
        # Which paths this role owns, and therefore the ONLY paths it stages.
        #
        # `git add -A` was staging the whole worktree. CLAUDE.md states the two
        # roles "own disjoint paths — ingest inbox/ + .state/, processor
        # processed/ + failures/", and that boundary is load-bearing for the
        # two-role design; add -A silently violated it. Observed four times on
        # 2026-07-31: pipeline commits swept up unrelated in-progress edits to
        # site/ and tests/ and PUSHED them, mid-edit, under messages like
        # "retry-wait <stem> (attempt 1)". Two costs, both real — work gets
        # published in a broken intermediate state, and commit messages
        # misdescribe their own contents, which degrades git history as the audit
        # trail the security posture depends on.
        #
        # None keeps the old whole-worktree behaviour, because some callers
        # legitimately want it: retention rewrites records anywhere under inbox/,
        # and the sweep path exists to recover work stranded by an interrupted
        # pass. Scoping is opt-in per caller rather than imposed here.
        self.paths = list(paths) if paths else None
        # A failed push used to be entirely silent: commit_push() returned
        # False and every caller ignored it, so work sat committed-but-local
        # while the journal said the pass succeeded. Anything that gives up
        # now says so, with git's own stderr attached.
        self.log = log or (lambda m: print(m, flush=True))
        # git's own words from the last failed pull, so a caller can report the
        # real cause instead of guessing at one.
        self.last_error = None
        # Serialises git against every other atticus process sharing this vault.
        # Lives in .git/ so it is per-repo, never committed, and shared by the
        # processor, ingest, retention and the site build alike.
        self._lock_path = self.vault / ".git" / "atticus-git.lock"
        self._lock_depth = 0
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
        }

    # How long to wait for another process to finish its git work. The operations
    # under this lock take a second or two; the agent run does NOT hold it, which
    # is the whole reason the lock is around git rather than around a pass.
    LOCK_WAIT_SECONDS = 60

    @contextmanager
    def _serialised(self):
        """Hold the vault's git lock for the duration of a git sequence.

        Ingest and the processor share one working tree but took DIFFERENT
        single-instance locks, so nothing excluded them from each other. Two
        concurrent `git fetch`es leave .git/FETCH_HEAD holding more than one
        branch, and `pull --rebase` then aborts with "Cannot rebase onto multiple
        branches" — observed in production every ~15 minutes, whenever the 5- and
        15-minute timers landed in the same second.

        Re-entrant: commit_push() -> _push_with_retry() -> pull() all nest, and
        flock() would deadlock against itself on a second descriptor.
        """
        if self._lock_depth or not self._lock_path.parent.is_dir():
            # Already held by this instance, or a local-only vault with no .git
            # (tests), where there is nothing to race against.
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return

        fd = os.open(self._lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            deadline = time.monotonic() + self.LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise VaultSyncError(
                            f"another atticus process has held the vault git "
                            f"lock for over {self.LOCK_WAIT_SECONDS}s "
                            f"({self._lock_path})") from None
                    time.sleep(0.2)
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

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
        with self._serialised():
            r = self._run("pull", "--rebase", "--autostash", check=False)
            if r.returncode == 0:
                self.last_error = None
                return True
            if self._resolve_benign():
                self.last_error = None
                return True
            self._run("rebase", "--abort", check=False)
            self.last_error = _tail(r)
            self.log(f"git pull failed: {self.last_error}")
            return False

    def commit_push(self, message: str) -> bool:
        """Stage everything, commit, push with bounded retry.

        Disjoint paths between hosts mean rebases nearly always apply cleanly;
        the retry is for the rare interleave, not a busy-wait. Returns False
        rather than raising so the caller can quarantine and continue.
        """
        if not self.is_repo():
            return False
        with self._serialised():
            return self._commit_push_locked(message)

    def _commit_push_locked(self, message: str) -> bool:
        # These two used to run with their return codes discarded, and that was
        # the worst silent failure in the system. If `add` fails — most often
        # because the other role holds .git/index.lock, which is routine now
        # that both run on one host — then `status` fails too, its empty stdout
        # reads as a clean tree, `_ahead()` finds nothing, and this function
        # returned True having committed NOTHING. The caller marked the record
        # done, the ledger was already written, and the work sat uncommitted and
        # invisible to the other stage with no error anywhere.
        # Scoped when the caller declared what it owns, whole-worktree otherwise.
        # `--` guards against a path that looks like a revision.
        if self.paths:
            add = self._run("add", "-A", "--", *self.paths, check=False)
        else:
            add = self._run("add", "-A", check=False)
        if add.returncode != 0:
            raise VaultSyncError(f"git add failed: {_tail(add)}")
        # Ask about the same scope that was staged. Asking about the whole tree
        # would see an unrelated dirty file, call the tree dirty, and then commit
        # nothing — `git commit` exits non-zero on an empty index, which this
        # function turns into a VaultSyncError and a failed pass. A neighbouring
        # editor should not be able to fail the pipeline.
        st = (self._run("status", "--porcelain", "--", *self.paths, check=False)
              if self.paths else
              self._run("status", "--porcelain", check=False))
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
