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
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "processor"))

from config import Config          # noqa: E402
from notify import clear as alarm_clear, notify   # noqa: E402
from vault import Git, write_atomic, utcnow   # noqa: E402

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
            err = (p.stderr or p.stdout or "").strip()[-300:]
            raise FetcherError(f"{self.path.name} {args[0]}: {err}", p.returncode)
        return p.stdout

    def whoami(self):
        return json.loads(self._run("whoami", "--json") or "{}")

    def list(self, days: int):
        out = self._run("list", "--days", str(days), "--json")
        data = json.loads(out or "{}")
        recs = data.get("recordings", data if isinstance(data, list) else [])
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


def make_stem(rec: dict) -> str:
    """2026-07-28T142211Z_<id12> — sorts chronologically, unique per recording."""
    try:
        dt = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.now(timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H%M%SZ')}_{str(rec['id'])[:12]}"


def ingest_one(rec: dict, vault: Path, fetcher: Fetcher, log) -> dict:
    stem = make_stem(rec)
    dt = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00"))
    ym = dt.strftime("%Y/%m")
    inbox = vault / "inbox" / ym

    ext = {"audio/mp3": ".mp3", "audio/mpeg": ".mp3", "audio/wav": ".wav",
           "audio/ogg": ".ogg", "audio/opus": ".opus"}.get(rec.get("filetype"), ".mp3")
    audio = inbox / f"{stem}{ext}"

    log(f"  ↓ {stem}  ({rec.get('duration_seconds', '?')}s)")
    fetcher.audio(rec["id"], audio)

    digest = sha256(audio.read_bytes()).hexdigest()
    size = audio.stat().st_size

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
        return EXIT_OK

    if args.dry_run:
        for r in fresh:
            log(f"  would pull {make_stem(r)}  {r.get('name','')[:40]}")
        return EXIT_OK

    ok = failed = skipped = unpushed = 0
    for rec in fresh:
        # Belt and braces. The ledger is the primary guard, but two hosts can
        # both list a recording before either has pushed. If the pull brought
        # the other host's metadata, honour it rather than downloading twice.
        stem = make_stem(rec)

        # Absurd length — do not spend the download. Plaud reports duration in
        # the listing, so this costs nothing. The processor truncates merely-long
        # recordings; this is the separate case of something pathological.
        secs = rec.get("duration_seconds")
        if (cfg.max_ingest_seconds and isinstance(secs, (int, float))
                and secs > cfg.max_ingest_seconds):
            log(f"  ⊘ {stem}: {secs / 60:.0f} min exceeds the "
                f"{cfg.max_ingest_seconds / 60:.0f} min ingest limit — not downloaded")
            # Ledger it so this is not re-evaluated every 15 minutes. Delete the
            # line to reconsider it after raising the limit.
            append_seen(vault, rec["id"], f"{stem} (skipped: too long)")
            notify(cfg, f"Skipped a {secs / 60:.0f}-minute recording on "
                        f"{host_id()} — over the ingest limit, not downloaded.",
                   log=log, key="too-long")
            skipped += 1
            continue

        dt = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00"))
        if (vault / "inbox" / dt.strftime("%Y/%m") / f"{stem}.json").exists():
            log(f"  = {stem} already in the vault (another host) — recording locally")
            append_seen(vault, rec["id"], stem)
            skipped += 1
            continue
        try:
            res = ingest_one(rec, vault, fetcher, log)
        except FetcherError as e:
            # Do NOT add to the ledger — an unfetched recording must be retried.
            log(f"  ✗ {rec['id']}: {e}")
            failed += 1
            if e.code == F_AUTH:
                log("    session died mid-pass; stopping")
                break
            continue
        append_seen(vault, rec["id"], res["stem"])
        if not args.no_push and not git.commit_push(f"ingest {res['stem']}"):
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
    return EXIT_PARTIAL if (failed or unpushed) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
