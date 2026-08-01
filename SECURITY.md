# Security

Atticus executes instructions derived from **ambient audio** captured by a
wearable microphone, unattended, on a machine with network access. That sentence
is the whole threat model. Everything below follows from it.

This is **alpha software for a single operator.** It has not been audited.

## Reporting a vulnerability

Open a [security advisory](https://github.com/beekeeper-lab/atticus/security/advisories/new),
or email the address on the maintainer's GitHub profile. Please don't open a
public issue for anything exploitable. Expect a first response within a week;
this is a personal project, not a staffed product.

## What we defend against, and how

### Anything spoken near the device could become an instruction

This is the primary risk, and it is not theoretical: the device records
conversations, meetings, and television. Three controls, in order of how much
they carry:

**The wake-phrase gate.** Only transcripts whose opening words match
`ATTICUS_WAKE_PHRASE` are executed; everything else is filed as an unexecuted
note with its transcript preserved. Of the first fifteen real recordings, most
were correctly not executed.

*Known weakness:* transcription mishears the wake word. Three of nine attempts
in one day came back as "Advocates", "Abacus" and "Artemis" — each silently
filing a real command as a note. Recovery is a **model adjudicator, on by
default** (`ATTICUS_WAKE_ADJUDICATOR`), which widens the gate rather than
replacing it: it runs **only after the strict exact-match has already failed**,
so it can admit a mishearing but never reject a real match. It asks a small
model whether the transcribed first word is a plausible mishearing of the wake
phrase, scores 0–100, and admits at or above `ATTICUS_WAKE_ADJUDICATOR_THRESHOLD`
(default 50). It **fails closed** — no key, no network, a timeout, a non-200, or
any non-integer reply all mean "no". Verdicts are **cached under `~/.cache`**, so
a recurring mishearing costs one call and the system learns its own aliases;
`ATTICUS_WAKE_ALIASES` remains as a deterministic override but now defaults empty.
Note the trade this opens: a **cached verdict can open the gate without a call**,
and a **bounded slice of transcript context** (the few words following the
candidate, capped) is sent to OpenAI alongside the single candidate word to tell
"research this" from a request addressed to a person. Fuzzy string matching is
still deliberately **not** used — measured against the real failure, "advocates"
scores 0.375 similarity to "atticus", *lower* than unrelated words like "status"
(0.615), so any threshold loose enough to catch it fires on ordinary speech.

**Bounded prompts.** `ATTICUS_MAX_COMMAND_SECONDS` caps how much audio is ever
transcribed; `ATTICUS_MAX_COMMAND_CHARS` and `ATTICUS_MAX_COMMAND_SENTENCES` cap
how much of the transcript reaches the agent. The full transcript is always kept
in the vault — only the prompt is cut.

*Known weakness:* this **bounds exposure; it does not isolate intent.** No
positional heuristic can separate a command from speech that immediately
follows it. If you keep talking for two sentences after the request, those two
sentences reach the agent. A real transcript in the vault contains the sentence
*"hey Atticus, send a message to <name>"*, spoken as an example of a future
capability. It is harmless only because no Signal skill exists yet. **Adding a
side-effecting skill makes that sentence executable.**

**Spend ceiling.** `ATTICUS_MAX_BUDGET_USD` bounds what one utterance can cost.

### The agent must not reach the operator's credentials

The agent runs under `bwrap` in its own mount namespace with a private `HOME`.
Inside it can see the scratch workspace (read-write), `/usr` and `/etc`
(read-only), the Claude CLI binary, its credential, and the skills directories.

It cannot see `~/.ssh`, `~/.config/ai/env`, the vault *filesystem*, or anything
else in the operator's home. This is enforced and tested — see
`tests/security/`, which exists so that a claim in the documentation cannot
quietly become false.

**Two important qualifications, because the sentence above was previously read
as broader than it is.**

*The vault's **contents** are reachable over the network even though its
filesystem is not.* The namespace shares the host network by default, so
anything served on `127.0.0.1` is reachable — and the vault's own web UI serves
the searchable text of every document, including the gated notes the wake gate
declined to execute, with an API write token embedded in each page. "Cannot see
the vault" is true of paths and false of HTTP. `ATTICUS_SANDBOX_NET=none`
removes the network entirely and closes this; the durable fix is a network
namespace with an allowlist proxy, which keeps research working while denying
loopback and the tailnet.

*An agent auth secret is inside the boundary by construction* — which one is a
configuration choice with different blast radii. Blank
`ATTICUS_CLAUDE_TOKEN_FILE` bind-mounts `~/.claude/.credentials.json`
read-only, which contains the operator's **refresh token**: an agent acting on
injected instructions has `Bash` and egress, so treat it as reachable, and
rotate it if you suspect a run was influenced. Setting
`ATTICUS_CLAUDE_TOKEN_FILE` to a `claude setup-token` output replaces that
with a dedicated long-lived token passed via `CLAUDE_CODE_OAUTH_TOKEN` — still
exfiltratable, but revocable without touching the operator's own sessions,
and the refresh token never enters the namespace. Prefer the token mode; a
per-run short-lived token or auth broker (#68) remains the durable fix.

Earlier versions asserted this and did not deliver it: the vault deploy key was
readable and the agent has a shell, so "the agent never touches git" was
decorative. That history is in `docs/history/forge-2026-07-29.md`.

### Failures must be loud

An unattended pipeline that fails silently is worse than one that fails. Push
failures raise; malformed records are quarantined and alarmed; a dead upstream
session alarms; timers use wall-clock schedules because a monotonic one stalled
undetected for 76 minutes. Set `ATTICUS_NOTIFY_URL` — without it, a dead
pipeline is indistinguishable from a quiet weekend.

## What we do NOT defend against

Stated plainly, because a threat model that lists only wins is marketing.

- **Network egress.** The agent has full internet access; it is doing research.
  Anything it can reach, it can reach. There is no egress filtering.
- **Network *ingress* on this host.** The same shared namespace means loopback
  and the tailnet are reachable *inward*: the vault web UI, its write API, and
  any other local service. This is the same omission as egress, but it is easy to
  miss because the sandbox denies the vault's files. `ATTICUS_SANDBOX_NET=none`
  is the switch when no skill needs the internet.
- **Prompt injection.** The transcript is fenced and labelled as untrusted data
  and the fence markers are stripped from the transcript so it cannot forge
  them — but a model is not obliged to honour a fence. `Bash` is in the default
  tool list and the permission mode is `acceptEdits`, so assume anything spoken
  near the pin can run arbitrary commands *inside the sandbox*. The namespace is
  the control; the fence only raises the bar.
- **Privilege escalation.** The sandbox is a filesystem and environment
  boundary. The agent runs as the same uid as the pipeline.
- **`ATTICUS_SANDBOX=off`.** A supported setting that disables all of the above.
- **A malicious skill.** Skills are instructions the model follows and are
  trusted completely. Review anything you add.
- **Physical access to the device**, or anyone within earshot of it.
- **Prompt injection reaching the agent.** We bound and gate the input; we do
  not claim to have solved it.

## Data exposure you are opting into

- **Audio and transcripts go to OpenAI** for transcription; **prompts and
  research go to Anthropic** via Claude Code. Ambient audio may include third
  parties who did not consent.
- **The vault must be a private repository.** It holds raw audio and
  transcripts. `ops/init-vault.sh` says so; nothing enforces it.
- **Audio is committed to git history**, where deletion is deliberately hard.
  There is no retention policy yet — decide one before this runs for months.
- **Notifications carry transcript text by default.** Set
  `ATTICUS_NOTIFICATION_DETAIL=title` to send only a title and link; the body
  otherwise reaches lock screens, watches, and phone backups via a third-party
  push service.
- **The published site is tailnet-only**, and agent HTML is sanitised and served
  under a restrictive CSP — but it shares an origin with other published sites.

## Third-party terms

`ingest/plaud_web.py` authenticates with a browser session and calls
**undocumented** Plaud endpoints. This project's licence grants no rights in
Plaud's service. Whether you may use it that way is governed by your agreement
with them, and those endpoints may change or be withdrawn without notice. The
transport is deliberately pluggable so it can be replaced.

The BLE protocol notes in `docs/decisions/` were derived from the vendor's own
published Android SDK for interoperability.

## Credentials

All secrets live in `~/.config/ai/env` (mode 600). Nothing in this repository
holds a key; `ops/.env` is gitignored, and `ops/pr.sh` refuses to commit
credential-shaped strings regardless of what `.gitignore` says. CI scans the
full history on every pull request.

If a key ever does reach a public repository, **rotate it** — cleaning history
is damage limitation, not a fix.
