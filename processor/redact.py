"""Strip credential-shaped material from anything we log or commit.

Written after a confirmed leak: on 2026-07-30 an ingest download failure put a
presigned S3 URL — complete with `AWSALB` session cookies — into the systemd
journal, because Fetcher._run reports the last 300 characters of the failing
subprocess's stderr and that tail happened to be the URL. journald is persistent,
so a short-lived credential became a durable one.

The same shape reaches the vault: transcribe.py puts a slice of an API error body
into `last_error`, which is committed to git, where deletion is deliberately hard.

Two rules make this worth having as a module rather than a local helper:

1. **Never log a subprocess's raw output.** A library decides what goes in its
   error strings, and libraries interpolate URLs.
2. **Redact by SHAPE, not by matching known secrets.** We cannot enumerate what
   an upstream will echo, so the patterns target the shapes credentials take.

Deliberately aggressive: a redacted diagnostic is mildly annoying, a leaked one
is unrecoverable. Where a pattern might eat something useful, the replacement
names what was removed so the message still reads.
"""
import os
import re

_PATTERNS = (
    # OpenAI / Anthropic style keys.
    (re.compile(r"\b(sk|rk)-[A-Za-z0-9_\-]{16,}"), r"\1-<redacted>"),
    # GitHub.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "gh?_<redacted>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"), "github_pat_<redacted>"),
    # A JWT — three base64url segments. This is the Plaud workspace token's shape.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
     "<redacted-jwt>"),
    # AWS access key ids.
    (re.compile(r"\bA(?:KIA|SIA)[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    # Slack.
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "<redacted-slack-token>"),
    # Authorization headers, however they are spelled. Consumes the WHOLE value,
    # not just the first token: `\S+` matched only the word "Bearer" and left the
    # credential after it completely intact.
    (re.compile(r"(?i)\b(authorization|x-api-key|api[-_]?key)\b(\s*[:=]\s*)"
                r"[^\r\n;,\"']+"),
     r"\1\2<redacted>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer <redacted>"),
    # Cookie-shaped assignments with a long opaque value: AWSALB, session ids,
    # anything a load balancer or app server sets. This is the pattern that
    # actually leaked.
    (re.compile(r"(?i)\b(AWSALB[A-Z]*|JSESSIONID|PHPSESSID|sessionid|session|"
                r"csrftoken|access_token|refresh_token|id_token|token|sig|"
                r"signature)(\s*=\s*)[A-Za-z0-9%+/_.\-]{12,}={0,2}"),
     r"\1\2<redacted>"),
    # ntfy topic URLs are a bearer capability in themselves.
    (re.compile(r"https?://ntfy\.sh/[A-Za-z0-9_\-]{6,}"),
     "https://ntfy.sh/<redacted-topic>"),
    # Presigned URLs and anything else carrying a query string. Keep the origin
    # and path — that is the diagnostically useful part — and drop the query,
    # which is where signatures and tokens live.
    (re.compile(r"(https?://[^\s?\"']+)\?[^\s\"']+"), r"\1?<redacted-query>"),
    # HEADLESS opaque blobs. The leak that prompted this module arrived with its
    # cookie NAME already cut off by a 300-character truncation, so every
    # name-based rule above missed it — what remained was 200 characters of bare
    # base64. Two shapes, both bounded so they cannot eat identifiers we log on
    # purpose:
    #
    #   * anything 40+ chars from the base64/urlsafe alphabet. A plaud_id is 32
    #     hex characters and a sha256 prefix is 12, so both survive.
    #   * anything 24+ chars that contains '+' or '/', which are base64-only —
    #     hex ids and filename stems never contain them.
    # '/' is deliberately absent, so a run can never span path separators and eat
    # `processed/2026/07/<stem>`. Base64's '/' is covered instead by the '+' and
    # padding signals below, and by the named-cookie rules above.
    (re.compile(r"[A-Za-z0-9+_\-]{8,}={0,2}"), lambda m: _blob(m.group(0))),
)


def _blob(run: str) -> str:
    """Decide whether one candidate run is opaque credential material.

    A function, not another regex. Pure patterns kept getting this wrong in both
    directions: including '/' in the alphabet let a rule span path separators and
    eat `processed/2026/07/<stem>`, while excluding it left 17-character fragments
    of the leaked cookie sitting between two redactions. The distinguishing facts
    are about the run's CONTENT, which is easier to state directly.

    Redacts when the run is:
      * 40+ characters — no identifier we log is that long; or
      * base64-signalled by a '+' or '=' padding, neither of which occurs in
        paths, hex ids or filename stems; or
      * 24+ characters with mixed case AND digits, which is token-shaped.

    Keeps hex ids (a 32-char plaud_id is single-case), record stems, sha256
    prefixes, paths and ordinary identifiers.
    """
    core = run.rstrip("=")
    padded = run.endswith("=")
    if len(core) < 8:
        return run
    has_plus = "+" in core
    # THREE of each, not one. A single uppercase letter is not entropy: the record
    # stem 2026-07-30T191634Z_<hex> has exactly two (the T and the Z) and was
    # being redacted out of every log line, which is worse than the leak in
    # day-to-day terms. Real base64 of random bytes has many of each.
    uppers = sum(1 for c in core if c.isupper())
    lowers = sum(1 for c in core if c.islower())
    digits = sum(1 for c in core if c.isdigit())
    mixed = uppers >= 3 and lowers >= 3 and digits >= 1
    if len(core) >= 40 or has_plus or (padded and mixed) or (len(core) >= 24 and mixed):
        return "<redacted-blob>"
    return run


def redact(text) -> str:
    """Return `text` with credential-shaped substrings replaced.

    Also removes the literal values of the environment's own secrets, since
    "should never be interpolated" is not "cannot be".
    """
    if text is None:
        return ""
    out = str(text)
    for var in ("OPENAI_API_KEY", "ATTICUS_NOTIFY_URL",
                "ATTICUS_RESULT_NOTIFY_URL", "PLAUD_EMBEDDED_API_KEY",
                "PLAUD_EMBEDDED_CLIENT_SECRET"):
        val = os.environ.get(var, "")
        if val and len(val) > 8:
            out = out.replace(val, f"<redacted-{var.lower()}>")
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out
