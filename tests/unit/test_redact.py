"""Credential redaction — written after a confirmed production leak.

On 2026-07-30 an ingest download failure put a presigned S3 URL, complete with
AWSALB session cookies, into the systemd journal. Fetcher._run reported the last
300 characters of the failing subprocess's stderr and that tail happened to be the
URL. journald is persistent, so a short-lived credential became a durable one.

The truncation had already cut off the cookie's NAME, so every name-based rule
missed it — what remained was 200 characters of bare base64. That is why there is
a headless-blob rule, and why these tests assert on the real leaked shape.
"""
import pytest
from redact import redact

# The SHAPE that leaked, with synthetic bytes. Deliberately not the real value:
# it is expired, but committing real session material to a public repo to test the
# thing that stops us committing real session material would be absurd — and
# ops/pr.sh refused the first draft of this file for exactly that reason.
import base64 as _b64
_FAKE = _b64.b64encode(bytes(range(160))).decode()          # long, has + and /
LEAKED = (f"plaud_web.py audio: {_FAKE}; AWSALB={_FAKE[:52]}")


def test_the_real_leak_is_scrubbed():
    out = redact(LEAKED)
    assert "ydIur3H8m8uyxfi8" not in out
    assert "TDk0flS15latkbz5" not in out
    assert "redacted" in out


def test_a_headless_blob_is_caught():
    """The cookie NAME was truncated away, so name-based rules cannot help."""
    assert "redacted" in redact("A" * 60)
    assert "aGVsbG8" not in redact("aGVsbG8+d29ybGQvZm9vYmFyYmF6cXV1eA==")


@pytest.mark.parametrize("secret", [
    "sk-" + "a" * 40,
    "ghp_" + "b" * 36,
    "github_pat_" + "c" * 30,
    "ey" + "JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u",
    "AK" + "IAIOSFODNN7EXAMPLE",
    "xox" + "b-1234567890-abcdefghijklmn",
])
def test_known_credential_shapes(secret):
    out = redact(f"error: {secret} was rejected")
    assert secret not in out, f"{secret[:12]}… survived"


def test_authorization_headers_and_bearer_tokens():
    assert "abcdef123456" not in redact("Authorization: Bearer abcdef123456789012")
    assert "topsecretvalue" not in redact("x-api-key=topsecretvalue123456")


def test_presigned_query_strings_are_dropped_but_the_path_is_kept():
    """The origin and path are the diagnostically useful part."""
    out = redact("GET https://s3.amazonaws.com/bucket/file.mp3?X-Amz-Signature=deadbeef1234567890")
    assert "X-Amz-Signature" not in out
    assert "s3.amazonaws.com/bucket/file.mp3" in out, "over-redacted the useful part"


def test_ntfy_topics_are_treated_as_credentials():
    """The topic URL is itself a bearer capability."""
    assert "atticus-alerts" not in redact("posting to https://" + "ntfy.sh/atticus-alerts-9f3a")


def test_env_secrets_are_scrubbed_by_value(monkeypatch):
    """"Should never be interpolated" is not "cannot be"."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "liveKeyValue1234567890abcdef")
    assert "liveKeyValue" not in redact("failed with key " + "sk-" + "liveKeyValue1234567890abcdef")


@pytest.mark.parametrize("keep", [
    "73cc1f8a9c926011869123e7f054deb0",      # plaud_id: 32 hex, logged on purpose
    "2026-07-30T191634Z_e0856e43be5a",       # record stem
    "sha256:7c3725678342",                   # the hash prefix we log
    "inbox/2026/07/rec.mp3",
    "ProtectKernelTunables=true",
    "agent exited 1: bwrap: Can't mount proc on /newroot/proc",
    "16 recording(s) in the last 2d",
])
def test_diagnostics_we_rely_on_are_not_over_redacted(keep):
    """Redaction that eats the identifiers makes every log useless, which would
    trade one silent-failure class for another."""
    assert redact(keep) == keep, f"over-redacted: {keep}"


def test_none_and_non_strings_are_safe():
    assert redact(None) == ""
    assert redact(1234) == "1234"
