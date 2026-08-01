"""slack.post — post a message to an allowlisted Slack channel. Issue #48.

Nothing Slack-shaped is installed on the box and nothing needs to be: posting is
one HTTPS call to `chat.postMessage`, so this is `requests` and no dependency.

## Why this needs its own token

A claude.ai Slack connector exists, but it is **interactively authenticated**, and
Atticus is a headless timer-driven pass with nobody at a keyboard. Relying on it
would give a capability that works when a human happens to be signed in and fails
silently otherwise, which is the worst of both. So this holds its own credential,
on the pipeline side of the intent boundary, per the outbox contract (#42).

**A bot token (`xoxb-`), never a user token.** A user token can do everything the
account can — read every DM, every private channel, and act as the person. A bot
token scoped to `chat:write` in the channels it has been invited to can post, and
nothing else. Since the text being posted derives from ambient audio, the width of
the token *is* the blast radius, so a `xoxp-` token is refused outright below
rather than accepted with a warning nobody reads.

## Why the channel comes from config, not from the request

`chat.postMessage` will happily post wherever the token can reach, and the channel
in the request originates in a transcript of speech picked up by a worn microphone.
"post it to the standup channel" is one mishearing away from "#general", and a
channel post cannot be unsent — it is read by everyone in the channel within
seconds. So the request may only *select* from a channel allowlist the operator
configured; anything else is refused by name. An empty allowlist means the skill is
off, not "anywhere" — a fail-open default here would be a bug with an audience.

## Reading is not here, deliberately

`outbox.py` explains why: a read needs data *during* the agent's run and an outbox
runs after it exits. `channels:history` has no place in this token's scopes.
"""
import re

import requests
from outbox import OUTWARD, OutboxError, handler

# The wire endpoint. Overridable only so tests can point it at a closed port.
_API = "https://slack.com/api/chat.postMessage"

# Slack accepts up to 40,000 characters but renders anything past a few thousand
# as a "click to expand" stub, and the report in the vault is the real artifact —
# a channel post is a pointer to it. Refusing rather than truncating keeps a
# sentence from being cut mid-word in front of a whole channel.
_MAX_CHARS = 3900

# Channel IDs are uppercase and go on the wire as-is; anything else is a name and
# gets a leading '#'. C=public, G=private/group, D=DM.
_CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]{6,}$")

# Slack's own error strings, mapped to something that tells the operator what to
# fix. `ok: false` arrives with HTTP 200, so these are the only real diagnosis.
_ERRORS = {
    "channel_not_found": ("channel not found — the bot must be invited to it, "
                          "and a private channel needs groups:write"),
    "not_in_channel": "the bot is not in that channel — invite it with /invite",
    "is_archived": "that channel is archived",
    "invalid_auth": "Slack rejected the token (invalid_auth)",
    "account_inactive": "the token's bot user is deactivated",
    "token_revoked": "the token has been revoked",
    "missing_scope": "the token lacks the chat:write scope",
    "not_allowed_token_type": "wrong token type — this needs a bot token (xoxb-)",
    "msg_too_long": "Slack considered the message too long",
    "ratelimited": "rate limited by Slack; nothing was posted",
}


def _norm(name: str) -> str:
    """A channel as written by a human: '#Standup ' and 'standup' are one thing."""
    return str(name or "").strip().lstrip("#").strip().lower()


def _resolve_channel(req: dict, cfg) -> str:
    """Pick a channel from the allowlist, or refuse.

    The allowlist is mandatory and it is the whole safety story for this handler.
    Note what is NOT done here: the requested channel is never passed through
    after failing to match, not even normalised into "closest thing". A refusal
    lands in the receipt with the name that was asked for, which is exactly what
    the operator needs to see to decide whether to widen the allowlist.
    """
    allowed = [str(c).strip().lstrip("#").strip()
               for c in (getattr(cfg, "slack_channels", []) or [])
               if str(c).strip()]
    if not allowed:
        raise OutboxError(
            "no Slack channels are allowlisted — set ATTICUS_SLACK_CHANNELS "
            "before slack.post can do anything")
    by_norm = {_norm(c): c for c in allowed}

    asked = _norm(req.get("channel"))
    if not asked:
        asked = _norm(getattr(cfg, "slack_default_channel", ""))
        if not asked:
            raise OutboxError(
                "slack.post needs a 'channel', and no ATTICUS_SLACK_DEFAULT_CHANNEL "
                f"is set; allowed: {', '.join(sorted(by_norm))}")
    if asked not in by_norm:
        raise OutboxError(
            f"#{asked} is not on the Slack channel allowlist; allowed: "
            f"{', '.join(sorted(by_norm))} (ATTICUS_SLACK_CHANNELS)")

    channel = by_norm[asked]
    return channel if _CHANNEL_ID.match(channel) else f"#{channel}"


def _token(cfg) -> str:
    token = str(getattr(cfg, "slack_bot_token", "") or "").strip()
    if not token:
        raise OutboxError("ATTICUS_SLACK_BOT_TOKEN is not configured")
    if not token.startswith("xoxb-"):
        # Refused, not warned. A xoxp- token would work — that is the problem.
        raise OutboxError(
            "ATTICUS_SLACK_BOT_TOKEN must be a bot token (xoxb-); a user token "
            "can do everything the account can and is not accepted here")
    return token


def _describe(req: dict) -> str:
    """What the operator reads in the confirmation, so it shows the actual text."""
    text = " ".join(str(req.get("text") or "").split())
    where = _norm(req.get("channel")) or "the default channel"
    return f"post to Slack #{where}: {text[:120]}" if text else f"post to Slack #{where}"


@handler("slack.post", risk=OUTWARD, schema=("text",), describe=_describe)
def post(req: dict, cfg, log=print) -> dict:
    """Post one message. Raises OutboxError with something actionable, or returns
    the channel and timestamp for the receipt."""
    channel = _resolve_channel(req, cfg)
    token = _token(cfg)

    text = str(req.get("text") or "").strip()
    if len(text) > _MAX_CHARS:
        raise OutboxError(
            f"message is {len(text)} characters; Slack posts are capped here at "
            f"{_MAX_CHARS} — post a summary and link to the report instead")

    timeout = int(getattr(cfg, "slack_timeout", 15) or 15)
    body = {"channel": channel, "text": text}
    try:
        resp = requests.post(
            str(getattr(cfg, "slack_api_url", "") or _API),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json; charset=utf-8"},
            json=body, timeout=timeout,
        )
    except requests.Timeout:
        # Ambiguous on purpose: a timeout after the POST left may still have
        # posted. Say so rather than implying nothing happened.
        raise OutboxError(f"Slack timed out after {timeout}s — the message may "
                          f"or may not have posted")
    except requests.RequestException as e:
        raise OutboxError(f"Slack network error: {type(e).__name__}")

    if resp.status_code != 200:
        # Response bodies get truncated everywhere in this pipeline: they land in
        # a receipt, the receipt is committed, and git is forever.
        raise OutboxError(f"Slack returned HTTP {resp.status_code}: {resp.text[:160]}")

    try:
        data = resp.json()
    except ValueError:
        raise OutboxError(f"Slack returned a non-JSON body: {resp.text[:160]}")

    # THE TRAP: Slack signals application errors with HTTP 200 and {"ok": false}.
    # Checking status_code alone reports every failure as a successful post, which
    # is the one wrong answer that matters — the operator's report would say the
    # channel was told something it never heard.
    if not data.get("ok"):
        err = str(data.get("error") or "unknown_error")
        detail = _ERRORS.get(err)
        needed = data.get("needed")
        raise OutboxError(
            f"Slack refused the post ({err})"
            + (f": {detail}" if detail else "")
            + (f" [needs {needed}]" if needed else ""))

    return {"channel": data.get("channel") or channel, "ts": data.get("ts")}
