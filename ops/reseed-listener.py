#!/usr/bin/env python3
"""WarDog side of the "Re-login now" button (ADR-013).

Forge's Plaud-session pushes carry an ntfy action button that POSTs `reseed` to
ATTICUS_RESEED_TOPIC_URL. This listener runs on a machine WITH A DISPLAY,
subscribes to that topic (outbound only — no inbound port), and when it sees
exactly `reseed` runs ops/reseed-plaud.sh, which opens the Plaud login, then
verifies, copies the session to Forge and restarts ingest.

Guards, because the topic name is the only secret:
  * only the exact body `reseed` acts; anything else is ignored
  * one login at a time, and a cooldown so a double-tap or replay cannot stack
    browser windows
  * the result is pushed to ATTICUS_NOTIFY_URL (if set) so the phone hears back

Stdlib only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COOLDOWN_S = 600
RECONNECT_MAX_S = 120


def log(msg: str) -> None:
    print(msg, flush=True)


def load_env(path: Path) -> dict:
    out: dict = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def push(url: str, title: str, text: str, tags: str) -> None:
    if not url.lower().startswith(("http://", "https://")):
        return
    try:
        req = urllib.request.Request(  # noqa: S310 — scheme checked above
            url, data=text.encode(), method="POST",
            headers={"Title": title.encode("ascii", "replace").decode(),
                     "Tags": tags})
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310
            pass
    except Exception as e:                                       # noqa: BLE001
        log(f"push failed: {type(e).__name__}: {e}")


def run_reseed(notify_url: str, host: str) -> None:
    log("reseed requested — opening the Plaud login")
    push(notify_url, "Atticus - re-login started",
         "Log in on WarDog, then close the window.", "hourglass")
    r = subprocess.run([str(REPO / "ops" / "reseed-plaud.sh"), "--host", host],
                       capture_output=True, text=True)
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-6:])
    log(tail)
    if r.returncode == 0:
        push(notify_url, "Atticus - re-login done",
             "Session copied to Forge and ingest restarted.", "white_check_mark")
    else:
        push(notify_url, "Atticus - re-login FAILED",
             f"reseed-plaud.sh exited {r.returncode}:\n{tail}", "x")


def listen(topic: str, notify_url: str, host: str) -> None:
    last_run = 0.0
    backoff = 2
    while True:
        try:
            with urllib.request.urlopen(  # noqa: S310
                    topic.rstrip("/") + "/json", timeout=90) as resp:
                log(f"subscribed to {topic.split('/')[2]}/…")
                backoff = 2
                for raw in resp:
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        continue
                    if ev.get("event") != "message":
                        continue
                    if (ev.get("message") or "").strip() != "reseed":
                        continue
                    now = time.monotonic()
                    if now - last_run < COOLDOWN_S:
                        log("reseed ignored — within cooldown")
                        continue
                    last_run = now
                    run_reseed(notify_url, host)
        except Exception as e:                                   # noqa: BLE001
            # ntfy closes idle streams; a timeout here is routine, not an error.
            log(f"stream ended: {type(e).__name__}: {e} — reconnecting in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX_S)


def main() -> int:
    env = {**load_env(REPO / "ops" / ".env"), **os.environ}
    topic = (env.get("ATTICUS_RESEED_TOPIC_URL") or "").strip()
    if not topic.lower().startswith(("http://", "https://")):
        log("ATTICUS_RESEED_TOPIC_URL is unset in ops/.env — nothing to listen to")
        return 2
    host = env.get("ATTICUS_INGEST_HOST", "forge")
    listen(topic, (env.get("ATTICUS_NOTIFY_URL") or "").strip(), host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
