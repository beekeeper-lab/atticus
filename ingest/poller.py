#!/usr/bin/env python3
"""Atticus ingest — recordings into the vault.

Runs on the agent host (ADR-003), every 15 minutes.

    list → dedupe against the ledger → download → commit → push

**The transport is a pluggable executable, not an import.** The poller shells
out to whatever `ATTICUS_FETCHER` points at and only requires that it
implement this contract:

    <fetcher> whoami --json           → {...}          health check
    <fetcher> list --days N --json    → {"recordings": [...]}
    <fetcher> audio <id> -o <path>    → writes the file

    exit 0 ok · 2 usage · 3 auth/session dead · 4 transient · 5 upstream changed

That is why the ingest transport (SPEC §2.2.1 — direct BLE, Android bridge, or
the web API) can still be undecided while this exists: whichever wins ships a
fetcher, and nothing here changes.

    poller.py                 one pass
    poller.py --status        ledger + vault summary, changes nothing
    poller.py --dry-run       list and diff, download nothing
    poller.py --health        exercise the fetcher only

Exit: 0 clean · 1 partial failure · 2 config error · 3 fetcher auth dead
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from hashlib import sha256
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "processor"))

from config import Config          # noqa: E402
from lock import AlreadyRunning, single_instance   # noqa: E402
from notify import clear as alarm_clear, notify   # noqa: E402
from redact import redact                         # noqa: E402
from vault import Git, VaultSyncError, write_atomic, utcnow   # noqa: E402

EXIT_OK, EXIT_PARTIAL, EXIT_CONFIG, EXIT_AUTH = 0, 1, 2, 3
F_OK, F_USAGE, F_AUTH, F_TRANSIENT, F_CHANGED = 0, 2, 3, 4, 5


class FetcherError(RuntimeError):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


class Fetcher:
    """Thin wrapper over the transport executable."""

    def __init__(self, path: Path, timeout: int = 300):
        self.path, self.timeout = Path(path), timeout

    def _run(self, *args, timeout=None):
        if not self.path.exists():
            raise FetcherError(f"fetcher not found: {self.path}", F_USAGE)
        cmd = [sys.executable, str(self.path), *args]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            raise FetcherError(f"fetcher timed out: {' '.join(args)}", F_TRANSIENT)
        if p.returncode != 0:
            # NEVER the raw tail. A library decides what goes in its error
            # strings and libraries interpolate URLs: on 2026-07-30 this put a
            # presigned S3 URL with its AWSALB session cookies into the journal,
            # which is persistent — a short-lived credential became a durable
            # one. Prefer the FIRST lines, where the actual message lives (the
            # URL tends to be mid-traceback), and redact whatever survives.
            raw = (p.stderr or p.stdout or "").strip()
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            head = " | ".join(lines[:3])[:300] if lines else "(no output)"
            raise FetcherError(
                f"{self.path.name} {args[0]}: {redact(head)}", p.returncode)
        return p.stdout

    @staticmethod
    def _json(out: str, what: str):
        """Parse fetcher stdout, mapping garbage into the exit-code contract.

        Non-JSON output raised JSONDecodeError, and a bare JSON list raised
        AttributeError on data.get() — both OUTSIDE the FetcherError handler, so
        a fetcher printing an unexpected shape produced a traceback and exit 1
        rather than the documented partial-failure path.
        """
        try:
            return json.loads(out or "{}")
        except ValueError as e:
            raise FetcherError(
                f"fetcher {what} returned non-JSON output: {e}", F_CHANGED) from e

    def whoami(self):
        data = self._json(self._run("whoami", "--json"), "whoami")
        if not isinstance(data, dict):
            raise FetcherError(f"whoami returned {type(data).__name__}, "
                               f"expected an object", F_CHANGED)
        return data

    def list(self, days: int):
        data = self._json(self._run("list", "--days", str(days), "--json"), "list")
        if isinstance(data, list):
            recs = data
        elif isinstance(data, dict):
            recs = data.get("recordings", [])
        else:
            raise FetcherError(f"list returned {type(data).__name__}, expected "
                               f"an object or array", F_CHANGED)
        if not isinstance(recs, list):
            raise FetcherError(f"list.recordings is {type(recs).__name__}, "
                               f"expected an array", F_CHANGED)
        for r in recs:
            missing = {"id", "created_at"} - set(r)
            if missing:
                raise FetcherError(f"record missing {missing}: {r}", F_CHANGED)
        return recs

    def audio(self, rec_id: str, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run("audio", rec_id, "-o", str(dest), timeout=self.timeout * 2)
        if not dest.is_file() or dest.stat().st_size == 0:
            raise FetcherError(f"fetcher reported success but produced no file "
                               f"for {rec_id}", F_CHANGED)
        return dest


# ---------------------------------------------------------------------------
#  ledger
# ---------------------------------------------------------------------------

def host_id() -> str:
    """Identifies which machine wrote a ledger entry. Override with
    ATTICUS_HOST when the hostname is not stable or not meaningful."""
    import socket
    h = os.environ.get("ATTICUS_HOST") or socket.gethostname()
    return re.sub(r"[^a-z0-9-]+", "-", h.lower().split(".")[0]) or "unknown"


def ledger_path(vault: Path, host: str | None = None) -> Path:
    """One ledger PER HOST.

    Any machine may run ingest — for failover or because the pin is in range
    of a different box. A single shared seen.jsonl would have every host
    appending to the same file, conflicting on every rebase. Per-host files
    are disjoint, so they never conflict, exactly like inbox/ vs processed/.

    Reads take the union of all of them, so a recording pulled by one host is
    never pulled again by another.
    """
    return vault / ".state" / f"seen-{host or host_id()}.jsonl"


def load_seen(vault: Path) -> set:
    """Union of every host's ledger, plus the legacy single-file one."""
    state = vault / ".state"
    if not state.is_dir():
        return set()
    out = set()
    for p in sorted(state.glob("seen*.jsonl")):
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return out


def append_seen(vault: Path, rec_id: str, stem: str):
    """Append-only, to THIS host's ledger. Written *after* the audio is
    durable, so a crash re-fetches rather than silently skipping."""
    p = ledger_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"id": rec_id, "stem": stem,
                            "host": host_id(), "at": utcnow()}) + "\n")


# ---------------------------------------------------------------------------

def _alarm_dead_session(cfg, err, log):
    """The one failure that must never be quiet.

    A dead session and a quiet weekend are indistinguishable from the outside:
    both are "0 new recordings" forever. Recordings keep piling up in Plaud
    Cloud meanwhile, so the cost of not noticing is unbounded.

    Throttled, because the timer rediscovers this every tick.
    """
    sent = notify(
        cfg,
        f"Atticus ingest: the Plaud session on {host_id()} is dead — no "
        f"recordings are being pulled.\n\n{err}\n\n"
        f"Re-seed it:  plaud_web.py login",
        log=log, key="plaud-auth", title="Atticus ingest — session dead",
    )
    if not cfg.notify_url:
        log("  ! ATTICUS_NOTIFY_URL is unset — this failure alarms nowhere. "
            "Set it; a dead session is otherwise silent.")
    elif sent:
        log("  ! alarm sent")


def _sweep_dirty(git, log, cfg):
    """Commit anything left uncommitted by an earlier interrupted pass.

    Cheap when the tree is clean (commit_push short-circuits), and it is the only
    thing that recovers a recording stranded between the ledger append and the
    commit. Never fatal: the pass had no new work anyway.
    """
    try:
        if git.commit_push("sweep: commit work stranded by an interrupted pass"):
            return
        log("  ! stranded work could not be pushed")
    except VaultSyncError as e:
        log(f"  ! sweep failed: {e}")
    notify(cfg, f"Atticus ingest on {host_id()}: found recordings committed "
                f"locally but not pushed. They are invisible downstream.",
           log=log, key="vault-push", title="Atticus ingest — push failed")


def safe_id(rec: dict) -> str:
    """The Plaud id is spliced into the audio path, the .json path, and the
    fetcher's -o argument. A changed or hostile id containing "/" or ".." would
    escape inbox/YYYY/MM — vault.py defends the audio_filename side, ingest did
    not. Allow only [A-Za-z0-9]; reject loudly if nothing survives."""
    sid = re.sub(r"[^A-Za-z0-9]", "", str(rec.get("id", "")))[:12]
    if not sid:
        raise FetcherError(f"record id has no filesystem-safe characters: "
                           f"{rec.get('id')!r}", F_CHANGED)
    return sid


def parse_created_at(rec: dict) -> datetime:
    """created_at drives both the vault filename and the inbox month directory.
    A malformed value used to raise an uncaught ValueError mid-loop, stalling
    every later record permanently. Fail loudly as a per-record FetcherError
    instead — and never substitute now(UTC), which would mint a different stem
    each pass and defeat the duplicate check."""
    try:
        return datetime.fromisoformat(str(rec["created_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError) as e:
        raise FetcherError(f"unparseable created_at {rec.get('created_at')!r}: "
                           f"{e}", F_CHANGED) from e


def make_stem(rec: dict) -> str:
    """2026-07-28T142211Z_<id12> — sorts chronologically, unique per recording."""
    dt = parse_created_at(rec)
    return f"{dt.strftime('%Y-%m-%dT%H%M%SZ')}_{safe_id(rec)}"


def sha256_stream(path: Path, chunk: int = 1 << 20) -> str:
    """Hash without loading the file. A 40-minute recording is ~10 MB today and
    a chunked meeting could be far more; there is no reason to hold any of it."""
    h = sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def verify_audio(path: Path, claimed_seconds, log) -> dict:
    """Confirm the download is really decodable audio.

    Returns metadata to fold into the record. Non-fatal when ffprobe is absent —
    this must not become a hard dependency of ingest — but a file that ffprobe
    can read and finds NO audio stream in is refused, because that is the saved
    error-page case.
    """
    exe = shutil.which("ffprobe")
    if not exe:
        return {"audio_verified": False}
    try:
        p = subprocess.run(
            [exe, "-v", "error", "-show_entries",
             "stream=codec_type,codec_name:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        # A ffprobe crash or timeout is a TOOL failure, not evidence of a bad
        # download — misclassifying it as "not audio" refused good files every
        # pass. Non-fatal per the docstring: skip verification, don't refuse.
        log(f"    ! ffprobe {type(e).__name__} — skipping audio verification "
            f"for {path.name}")
        return {"audio_verified": False}
    # A non-zero exit (e.g. ffprobe cannot parse a saved error page) still means
    # ffprobe ran, so it stays in the refuse path below via an empty stream set.
    try:
        data = json.loads(p.stdout or "{}") if p.returncode == 0 else {}
    except ValueError:
        data = {}

    streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise FetcherError(
            f"downloaded file is not decodable audio: {path.name} "
            f"({path.stat().st_size:,} bytes) — a saved error page or truncated "
            f"download looks exactly like this", F_CHANGED)

    try:
        observed = round(float(data.get("format", {}).get("duration")), 1)
    except (TypeError, ValueError):
        observed = None

    out = {"audio_verified": True,
           "detected_codec": streams[0].get("codec_name"),
           "verified_duration_seconds": observed}

    # Disagreement means the download is short, or upstream metadata is wrong.
    # Either way the operator should know before it is transcribed.
    if observed and isinstance(claimed_seconds, (int, float)) and claimed_seconds:
        drift = abs(observed - claimed_seconds)
        if drift > max(5, claimed_seconds * 0.1):
            log(f"    ! duration mismatch: upstream says {claimed_seconds}s, "
                f"file is {observed}s")
            out["duration_mismatch"] = True
    return out


def ingest_one(rec: dict, vault: Path, fetcher: Fetcher, log) -> dict:
    stem = make_stem(rec)
    dt = parse_created_at(rec)
    ym = dt.strftime("%Y/%m")
    inbox = vault / "inbox" / ym

    ext = {"audio/mp3": ".mp3", "audio/mpeg": ".mp3", "audio/wav": ".wav",
           "audio/ogg": ".ogg", "audio/opus": ".opus"}.get(rec.get("filetype"), ".mp3")
    audio = inbox / f"{stem}{ext}"

    log(f"  ↓ {stem}  ({rec.get('duration_seconds', '?')}s)")
    fetcher.audio(rec["id"], audio)

    digest = sha256_stream(audio)
    size = audio.stat().st_size

    # B6: size > 0 is not proof it is audio. A saved HTML error page or a
    # truncated download passes that check and becomes a "recording" that only
    # fails later, in the processor, after it has been committed.
    #
    # verify_audio raises AFTER the fetcher has already renamed the file into
    # inbox/, and the caller's failure path does not ledger the record — so the
    # orphan sat in the working tree until the next successful recording's
    # `add -A` committed it into vault history PERMANENTLY, while the bad
    # recording re-downloaded every 15 minutes forever. Remove it on the way out.
    try:
        probe = verify_audio(audio, rec.get("duration_seconds"), log)
    except FetcherError:
        audio.unlink(missing_ok=True)
        for stray in inbox.glob(f"{stem}*.part"):
            stray.unlink(missing_ok=True)
        raise

    if rec.get("md5"):
        log(f"    {size:,} bytes  sha256:{digest[:12]}…  (upstream md5 recorded)")
    else:
        log(f"    {size:,} bytes  sha256:{digest[:12]}…")

    meta = {
        "plaud_id": rec["id"],
        "source": "plaud-notepin-s",
        "transport": rec.get("transport", "unknown"),
        "recorded_at": rec["created_at"],
        "ingested_at": utcnow(),
        "audio_filename": audio.name,
        "audio_sha256": digest,
        "bytes": size,
        "duration_seconds": rec.get("duration_seconds"),
        **probe,
        "upstream_name": rec.get("name") or "",
        "upstream_md5": rec.get("md5"),
        "ingested_by": host_id(),
        "status": "raw",
        "attempts": 0,
    }
    write_atomic(inbox / f"{stem}.json", json.dumps(meta, indent=2) + "\n")
    return {"stem": stem, "bytes": size}


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int)
    ap.add_argument("--env", type=Path)
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    try:
        cfg = Config(args.env)
    except Exception as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG

    def log(m): print(m, flush=True)

    vault = cfg.vault
    if not vault.is_dir():
        print(f"vault not found: {vault}", file=sys.stderr)
        return EXIT_CONFIG

    fetcher_path = Path(cfg.fetcher).expanduser()
    if not fetcher_path.is_absolute():
        fetcher_path = REPO / fetcher_path
    fetcher = Fetcher(fetcher_path, cfg.fetcher_timeout)
    days = args.days or cfg.poll_days

    if args.status:
        seen = load_seen(vault)
        inbox = list((vault / "inbox").rglob("*.json")) if (vault / "inbox").is_dir() else []
        print(f"host     {host_id()}")
        print(f"vault    {vault}")
        print(f"fetcher  {fetcher_path}"
              f"{'' if fetcher_path.exists() else '   ** MISSING **'}")
        print(f"ledger   {len(seen)} recording(s) already pulled")
        print(f"inbox    {len(inbox)} metadata file(s)")
        return EXIT_OK

    if args.health:
        try:
            print(json.dumps(fetcher.whoami(), indent=2))
            alarm_clear("plaud-auth")
            return EXIT_OK
        except FetcherError as e:
            print(f"fetcher unhealthy: {e}", file=sys.stderr)
            if e.code == F_AUTH:
                _alarm_dead_session(cfg, e, log)
                return EXIT_AUTH
            return EXIT_PARTIAL

    # -- the pass ----------------------------------------------------------
    git = Git(vault, cfg.git_name, cfg.git_email, cfg.push_retries,
              log=lambda m: log(f"  ! {m}"))
    if not args.no_push:
        git.pull()

    try:
        recs = fetcher.list(days)
    except FetcherError as e:
        # A dead session is silent otherwise: no new recordings and a broken
        # login look identical from here. Exit distinctly so the timer's
        # failure shows up in the journal.
        if e.code == F_AUTH:
            print(f"AUTH FAILURE — the fetcher's session is dead: {e}", file=sys.stderr)
            _alarm_dead_session(cfg, e, log)
            return EXIT_AUTH
        if e.code == F_CHANGED:
            print(f"UPSTREAM CHANGED — re-run recon: {e}", file=sys.stderr)
            return EXIT_PARTIAL
        print(f"fetcher error: {e}", file=sys.stderr)
        return EXIT_PARTIAL

    # The session answered, so any standing auth alarm is stale. Clearing it
    # means the *next* failure alarms at once instead of waiting out the window.
    alarm_clear("plaud-auth")

    seen = load_seen(vault)
    fresh = [r for r in recs if r["id"] not in seen]
    log(f"{len(recs)} recording(s) in the last {days}d · "
        f"{len(seen)} already seen · {len(fresh)} new")

    if not fresh:
        # Nothing new, but the tree may still be dirty from an earlier pass that
        # died between append_seen() and commit_push(): the id is in the local
        # ledger, so `fresh` is empty forever after, and this used to return
        # EXIT_OK without ever committing. The audio then sat uncommitted and
        # invisible to the processor until some FUTURE recording's `add -A`
        # happened to sweep it in — days, given bursty arrivals — while every
        # pass reported clean. Sweep it now instead.
        if not args.no_push:
            _sweep_dirty(git, log, cfg)
        return EXIT_OK

    if args.dry_run:
        for r in fresh:
            log(f"  would pull {make_stem(r)}  {r.get('name','')[:40]}")
        return EXIT_OK

    ok = failed = skipped = unpushed = 0
    auth_died = False
    for rec in fresh:
        # Everything up to and including the download is wrapped: a single bad
        # record (unparseable created_at, filesystem-hostile id, failed fetch)
        # counts as `failed` and the loop continues, rather than an uncaught
        # error killing the whole pass and stalling every later record.
        try:
            # Belt and braces. The ledger is the primary guard, but two hosts
            # can both list a recording before either has pushed. If the pull
            # brought the other host's metadata, honour it rather than
            # downloading twice.
            stem = make_stem(rec)

            # Absurd length — do not spend the download. Plaud reports duration
            # in the listing, so this costs nothing. The processor truncates
            # merely-long recordings; this is the separate case of something
            # pathological.
            secs = rec.get("duration_seconds")
            if (cfg.max_ingest_seconds and isinstance(secs, (int, float))
                    and secs > cfg.max_ingest_seconds):
                log(f"  ⊘ {stem}: {secs / 60:.0f} min exceeds the "
                    f"{cfg.max_ingest_seconds / 60:.0f} min ingest limit — not downloaded")
                # Ledger it so this is not re-evaluated every 15 minutes. Delete
                # the line to reconsider it after raising the limit.
                append_seen(vault, rec["id"], f"{stem} (skipped: too long)")
                # Per-recording throttle key — a shared "too-long" key silenced
                # every oversized recording after the first for 6h, so a second
                # one vanished with only a journal line.
                notify(cfg, f"Skipped a {secs / 60:.0f}-minute recording on "
                            f"{host_id()} — over the ingest limit, not downloaded.",
                       log=log, key=f"too-long-{safe_id(rec)}")
                skipped += 1
                continue

            dt = parse_created_at(rec)
            if (vault / "inbox" / dt.strftime("%Y/%m") / f"{stem}.json").exists():
                log(f"  = {stem} already in the vault (another host) — recording locally")
                append_seen(vault, rec["id"], stem)
                skipped += 1
                continue

            res = ingest_one(rec, vault, fetcher, log)
        except FetcherError as e:
            # Do NOT add to the ledger — an unfetched recording must be retried.
            log(f"  ✗ {rec.get('id')}: {e}")
            failed += 1
            if e.code == F_AUTH:
                # ingest/README documents "alarms on exit 3", and this path did
                # neither: it returned EXIT_PARTIAL and never alarmed, having
                # ALREADY cleared the plaud-auth throttle earlier in the pass.
                # The operator learned about a dead session only when the next
                # pass's list() failed — or never, if every record hit this.
                log("    session died mid-pass; stopping")
                print(f"AUTH FAILURE — session died mid-pass: {e}", file=sys.stderr)
                _alarm_dead_session(cfg, e, log)
                auth_died = True
                break
            continue
        append_seen(vault, rec["id"], res["stem"])
        try:
            pushed = args.no_push or git.commit_push(f"ingest {res['stem']}")
        except VaultSyncError as e:
            log(f"  ! vault sync failed: {e}")
            pushed = False
        if not pushed:
            # The audio is safe in a local commit, so this is not data loss —
            # but Forge only ever sees the vault through the remote, so an
            # unpushed recording will never be processed. Loud, not fatal:
            # the next pass retries the push as part of its own.
            unpushed += 1
        ok += 1

    tail = f", {unpushed} committed but NOT PUSHED" if unpushed else ""
    log(f"ingested {ok}, skipped {skipped}, failed {failed}{tail}")
    if unpushed:
        notify(cfg, f"Atticus ingest on {host_id()}: {unpushed} recording(s) "
                    f"committed locally but the push failed — they are "
                    f"invisible downstream until it succeeds.",
               log=log, key="vault-push",
               title="Atticus ingest — push failed")
    elif ok:
        # Pushes are working again; clear the throttle so the NEXT failure alarms
        # at once instead of being swallowed by a window opened hours ago. Only
        # plaud-auth was ever cleared, which is why a fail/recover/fail pattern
        # stayed silent — the exact shape notify.clear() exists to prevent.
        alarm_clear("vault-push")
    if auth_died:
        return EXIT_AUTH
    return EXIT_PARTIAL if (failed or unpushed) else EXIT_OK


def _guarded():
    # Vault-relative lock, for the same reason as the processor's — see lock.py.
    vault = None
    try:
        vault = Config().vault
    except Exception as e:                          # noqa: BLE001
        # Not fatal to locking: fall back to the runtime-dir lock and let main()
        # report the config problem properly a moment later.
        print(f"lock: cannot resolve the vault ({type(e).__name__}); "
              f"using a fallback lock location", file=sys.stderr)
    try:
        with single_instance("ingest", vault=vault):
            return main()
    except AlreadyRunning as e:
        print(f"skipped: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(_guarded())
