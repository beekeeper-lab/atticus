#!/usr/bin/env python3
"""One-time recon against Plaud Web.

The official CLI is paywalled (see ADR-002). This script watches the network
traffic of a normal, logged-in Plaud Web session and reports which API
endpoints back the recording list and the audio download. Its output is the
input to writing the real fetcher.

    ./plaud_discover.py                 # full run: login, browse, report
    ./plaud_discover.py --headless      # reuse an existing session, no window

It reads nothing and uploads nothing. It records URLs, methods, status codes,
and small response samples to a local report.

SECRETS: bearer tokens, cookies and passwords are redacted before anything is
written to disk. Only the *shape* of an auth header is reported (scheme, length,
first six characters) so the fetcher can be written against it.

What to do:
  1. Run it. A browser opens on Plaud Web.
  2. Log in.
  3. Click into the recordings list. Open one recording. Export/download its
     original audio. The point is to make the app perform the calls we need to
     replicate — if you don't click it, we don't see it.
  4. Close the browser window (or wait for the timeout).
  5. Hand me the report path.

If the account has no recordings yet, import any MP3 through the web UI first —
that exercises the same list and download paths without needing the device.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit(
        "playwright not importable.\n"
        "Run with the fetchers venv:\n"
        "  ~/.local/share/claude-fetchers/venv/bin/python plaud_discover.py"
    )

SITE = "plaud"
SESSION_ROOT = Path.home() / ".local/share/claude-fetchers/sessions"
START_URL = "https://web.plaud.ai/"
DEFAULT_TIMEOUT_S = 900

# Header names whose values must never be written to disk.
SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token",
    "x-access-token", "token", "authentication", "proxy-authorization",
    "x-csrf-token", "x-session-token", "refresh-token",
}

# Response body keys that commonly carry credentials.
SENSITIVE_KEYS = re.compile(
    r"(token|secret|password|passwd|authorization|credential|signature|api[-_]?key)",
    re.I,
)

# Endpoints we specifically care about reproducing.
INTERESTING = re.compile(
    r"(file|record|audio|media|note|transcript|list|library|download|mp3|wav|presign)",
    re.I,
)

STATIC = re.compile(
    r"\.(js|mjs|css|png|jpe?g|gif|svg|webp|woff2?|ttf|eot|ico|map)(\?|$)", re.I
)


def redact_headers(headers):
    """Return headers with sensitive values replaced by a shape description."""
    out = {}
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk in SENSITIVE_HEADERS:
            scheme = v.split(" ", 1)[0] if " " in v else "(none)"
            out[k] = f"<REDACTED scheme={scheme} len={len(v)} prefix={v[:6]!r}>"
        else:
            out[k] = v
    return out


def redact_json(obj, depth=0):
    """Recursively blank values whose keys look credential-ish. Truncate bulk."""
    if depth > 6:
        return "<max-depth>"
    if isinstance(obj, dict):
        out = {}
        for k, v in list(obj.items())[:40]:
            if SENSITIVE_KEYS.search(str(k)):
                out[k] = f"<REDACTED len={len(str(v))}>"
            else:
                out[k] = redact_json(v, depth + 1)
        if len(obj) > 40:
            out["…"] = f"{len(obj) - 40} more keys"
        return out
    if isinstance(obj, list):
        sample = [redact_json(x, depth + 1) for x in obj[:3]]
        if len(obj) > 3:
            sample.append(f"<{len(obj) - 3} more items>")
        return sample
    if isinstance(obj, str):
        # Presigned download URLs carry the signature in the query string and
        # arrive under innocuous keys ("presigned_url", "url", "src"), so the
        # key-name check above never fires. Scrub by value, not by key.
        if obj.startswith("http") and "?" in obj:
            return strip_query_secrets(obj)
        # A long opaque string is very likely a token.
        if len(obj) > 300:
            return f"<str len={len(obj)} head={obj[:80]!r}>"
        return obj
    return obj


def strip_query_secrets(url):
    """Presigned URLs carry signatures in the query string. Keep shape, drop value."""
    if "?" not in url:
        return url
    base, q = url.split("?", 1)
    parts = []
    for kv in q.split("&"):
        k = kv.split("=", 1)[0]
        if SENSITIVE_KEYS.search(k) or k.lower() in {
            "x-amz-signature", "x-amz-credential", "sig", "sign", "auth",
        }:
            parts.append(f"{k}=<REDACTED>")
        else:
            parts.append(kv if len(kv) < 120 else f"{k}=<len {len(kv)}>")
    return base + "?" + "&".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true",
                    help="reuse the stored session without opening a window")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                    help=f"seconds to observe (default {DEFAULT_TIMEOUT_S})")
    ap.add_argument("-o", "--output", default=None, help="report path")
    args = ap.parse_args()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = Path(args.output or f"plaud-discovery-{stamp}.json")

    session_dir = SESSION_ROOT / SITE
    session_dir.mkdir(parents=True, exist_ok=True)

    calls = []
    seen = set()

    def on_response(resp):
        try:
            req = resp.request
            url = resp.url
            if req.resource_type in ("image", "font", "stylesheet", "media"):
                return
            if STATIC.search(url):
                return
            if not url.startswith("http"):
                return

            key = (req.method, url.split("?")[0])
            interesting = bool(INTERESTING.search(url))
            # Record every distinct endpoint once; record interesting ones always.
            if key in seen and not interesting:
                return
            seen.add(key)

            entry = {
                "method": req.method,
                "url": strip_query_secrets(url),
                "status": resp.status,
                "resource_type": req.resource_type,
                "interesting": interesting,
                "request_headers": redact_headers(req.headers),
                "content_type": (resp.headers or {}).get("content-type", ""),
            }

            if req.method in ("POST", "PUT", "PATCH"):
                try:
                    body = req.post_data
                    if body:
                        entry["request_body"] = (
                            redact_json(json.loads(body))
                            if body.lstrip().startswith(("{", "["))
                            else f"<{len(body)} bytes non-json>"
                        )
                except Exception:
                    pass

            ctype = entry["content_type"]
            if "json" in ctype:
                try:
                    entry["response_sample"] = redact_json(resp.json())
                except Exception:
                    entry["response_sample"] = "<unparseable json>"
            elif "audio" in ctype or "octet-stream" in ctype:
                size = (resp.headers or {}).get("content-length", "?")
                entry["response_sample"] = f"<binary {size} bytes>"
                # octet-stream alone is not enough — the desktop-updater
                # manifest (client-download.plaud.ai/.../latest.yml) matched on
                # the first run and produced a false "audio captured" report.
                # Require an audio content-type, or a plausibly large body.
                looks_audio = "audio" in ctype or (
                    str(size).isdigit() and int(size) > 100_000
                )
                if looks_audio and "client-download.plaud.ai" not in url:
                    entry["AUDIO_DOWNLOAD"] = True

            calls.append(entry)
            flag = " ★" if interesting else ""
            print(f"  {resp.status} {req.method:6} {url.split('?')[0][:96]}{flag}",
                  flush=True)
        except Exception:
            pass

    print(f"Session dir: {session_dir}")
    print(f"Report will be written to: {out_path.resolve()}\n")

    p = sync_playwright().start()
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(session_dir),
        headless=args.headless,
        viewport={"width": 1500, "height": 950},
        accept_downloads=True,
    )
    try:
        ctx.on("response", on_response)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")

        print("Browser open on Plaud Web.\n")
        print("  1. Log in (free Starter account is fine).")
        print("  2. Open the recordings list.")
        print("  3. Open a recording.")
        print("  4. Export / download its ORIGINAL AUDIO — this is the")
        print("     call we most need to see. If you skip it, the report")
        print("     will not contain the download path.")
        print("  5. Close the browser window when done.\n")
        print(f"Observing for up to {args.timeout}s. Traffic:\n", flush=True)

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if not ctx.pages:
                print("\nBrowser closed.")
                break
            time.sleep(1)
        else:
            print("\nTimeout reached.")

    finally:
        try:
            ctx.close()
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass

    interesting = [c for c in calls if c.get("interesting")]
    audio = [c for c in calls if c.get("AUDIO_DOWNLOAD")]
    auth_styles = sorted({
        h for c in calls for h in c["request_headers"]
        if h.lower() in SENSITIVE_HEADERS
    })

    report = {
        "captured_at": stamp,
        "start_url": START_URL,
        "totals": {
            "calls": len(calls),
            "interesting": len(interesting),
            "audio_downloads": len(audio),
        },
        "auth_header_names_seen": auth_styles,
        "interesting_calls": interesting,
        "audio_calls": audio,
        "all_calls": calls,
    }
    out_path.write_text(json.dumps(report, indent=2, default=str))

    print(f"\n{'─' * 60}")
    print(f"  {len(calls)} calls, {len(interesting)} interesting, "
          f"{len(audio)} audio download(s)")
    print(f"  auth headers seen: {auth_styles or 'NONE — likely cookie-based'}")
    print(f"  report: {out_path.resolve()}")
    print(f"{'─' * 60}")
    if not audio:
        print("\n⚠  No audio download captured. Re-run and make sure you actually")
        print("   export the original audio — that is the call the fetcher needs.")


if __name__ == "__main__":
    main()
