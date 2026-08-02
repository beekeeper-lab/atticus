#!/usr/bin/env python3
"""Console entry points.

Thin wrappers so a packaged install exposes `atticus-ingest`, `atticus-process`
and `atticus-doctor` rather than requiring people to know where the scripts
live. The logic stays in `processor/` and `ingest/`.
"""
import os
import shutil
import subprocess
import json
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

class _TokenModeChecked(Exception):
    """Control flow, not an error: the token-mode branch has already reported."""


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
        # Token mode (ATTICUS_CLAUDE_TOKEN_FILE, #68) retires the 8-hour cycle
        # entirely: the agent authenticates from a long-lived setup-token and
        # the operator's own credential is never bound into the sandbox. What
        # is worth checking then is that the token FILE is present and sane;
        # its ~1-year expiry is not readable from the opaque token, so the
        # signal when it dies is the run-failure path, which names the re-mint.
        token_file = str(getattr(cfg, "claude_token_file", "") or "").strip()
        if token_file:
            tp = Path(token_file).expanduser()
            try:
                tok = tp.read_text().strip()
            except OSError:
                d.b(f"ATTICUS_CLAUDE_TOKEN_FILE is set but {tp} is unreadable — "
                    f"every agent run will refuse. Re-mint with "
                    f"`claude setup-token` into that file (0600).")
                tok = ""
            if tok and "\n" not in tok:
                d.ok("agent auth: long-lived token (claude setup-token)")
            elif tok:
                d.b(f"{tp} is not a single-line token — every agent run will refuse")
            raise _TokenModeChecked
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
    except _TokenModeChecked:
        pass                       # token mode already reported above
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


def usage_main() -> int:
    """`atticus-usage` — what this month consumed, money and quota kept apart.

    The two halves are reported separately and labelled, because they are not the
    same kind of thing: the api section is money that left an account and is
    bounded by a budget; the subscription section is rate-limit quota against the
    operator's Claude plan, where the dollar figure is the CLI's imputed estimate
    and is useful only for comparing runs against each other.
    """
    import argparse
    ap = argparse.ArgumentParser(prog="atticus-usage", description=usage_main.__doc__)
    ap.add_argument("--month", help="YYYY-MM (default: this UTC month)")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    ap.add_argument("--by-recording", action="store_true",
                    help="one line per recording instead of totals")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent / "processor"))
    from config import Config
    import usage as u

    cfg = Config()
    month = args.month or u.month_key()
    summary = u.summarise(cfg.vault, month)
    state = u.budget_state(cfg.vault, cfg)

    if args.json:
        print(json.dumps({"summary": summary, "budget": state}, indent=2))
        return 0

    print(f"\nAtticus usage — {month}   (vault: {cfg.vault})\n")

    print("  REAL MONEY (OpenAI API)")
    if not summary["api"]:
        print("    nothing recorded")
    for kind, row in sorted(summary["api"].items()):
        extra = f", {row['seconds'] / 60:.1f} min audio" if row["seconds"] else ""
        print(f"    {kind:<14} {row['calls']:>4} call(s)  ${row['usd']:.4f}{extra}")
    print(f"    {'TOTAL':<14} {'':>4}          ${summary['api_total_usd']:.4f}")
    if state["enabled"]:
        pct = (state["spent_usd"] / state["budget_usd"] * 100) if state["budget_usd"] else 0
        bar = "#" * min(30, int(pct / 100 * 30))
        flag = "  ** EXHAUSTED — transcription is stopped **" if state["exhausted"] else ""
        print(f"    budget         ${state['spent_usd']:.4f} of "
              f"${state['budget_usd']:.2f}  ({pct:.1f}%) [{bar:<30}]{flag}")
    else:
        print("    budget         disabled (ATTICUS_API_BUDGET_USD=0)")

    print("\n  SUBSCRIPTION (Claude plan — quota, not billed per token)")
    if not summary["subscription"]:
        print("    nothing recorded")
    for model, row in sorted(summary["subscription"].items()):
        print(f"    {model}")
        print(f"      {row['calls']} run(s), {row['input_tokens']:,} in / "
              f"{row['output_tokens']:,} out tokens")
        print(f"      cache: {row['cache_read_tokens']:,} read / "
              f"{row['cache_write_tokens']:,} written"
              + (f", {row['web_searches']} web request(s)" if row["web_searches"] else ""))
        print(f"      ~${row['imputed_usd']:.4f} imputed (NOT a charge — "
              f"subscription usage)")

    if args.by_recording:
        print("\n  BY RECORDING")
        per = {}
        for e in u.load(cfg.vault, month):
            if not e.get("stem"):
                continue
            row = per.setdefault(e["stem"], {"api": 0.0, "imputed": 0.0, "tok": 0})
            if e.get("billing") == u.API:
                row["api"] += e.get("usd", 0.0)
            else:
                row["imputed"] += e.get("usd", 0.0)
                row["tok"] += int(e.get("output_tokens") or 0)
        for stem, row in sorted(per.items()):
            print(f"    {stem}  api ${row['api']:.4f}  "
                  f"imputed ${row['imputed']:.4f}  {row['tok']:,} out-tok")

    print()
    return 0
