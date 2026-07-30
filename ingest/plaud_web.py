#!/usr/bin/env python3
"""Plaud Web fetcher — the CLI replacement (ADR-002).

    plaud_web.py login                    seed the session (interactive, once)
    plaud_web.py whoami [--json]          session health check
    plaud_web.py list [--days N] [--json] recordings, newest first
    plaud_web.py audio <id> -o <path>     download original audio

STATUS: the contract above is committed and implemented. The endpoint layer
(class PlaudAPI) is STUBBED — Plaud gates audio import behind a bound device,
so the web API could not be observed before the hardware arrived. Run
`plaud_discover.py` (T-06), then fill in the four TODO(T-06) methods. Nothing
outside PlaudAPI should need to change.

Exit codes — ingest depends on these:
    0  success
    2  usage error
    3  session expired / auth failed   ← alarm loudly, needs re-seeding
    4  network or transient upstream error
    5  unexpected: upstream probably changed, re-run recon
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

SITE = "plaud"
# `or`, not a get() default. systemd's EnvironmentFile sets a blank line as the
# EMPTY STRING, not as unset — and ops/.env deliberately ships
# `PLAUD_SESSION_ROOT=` blank so the default applies. With get(k, default) the
# empty string wins, Path("") / "plaud" resolves to the *relative* path
# `plaud`, and every pass dies with "no session at plaud" while an interactive
# run (where the var is truly absent) works perfectly. Config.g() already uses
# `or` for exactly this reason.
SESSION_ROOT = Path(
    os.environ.get("PLAUD_SESSION_ROOT")
    or Path.home() / ".local/share/claude-fetchers/sessions"
)
SESSION_DIR = SESSION_ROOT / SITE
WEB_ORIGIN = "https://web.plaud.ai"

EXIT_OK, EXIT_USAGE, EXIT_AUTH, EXIT_NET, EXIT_UNEXPECTED = 0, 2, 3, 4, 5


class AuthError(RuntimeError):
    """Session missing, expired, or rejected."""


class TransientError(RuntimeError):
    """Network blip or upstream 5xx. Safe to retry next tick."""


class UpstreamChanged(RuntimeError):
    """Response did not look the way we expect. Recon is stale."""


# ─────────────────────────────────────────────────────────────────────────
#  Endpoint layer — derived from recon (T-06, 2026-07-28)
# ─────────────────────────────────────────────────────────────────────────

API = "https://api.plaud.ai"
EP_LIST = f"{API}/file/simple/web"
EP_ME = f"{API}/user/me"
PAGE_SIZE = 100
HARVEST_TIMEOUT_S = 60
# Playwright's request default is 30s for the WHOLE request — a large MP3 on a
# slow link blows past it and raises a playwright TimeoutError. Give the audio
# download a generous ceiling; the ingest poller runs us with its own outer
# subprocess timeout (fetcher_timeout * 2), so this only needs to be larger
# than any plausible single download.
DOWNLOAD_TIMEOUT_MS = 10 * 60 * 1000


class PlaudAPI:
    """Adapter over Plaud's web API.

    Auth (E1, answered): `Authorization: Bearer <workspace_token>`. The token is
    workspace-scoped, obtained by the web app via
    POST /user-app/auth/workspace/token/<ws_id>, and lives 24h with a 30-day
    refresh token.

    Rather than reimplement that dance, we let the app do it and harvest the
    header off a live request. That means **the browser handles token refresh
    for us** — so the practical session lifetime is the 30-day refresh window,
    not 24 hours.

    Transport (E2, answered): Playwright's APIRequestContext. Shares the
    browser's cookie jar and TLS fingerprint, and needs no extra dependency —
    `requests` is not in the fetchers venv.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self._page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self._token = None

    # -- E1: harvest the bearer off a live request ------------------------
    def _bearer(self):
        if self._token:
            return self._token
        import time as _t
        found = {}

        def on_request(req):
            if found or "api.plaud.ai" not in req.url:
                return
            auth = (req.headers or {}).get("authorization")
            if auth and auth.lower().startswith("bearer "):
                found["v"] = auth

        self.ctx.on("request", on_request)
        try:
            self._page.goto(WEB_ORIGIN, wait_until="domcontentloaded")
            deadline = _t.time() + HARVEST_TIMEOUT_S
            while _t.time() < deadline and "v" not in found:
                self._page.wait_for_timeout(250)
        finally:
            try:
                self.ctx.remove_listener("request", on_request)
            except Exception:
                pass

        if "v" not in found:
            raise AuthError(
                "no Authorization header seen — session expired or login "
                "changed. Re-seed with `plaud_web.py login`."
            )
        self._token = found["v"]
        return self._token

    def _get(self, url, params=None):
        resp = self.ctx.request.get(
            url, params=params or {},
            headers={"Authorization": self._bearer(), "Accept": "application/json"},
        )
        if resp.status in (401, 403):
            raise AuthError(f"{resp.status} from {url} — session rejected")
        if resp.status >= 500:
            raise TransientError(f"{resp.status} from {url}")
        if resp.status != 200:
            raise UpstreamChanged(f"{resp.status} from {url}")
        try:
            body = resp.json()
        except Exception as e:
            raise UpstreamChanged(f"non-JSON from {url}: {e}") from e
        # Plaud signals application-level errors in-band with HTTP 200.
        if isinstance(body, dict) and body.get("status") not in (0, None):
            raise UpstreamChanged(
                f"api status={body.get('status')} msg={body.get('msg')!r}"
            )
        return body

    @staticmethod
    def _normalize(rec):
        """Plaud's vocabulary stops here. Nothing downstream sees these names."""
        ms = rec.get("start_time") or rec.get("version_ms")
        if ms:
            created = datetime.fromtimestamp(ms / 1000, tz=UTC)
        else:
            # Neither timestamp present. Falling back to 0 → 1970, which the
            # `since` window then excludes on every pass — the recording would
            # vanish forever with no log, error, or ledger entry. Keep it with an
            # ingest-time sentinel so it survives the filter and gets pulled, and
            # shout so the operator knows recon may be stale.
            created = datetime.now(UTC)
            print(f"! recording {rec.get('id')!r} has no start_time/version_ms — "
                  f"using an ingest-time sentinel; recon may be stale",
                  file=sys.stderr)
        return {
            "id": rec["id"],
            "name": rec.get("filename") or rec.get("fullname") or "",
            # Second precision — this feeds vault filenames, so no microseconds.
            "created_at": created.replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"),
            "duration_seconds": round((rec.get("duration") or 0) / 1000),
            # Operationally significant extras:
            "ori_ready": rec.get("ori_ready"),      # NOT a download gate — see audio_url()
            "filetype": rec.get("filetype"),        # "" on device recordings
            "filesize": rec.get("filesize"),
            "md5": rec.get("file_md5"),
            "serial_number": rec.get("serial_number"),
            "scene": rec.get("scene"),
        }

    @staticmethod
    def _is_demo(rec) -> bool:
        """Plaud seeds every new account with three marketing files — a
        welcome clip, a how-to, and 81 minutes of a Steve Jobs conversation.

        They are indistinguishable from real recordings by duration or name,
        and ingesting them would burn transcription budget on vendor content.
        `serial_number` is the reliable discriminator: real recordings carry
        the pin's serial, demos carry `welcome_*`. (`edit_from` is not
        reliable — the Steve Jobs file reports `ios`.)"""
        return str(rec.get("serial_number") or "").startswith("welcome_")

    # -- E3: list recordings (answered: skip/limit pagination) ------------
    def list_recordings(self, since=None, include_demos=False):
        out, skip, demos = [], 0, 0
        cutoff = since.isoformat().replace("+00:00", "Z") if since is not None else None
        while True:
            body = self._get(EP_LIST, {
                "skip": skip, "limit": PAGE_SIZE, "is_trash": 0,
                "sort_by": "start_time", "is_desc": "true",
            })
            if "data_file_list" not in body:
                raise UpstreamChanged("no data_file_list in response")
            batch = body["data_file_list"]
            if not batch:
                break
            for r in batch:
                if not include_demos and self._is_demo(r):
                    demos += 1
                    continue
                out.append(self._normalize(r))
            total = body.get("data_file_total")
            skip += len(batch)
            # Newest-first (is_desc), so once a page's OLDEST record predates the
            # `since` window nothing older is worth paging for. Without this we
            # walk the entire account history every pass to keep a 2-day slice.
            if cutoff is not None and self._normalize(batch[-1])["created_at"] < cutoff:
                break
            if total is not None and skip >= total:
                break
        if cutoff is not None:
            out = [r for r in out if r["created_at"] >= cutoff]
        if demos:
            print(f"(skipped {demos} Plaud demo file(s))", file=sys.stderr)
        return out

    # -- E4/E5: resolved 2026-07-29 ---------------------------------------
    def audio_url(self, recording_id, prefer_opus=False):
        """Return a presigned S3 URL for the recording's audio.

        `GET /file/temp-url/{id}` → {"temp_url": …mp3, "temp_url_opus": …opus}

        Found by extracting the API surface from the web app's own JS chunks
        rather than by guessing endpoints. Verified end to end against a real
        13-second recording: 55,952 bytes, 13.84s, 40 kbps 16 kHz mono MP3.

        Notes from that verification:

        - `temp_url_opus` is frequently **absent** even though the device
          records Opus (`fullname` ends `.opus`). Plaud transcodes server-side
          and only reliably exposes the MP3. Prefer MP3; treat Opus as a bonus.
        - **`ori_ready` is not a gate.** It is `false` on files that download
          fine, including Plaud's own demos. An earlier revision of this file
          assumed it meant "audio not yet prepared" and refused to download —
          that was wrong.
        - The URL is presigned. It carries its own credentials in the query
          string and must be fetched **without** an Authorization header.
        """
        body = self._get(f"{API}/file/temp-url/{recording_id}")
        order = ("temp_url_opus", "temp_url") if prefer_opus else ("temp_url", "temp_url_opus")
        for key in order:
            if body.get(key):
                return body[key]
        raise UpstreamChanged(
            f"no temp_url in response for {recording_id} — keys: {list(body)}")

    def download_audio(self, recording_id, dest: Path):
        """Fetch the audio to `dest`, atomically.

        Writes a .part file and renames on success, so a crash mid-download can
        never leave something the ingest ledger mistakes for a complete file.
        """
        url = self.audio_url(recording_id)
        # Presigned: sending our bearer alongside the S3 signature can be
        # rejected as a conflicting auth method. Send no Authorization header.
        resp = self.ctx.request.get(url, timeout=DOWNLOAD_TIMEOUT_MS)
        if resp.status in (401, 403):
            raise AuthError(f"{resp.status} on presigned URL — it likely expired")
        if resp.status != 200:
            raise TransientError(f"{resp.status} downloading {recording_id}")
        data = resp.body()
        if not data:
            raise TransientError(f"empty body for {recording_id}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        part.write_bytes(data)
        part.replace(dest)
        return dest

    def detail(self, recording_id):
        """`GET /file/detail/{id}` — presigned URLs for transcript, summary and
        outline. Plaud transcribes even 9-second clips, so its transcript is a
        free fallback if our own STT ever fails."""
        return self._get(f"{API}/file/detail/{recording_id}")

    # -- Session health ---------------------------------------------------
    def whoami(self):
        """Make a real authenticated call — a stale session still has cookies,
        and that is exactly the failure T-72 needs to catch."""
        body = self._get(EP_ME)
        u = body.get("data_user") or {}
        st = body.get("data_state") or {}
        return {
            "email": u.get("email"),
            "nickname": u.get("nickname"),
            "device_bound": bool(st.get("is_bind")),
            "membership": bool(st.get("is_membership")),
            "seconds_left": u.get("seconds_left"),
        }


# ─────────────────────────────────────────────────────────────────────────
#  Session handling — committed
# ─────────────────────────────────────────────────────────────────────────

def launch(headless=True):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed; use the fetchers venv", file=sys.stderr)
        sys.exit(EXIT_USAGE)

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    p = sync_playwright().start()
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(SESSION_DIR),
        headless=headless,
        viewport={"width": 1400, "height": 900},
        accept_downloads=True,
    )
    return p, ctx


def close(p, ctx):
    for fn in (ctx.close, p.stop):
        try:
            fn()
        except Exception:
            pass


def session_seeded():
    return SESSION_DIR.exists() and any(SESSION_DIR.iterdir())


# ─────────────────────────────────────────────────────────────────────────
#  Commands — committed
# ─────────────────────────────────────────────────────────────────────────

def cmd_login(args):
    p, ctx = launch(headless=False)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(WEB_ORIGIN, wait_until="domcontentloaded")
        print(f"Browser open on {WEB_ORIGIN}.")
        print("Log in, then close the window. Session persists to:")
        print(f"  {SESSION_DIR}")
        while ctx.pages:
            page.wait_for_timeout(1000)
        print("Session stored.")
        return EXIT_OK
    finally:
        close(p, ctx)


def cmd_whoami(args):
    if not session_seeded():
        raise AuthError(f"no session at {SESSION_DIR} — run `plaud_web.py login`")
    p, ctx = launch(headless=not args.headed)
    try:
        info = PlaudAPI(ctx).whoami()
        print(json.dumps(info) if args.json else f"Signed in: {info}")
        return EXIT_OK
    finally:
        close(p, ctx)


def cmd_list(args):
    if not session_seeded():
        raise AuthError(f"no session at {SESSION_DIR} — run `plaud_web.py login`")
    since = datetime.now(UTC) - timedelta(days=args.days)
    p, ctx = launch(headless=not args.headed)
    try:
        recs = PlaudAPI(ctx).list_recordings(
            since=since, include_demos=getattr(args, "include_demos", False))
    finally:
        close(p, ctx)

    if args.json:
        print(json.dumps({"recordings": recs}, indent=2))
    else:
        for r in recs:
            print(f"{r['id']:>24}  {r['created_at']}  "
                  f"{r.get('duration_seconds', '?'):>5}s  {r.get('name', '')}")
    return EXIT_OK


def cmd_audio(args):
    if not session_seeded():
        raise AuthError(f"no session at {SESSION_DIR} — run `plaud_web.py login`")
    dest = Path(args.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    p, ctx = launch(headless=not args.headed)
    try:
        PlaudAPI(ctx).download_audio(args.id, dest)
    finally:
        close(p, ctx)
    print(f"{dest}  ({dest.stat().st_size} bytes)")
    return EXIT_OK


def main():
    ap = argparse.ArgumentParser(description="Plaud Web fetcher (see ADR-002)")
    ap.add_argument("--headed", action="store_true",
                    help="show the browser (debugging)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="seed the session interactively")

    w = sub.add_parser("whoami", help="session health check")
    w.add_argument("--json", action="store_true")

    ls = sub.add_parser("list", help="list recordings")
    ls.add_argument("--days", type=int, default=2)
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--include-demos", action="store_true",
                    help="also list Plaud's seeded marketing files")

    au = sub.add_parser("audio", help="download original audio")
    au.add_argument("id")
    au.add_argument("-o", "--output", required=True)

    args = ap.parse_args()
    handler = {
        "login": cmd_login, "whoami": cmd_whoami,
        "list": cmd_list, "audio": cmd_audio,
    }[args.cmd]

    try:
        sys.exit(handler(args))
    except AuthError as e:
        print(f"auth: {e}", file=sys.stderr)
        sys.exit(EXIT_AUTH)
    except TransientError as e:
        print(f"transient: {e}", file=sys.stderr)
        sys.exit(EXIT_NET)
    except (UpstreamChanged, NotImplementedError) as e:
        print(f"upstream/unimplemented: {e}", file=sys.stderr)
        print("Run plaud_discover.py (T-06) and fill in PlaudAPI.", file=sys.stderr)
        sys.exit(EXIT_UNEXPECTED)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        # Playwright's own errors — a download/goto/launch timeout or a browser
        # crash — are outside our exception vocabulary. Left uncaught they exit
        # 1 with a traceback, breaking the documented 0/2/3/4/5 contract that
        # ingest keys off. A browser network/timeout is transient, so map it to
        # EXIT_NET and let the timer retry next tick. Anything genuinely
        # unexpected re-raises rather than masquerading as a network blip.
        try:
            from playwright.sync_api import Error as PlaywrightError
        except ImportError:
            raise
        if isinstance(e, PlaywrightError):
            print(f"transient (browser): {e}", file=sys.stderr)
            sys.exit(EXIT_NET)
        raise


if __name__ == "__main__":
    main()
