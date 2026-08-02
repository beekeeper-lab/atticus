# ADR-009 — An approval must arrive on a channel the sandbox cannot reach

**Status:** Accepted
**Date:** 2026-08-02
**Issue:** [#83](https://github.com/beekeeper-lab/atticus/issues/83)
**Related:** [#42](https://github.com/beekeeper-lab/atticus/issues/42) (the outbox), [#69](https://github.com/beekeeper-lab/atticus/issues/69) (the embedded write token), `SECURITY.md`

## Context

`outbox.gate()` has always returned `auto | confirm | off`. Until this change
**`confirm` meant held forever**: the intent was recorded in a receipt, the
report said pending, and nothing in the system could ever approve it.

In an unattended pipeline that makes the middle setting functionally identical
to `off`, with better paperwork. The consequence was visible in the
configuration: every verb the operator enabled — `github.issue`,
`github.comment`, `github.close`, `slack.post`, `todo.add` — is set to `auto`,
because opening a verb to `auto` was the only way to make it work at all. A gate
whose safe middle position is unusable is a gate that pushes you to the unsafe
one, which is precisely what per-verb gates were introduced to prevent.

So the queue had to exist. The question was where approving happens.

## The obvious answer, and why it is wrong

The vault browser was the natural home. It already has a write API
(`site/api.py`), a token-authenticated mutation path, and a working check-off UI
built for todos on 2026-08-01. An approvals view would have been an afternoon.

**It would also have handed the agent the power to approve itself.**

Three facts compose into that:

1. the vault API answers on **loopback**;
2. the sandbox deliberately keeps the **host network namespace**, because
   research needs egress (`ATTICUS_SANDBOX_NET=host`, and #70 tracks the
   allowlist-proxy fix);
3. the vault-API **write token is embedded in every published page** (#69, still
   open).

An agent acting on injected instructions can therefore fetch a published page,
scrape the token, and POST to the API. If approving lived behind that API, a
held action would be one prompt injection away from performed — and the control
would be reachable by the exact thing it exists to control. The queue would be
theatre.

## Decision

**Approval arrives out of band, on a second ntfy topic, and the pipeline polls
for it.**

- The push that announces a held action carries ntfy **action buttons**; tapping
  one publishes a small JSON decision to `ATTICUS_APPROVAL_TOPIC_URL`.
- The processor polls that topic on its existing pass (`?poll=1&since=`), so
  there is **no new timer** and **no inbound endpoint** on the box that runs an
  autonomous agent — the same reasoning that made this project poll Plaud and
  poll git rather than accept webhooks.
- **The agent cannot discover the topic.** `agent_env()` is a strict allowlist:
  `LANG, LC_ALL, TZ, TERM`, a synthetic `HOME`/`PATH`, `ATTICUS_OUTPUT_DIR`, and
  its own auth token. No `ATTICUS_*` URL is in it.
- Every pending item carries a **nonce** minted at enqueue; a decision whose
  nonce does not match is refused, so an old push cannot be re-tapped and a
  guessed message cannot approve anything.
- The vault browser shows the queue **read-only**. Seeing is harmless — the
  agent can already read the site — and it means the operator can review what is
  waiting without the browser being able to act.
- Approved actions run through `outbox.validate()` and the ordinary handler, the
  same path an `auto` action takes. An approval must not become a second, laxer
  route to a credential.

## Residual risk, stated plainly

**The approval topic is a bearer capability.** Anyone who knows the URL can
approve. That is the same trust model as the existing alarm topic
(`ATTICUS_NOTIFY_URL`, which `config.redacted()` already treats as secret), and
it is why the URL must be unguessable, must stay out of the repository, and is
blank by default. The nonce bounds replay but not possession.

A stronger design exists — a unix socket under `.state/`, openable only with a
vault mount the sandbox lacks — and it is the same fix #69 and #70 point at. It
is not built here because it changes the network topology, and because the
out-of-band channel is a genuine improvement available today.

## Consequences

- `confirm` becomes usable, so `outlook.draft` and `outlook.event` no longer
  face an all-or-nothing choice, and Signal has a safe first configuration when
  it is enabled.
- Held actions **expire** (`ATTICUS_APPROVAL_TTL_HOURS`, default 24) and expiry
  is reported, never silent: the operator otherwise believes something is still
  waiting.
- `ATTICUS_OUTBOX=off` still queues nothing. A global stop must not quietly
  become a global "later".
- With `ATTICUS_APPROVAL_TOPIC_URL` blank — the shipped default — behaviour is
  exactly as before. A queue nobody configured must not start accepting
  decisions from a topic nobody chose.
