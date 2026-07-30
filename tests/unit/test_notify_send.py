"""notify()'s actual HTTP send path, which had no coverage.

The existing tests covered _ascii(), the throttle and the disabled case, but
nothing exercised the request itself — so the header set, the scheme guard and the
failure handling were all unverified. This is the transport for every alarm in a
system whose stated thesis is that silent failure is the worst failure, so "we
think the POST is well-formed" is not good enough.

Uses a real loopback HTTP server rather than a mock, so the headers are asserted
as the server actually receives them — latin-1 encoding included, which is the
bug that motivated _ascii() in the first place.
"""
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

import notify as nt
import pytest


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        _Handler.received.append({
            "path": self.path,
            "body": self.rfile.read(n).decode(),
            "headers": dict(self.headers),
        })
        self.send_response(_Handler.status)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _Handler.received = []
    _Handler.status = 200
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield types.SimpleNamespace(
        url=f"http://127.0.0.1:{srv.server_port}/atticus",
        received=_Handler.received,
        set_status=lambda s: setattr(_Handler, "status", s))
    srv.shutdown()


def _cfg(url):
    return types.SimpleNamespace(notify_url=url, alarm_throttle_hours=6)


def test_the_post_carries_body_and_ntfy_headers(server):
    assert nt.notify(_cfg(server.url), "the pipeline is stuck",
                     title="Atticus heartbeat", tags="rotating_light",
                     priority="high") is True
    assert len(server.received) == 1
    got = server.received[0]
    assert got["body"] == "the pipeline is stuck"
    assert got["headers"]["Title"] == "Atticus heartbeat"
    assert got["headers"]["Tags"] == "rotating_light"
    assert got["headers"]["Priority"] == "high"


def test_typography_in_the_title_does_not_defeat_the_alarm(server):
    """HTTP headers are latin-1: an em-dash in a title raised
    UnicodeEncodeError and took the whole alarm down. Observed, and absurd — an
    alarm must not be defeated by punctuation. The BODY keeps its typography."""
    ok = nt.notify(_cfg(server.url), "recording — truncated · 40 min",
                   title="Atticus — ingest · session dead", tags="🚨 warning")
    assert ok is True
    got = server.received[0]
    assert "—" in got["body"], "the body must keep its typography"
    got["headers"]["Title"].encode("latin-1")   # must not raise
    got["headers"]["Tags"].encode("latin-1")


def test_a_non_http_scheme_is_refused_without_sending(server):
    """cfg.notify_url comes from a file. urlopen() would happily accept file://,
    turning an alarm into a local file operation."""
    logged = []
    for bad in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)"):
        assert nt.notify(_cfg(bad), "x", log=logged.append) is False
    assert not server.received
    assert all("not http(s)" in m for m in logged)


def test_a_server_error_is_reported_not_swallowed(server):
    server.set_status(500)
    logged = []
    assert nt.notify(_cfg(server.url), "x", log=logged.append) is False
    assert any("notification failed" in m for m in logged)


def test_an_unreachable_endpoint_returns_false_and_says_why():
    logged = []
    # Port 1 on loopback: nothing listens, connection refused immediately.
    assert nt.notify(_cfg("http://127.0.0.1:1/nope"), "x",
                     log=logged.append) is False
    assert any("notification failed" in m for m in logged)


def test_no_url_configured_sends_nothing(server):
    assert nt.notify(_cfg(None), "x") is False
    assert not server.received


def test_the_throttle_suppresses_the_second_send(server, monkeypatch, tmp_path):
    """A recurring condition is rediscovered every tick; one alarm per window."""
    monkeypatch.setattr(nt, "STATE", tmp_path)
    cfg = _cfg(server.url)
    assert nt.notify(cfg, "first", key="dupe") is True
    assert nt.notify(cfg, "second", key="dupe") is False
    assert len(server.received) == 1


def test_clear_lets_the_next_alarm_through(server, monkeypatch, tmp_path):
    """Clear-on-recovery is what stops a fail/recover/fail cycle going silent for
    the rest of a six-hour window."""
    monkeypatch.setattr(nt, "STATE", tmp_path)
    cfg = _cfg(server.url)
    assert nt.notify(cfg, "first", key="dupe") is True
    nt.clear("dupe")
    assert nt.notify(cfg, "second", key="dupe") is True
    assert len(server.received) == 2
