# ADR-002 — Plaud Web fetcher instead of the official CLI

**Status:** Accepted
**Date:** 2026-07-28
**Supersedes:** SPEC §7's "use `@plaud-ai/cli`" recommendation

## Context

The v1 design had WarDog poll Plaud Cloud with the official `@plaud-ai/cli`.
On attempting login, the CLI requires a paid subscription. The MCP server is
gated the same way — Plaud ships both as beta features behind a plan.

What the **free Starter tier** does include:

- Original audio export in MP3/WAV, via app and web
- Wi-Fi sync from the device to Plaud Cloud
- Full web and app access to recordings
- 300 transcription minutes/month

So the recordings and their original audio are reachable on a free account.
Only the *programmatic* door is locked.

Two facts make the paid tier poor value for this project specifically:

1. **We do not use Plaud's transcription.** `faster-whisper` runs on Forge.
2. **Short commands consume zero transcription minutes anyway.** AutoFlow only
   fires above 200 words; a 15-second utterance is ~25. The 300-minute free
   allowance is not even a constraint.

The subscription would buy a CLI wrapper around data already accessible, plus
minutes that go unused.

## Decision

**Build a Plaud Web fetcher** following the established site-fetcher pattern in
`~/.claude/site-fetchers/`: Playwright with a persistent session, credentials
from 1Password, no stored password.

The refinement: **authenticate with Playwright, then call Plaud's JSON API
directly** using the session the browser established. Scraping the DOM would
break on any cosmetic change; the API behind the web app is a narrower and more
stable target.

`ingest/` calls the fetcher where it would have called `plaud`. The interface is
the same — list recordings, get a download URL, fetch audio — so nothing
downstream of ingest changes.

## Alternatives rejected

| Option | Why not |
|--------|---------|
| Pay Pro/Unlimited | ~$216–360/yr for a wrapper around free-tier data plus unused minutes |
| Community toolkits (`plaud-toolkit` et al.) | Alpha, unofficial, and they want the account password in plaintext |
| Build the iOS app now (W8) | Genuinely solves it and is cheaper per year than Plaud Pro, but costs a Mac, $99/yr Apple, and a Swift project |
| Manual export to a watched folder | Breaks the zero-touch requirement, which is the whole point |

## Consequences

- **$0/yr.** No Plaud subscription.
- **We own a fragile dependency.** If Plaud reworks their web app, the fetcher
  breaks. This is the same bet the other five fetchers in the registry already
  make, and it is why T-72's health check matters more than it did.
- **No stored password.** The session cookie is the credential; 1Password holds
  the login for re-seeding. Strictly better than the community toolkits.
- **Session seeding is interactive.** Like the Plaud CLI before it, first login
  needs a browser. Seed on a desktop, copy the session directory to WarDog.
  Same operational shape as the `tokens.json` plan it replaces.
- **Session expiry is a silent failure mode.** Unchanged from the CLI design and
  still covered by T-72.
- **W8 stays contingent** but its economics improved: the Plaud Developer Portal
  is free, so the iOS app would carry no recurring Plaud cost either. If T-14
  rules out Plaud Cloud, the choice is between two $0-Plaud designs.

## Revised ADR-001 note

ADR-001 held that Plaud Cloud plus the official CLI made an iOS app unnecessary.
The CLI half of that is now false. The conclusion survives — the cloud path
still works, just through a different client — but the margin is thinner. A
second failure of the web path would make W8 the obvious answer rather than the
expensive one.
