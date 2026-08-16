"""The bearer harvest picks the workspace token, never the refresh token.

Regression for the outage of 2026-08-06 → 2026-08-16: ingest fetched nothing
for ten days and reported it as `upstream/unimplemented — re-run recon`, which
is a diagnosis pointing at Plaud rather than at us.

Both halves of that were ours. `_bearer()` harvested the first `Authorization:
Bearer` header seen on any api.plaud.ai request, and on a page load where the
cached 24h workspace token had gone stale, the *first* request is the token
exchange — `POST /user-app/auth/workspace/refresh/<ws_id>`, carrying the ~30-day
refresh token. Sending that to `/file/simple/web` earns exactly the message the
logs carried for ten days: `status=-3901 "token type does not match parse mode"`.

Nothing upstream had changed, and two things said so at the time: a single tick
failed this way on 2026-07-29 and the next tick succeeded untouched, and a live
capture showed the two tokens decoding to visibly different lifetimes. Recon was
never stale; the harvest was just indiscriminate.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pw = _load("atticus_plaud_web", "ingest/plaud_web.py")


# --- which requests are off-limits for harvesting -------------------------

@pytest.mark.parametrize("url", [
    # The exact request observed carrying the refresh token on 2026-08-16.
    "https://api.plaud.ai/user-app/auth/workspace/refresh/ws_fmkfebtesU",
    # The mint call named in PlaudAPI's docstring, same family.
    "https://api.plaud.ai/user-app/auth/workspace/token/ws_fmkfebtesU",
    "https://api.plaud.ai/user-app/auth/anything?x=1",
])
def test_token_exchange_requests_are_never_harvested(url):
    assert pw._is_token_exchange(url)


@pytest.mark.parametrize("url", [
    # Every one of these was observed carrying the *workspace* token, and one
    # of them is a near-miss on the exclusion prefix: /user-app/profile/ is
    # real data and must stay harvestable.
    "https://api.plaud.ai/user-app/profile/workspace/me",
    "https://api.plaud.ai/file/simple/web?skip=0&limit=100",
    "https://api.plaud.ai/user/me",
    "https://api.plaud.ai/device/list",
    "https://api.plaud.ai/filetag/",
])
def test_data_requests_stay_harvestable(url):
    assert not pw._is_token_exchange(url)


def test_the_endpoints_we_actually_call_are_harvestable():
    """Guards against an exclusion prefix widening until it eats our own calls."""
    for url in (pw.EP_LIST, pw.EP_ME):
        assert not pw._is_token_exchange(url), url


# --- the harvest itself, against a replay of the observed request order ---

class _FakeRequest:
    def __init__(self, url, token):
        self.url = url
        self.headers = {"authorization": f"Bearer {token}"}


class _FakeContext:
    """Replays a page load's requests to whatever listener _bearer() attaches."""

    def __init__(self, requests):
        self._requests = requests
        self._listeners = []
        self.pages = []

    def on(self, event, fn):
        assert event == "request"
        self._listeners.append(fn)

    def remove_listener(self, event, fn):
        self._listeners.remove(fn)

    def new_page(self):
        return _FakePage(self)

    def _fire(self):
        for r in self._requests:
            for fn in list(self._listeners):
                fn(r)


class _FakePage:
    def __init__(self, ctx):
        self._ctx = ctx

    def goto(self, url, wait_until=None):
        self._ctx._fire()

    def wait_for_timeout(self, ms):
        pass


# The order observed live on 2026-08-16. The refresh POST comes first, which is
# the whole trap: anything that takes the first bearer takes the wrong one.
OBSERVED_ORDER = [
    ("https://api.plaud.ai/user-app/auth/workspace/refresh/ws_fmkfebtesU", "REFRESH"),
    ("https://api.plaud.ai/filetag/", "WORKSPACE"),
    ("https://api.plaud.ai/user-app/profile/workspace/me", "WORKSPACE"),
    ("https://api.plaud.ai/file/simple/web", "WORKSPACE"),
]


def test_harvest_skips_the_refresh_token_and_takes_the_workspace_token():
    ctx = _FakeContext([_FakeRequest(u, t) for u, t in OBSERVED_ORDER])
    api = pw.PlaudAPI(ctx)
    assert api._bearer() == "Bearer WORKSPACE"


def test_harvest_raises_auth_when_only_the_exchange_is_seen():
    """A page load that never gets past the token dance is a dead session, and
    must say so — not silently cache the refresh token and fail downstream with
    an upstream-shaped error, which is what produced the ten-day outage."""
    ctx = _FakeContext([_FakeRequest(OBSERVED_ORDER[0][0], "REFRESH")])
    api = pw.PlaudAPI(ctx)
    with pytest.raises(pw.AuthError):
        api._bearer()
