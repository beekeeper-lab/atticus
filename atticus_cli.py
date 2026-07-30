#!/usr/bin/env python3
"""Console entry points.

Thin wrappers so a packaged install exposes `atticus-ingest`, `atticus-process`
and `atticus-doctor` rather than requiring people to know where the scripts
live. The logic stays in `processor/` and `ingest/`.
"""
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
for sub in ("processor", "ingest"):
    p = str(REPO / sub)
    if p not in sys.path:
        sys.path.insert(0, p)


def ingest_main():
    from poller import _guarded
    return _guarded()


def process_main():
    from pipeline import _guarded
    return _guarded()


# ---------------------------------------------------------------------------
#  doctor
# ---------------------------------------------------------------------------

OK, WARN, BAD = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m"


class Doctor:
    """Checks every precondition, and distinguishes the failures.

    The installer's preflight runs in your shell, which is exactly why it once
    reported `✓ claude 2.1.220` for a unit that could not find claude at all.
    Doctor checks what the *services* see wherever it can.
    """

    def __init__(self):
        self.bad = self.warn = 0

    def ok(self, msg):
        print(f"  {OK} {msg}")

    def w(self, msg):
        self.warn += 1
        print(f"  {WARN} {msg}")

    def b(self, msg):
        self.bad += 1
        print(f"  {BAD} {msg}")

    def section(self, name):
        print(f"\n{name}")


def doctor_main():
    d = Doctor()
    print("atticus doctor")

    d.section("Runtime")
    if sys.version_info >= (3, 11):  # noqa: UP036 — doctor reports it regardless
        d.ok(f"python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        d.b(f"python {sys.version_info.major}.{sys.version_info.minor} — need 3.11+")
    for tool, why, fatal in (("git", "vault access", True),
                             ("ffprobe", "audio verification and truncation", False),
                             ("ffmpeg", "truncating over-long recordings", False),
                             ("bwrap", "CONTAINING THE AGENT", True)):
        if shutil.which(tool):
            d.ok(f"{tool} ({why})")
        elif fatal:
            d.b(f"{tool} MISSING — {why}")
        else:
            d.w(f"{tool} missing — {why} will be skipped")

    d.section("Configuration")
    try:
        from config import Config
        cfg = Config()
    except Exception as e:
        d.b(f"config unreadable: {e}")
        return 2
    d.ok(f"vault path {cfg.vault}")
    if not cfg.vault.is_dir():
        d.b("vault directory does not exist — run ops/init-vault.sh")
    elif not (cfg.vault / ".git").exists():
        d.b("vault is not a git repository")
    else:
        d.ok("vault is a git repository")

    if getattr(cfg, "sandbox", True):
        d.ok("agent sandbox ENABLED")
    else:
        d.b("ATTICUS_SANDBOX is off — the agent can read every credential here")

    # The agent authenticates with the operator's own Claude Code credential, and
    # it is mounted read-only so the CLI cannot refresh an expired access token
    # from inside the sandbox — it exits 1 with EMPTY stdout and stderr, which
    # says nothing. Every recording then fails until a human logs in. Check it
    # here, where the answer is cheap, rather than discovering it on a recording.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "processor"))
        from execute import credential_expiry
        expired, when = credential_expiry()
        if when is None:
            d.w("cannot read the Claude Code credential expiry — the agent may "
                "fail to authenticate")
        elif expired:
            d.b(f"Claude Code credential EXPIRED at "
                f"{when.isoformat(timespec='seconds')} — every agent run will "
                f"fail. Run `claude` interactively to renew it.")
        else:
            hrs = (when - datetime.now(UTC)).total_seconds() / 3600
            msg = f"Claude Code credential valid for {hrs:.1f}h"
            d.ok(msg) if hrs > 1 else d.w(msg + " — renew it soon")
    except Exception as e:                          # noqa: BLE001
        # Never let a diagnostic crash the diagnostics — but say what happened,
        # rather than swallowing it. A bare ImportError catch here hid a NameError
        # and the check silently printed nothing at all.
        d.w(f"credential check failed: {type(e).__name__}: {e}")

    if cfg.notify_url:
        d.ok("failure notifications configured")
    else:
        d.b("ATTICUS_NOTIFY_URL unset — a dead pipeline will be SILENT")

    if cfg.wake_phrase:
        d.ok(f"wake phrase {cfg.wake_phrase!r}"
             + (f" (+{len(cfg.wake_aliases)} alias)" if cfg.wake_aliases else ""))
    else:
        d.w("no wake phrase — EVERY transcript will be executed")

    if getattr(cfg, "max_budget_usd", ""):
        d.ok(f"spend ceiling ${cfg.max_budget_usd} per recording")
    else:
        d.w("no spend ceiling set")

    d.section("Credentials (presence only — values are never shown)")
    ai_env = Path.home() / ".config/ai/env"
    if ai_env.is_file():
        mode = oct(ai_env.stat().st_mode)[-3:]
        (d.ok if mode == "600" else d.w)(f"{ai_env} (mode {mode})")
        try:
            _ = cfg.openai_key
            d.ok("OPENAI_API_KEY present and well-formed")
        except Exception as e:
            d.b(str(e))
    else:
        d.b(f"{ai_env} not found")

    d.section("Vault remote")
    if (cfg.vault / ".git").exists():
        env = {**os.environ,
               "GIT_SSH_COMMAND": f"ssh -F {Path.home()}/.ssh/config"}
        try:
            r = subprocess.run(["git", "-C", str(cfg.vault), "push", "--dry-run"],
                               capture_output=True, text=True, env=env, timeout=60)
        except subprocess.TimeoutExpired:
            # A hung ssh (unreachable remote) used to crash doctor with a
            # traceback instead of reporting the fault it exists to catch.
            r = None
            d.b("push check hung — remote unreachable?")
        if r is None:
            pass
        elif r.returncode == 0:
            d.ok("vault push authenticates")
        else:
            d.b("cannot push to the vault — work would commit locally and "
                "never reach the other half")
        ahead = subprocess.run(["git", "-C", str(cfg.vault), "rev-list",
                                "--count", "@{u}..HEAD"],
                               capture_output=True, text=True)
        if ahead.returncode != 0:
            # No upstream configured: rev-list fails, and the old code read that
            # as "0 ahead" and printed a false all-clear.
            d.w("no upstream configured — cannot tell if commits are unpushed")
        else:
            n = (ahead.stdout or "0").strip()
            if n.isdigit() and int(n):
                d.b(f"{n} local commit(s) NOT pushed — downstream cannot see them")
            else:
                d.ok("vault is level with its remote")

    d.section("Queue")
    try:
        from vault import load_records
        bad = []
        recs = load_records(cfg.vault, on_bad=lambda p, e: bad.append(p))
        stuck = [r for r in recs if r.status not in ("published", "failed")]
        d.ok(f"{len(recs)} record(s); {len(stuck)} in flight")
        if bad:
            d.b(f"{len(bad)} MALFORMED record(s) — not processable")
    except Exception as e:
        d.w(f"could not read the queue: {e}")

    print()
    if d.bad:
        print(f"{d.bad} problem(s), {d.warn} warning(s)")
        return 1
    print(f"healthy ({d.warn} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(doctor_main())
