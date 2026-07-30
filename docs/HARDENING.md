# Hardening plan — v0.1.0-alpha

**Status:** in progress — A1, A2, A3 landed 2026-07-29
**Opened:** 2026-07-29
**Source:** external review of the public repo, plus findings verified on Forge.

This is the working checklist between "works end to end" and "safe to tell other
people to install." Items are marked off here as they land. Each has an
**acceptance criterion** — something checkable, not a feeling.

Ordering is by blast radius, not by effort.

---

## Verified findings that motivate this

Measured on Forge inside the processor's real sandbox, not inferred:

```
credential-shaped env vars visible to the agent:  OPENAI_API_KEY, NOTIFY_NTFY_URL
~/.ssh/atticus_vault (the vault deploy key):      READABLE
~/.config/ai/env (all machine credentials):       READABLE
```

Three consequences, all of which contradict statements currently in the code and
docs:

1. **"The agent never touches git" is not enforced.** Stripping
   `GIT_SSH_COMMAND` is cosmetic when the agent has a shell and the private key
   is readable. It could `ssh -i` and push directly.
2. **An env allowlist alone does not fix this.** The agent can read
   `~/.config/ai/env` off disk regardless of what is in its environment.
3. **The pipeline and the agent cannot share a namespace.** The pipeline *needs*
   `~/.ssh` to push and `~/.config/ai/env` to transcribe. The agent must not have
   either. That is only reconcilable by putting the agent somewhere else.

`bwrap` and `podman` are present on Forge and unprivileged user namespaces work,
so this is buildable without sudo.

---

## A — Security. Blocks telling anyone else to install.

- [x] **A1. Explicit env allowlist for the agent subprocess.**
      Replace the copy-everything-minus-three-vars construction in
      `execute.run()`.
      *Acceptance:* a test spawns the agent path with fake credentials in the
      parent environment and asserts none appear in the child's environment.

- [x] **A2. Run the agent in a real sandbox (`bwrap`).**
      Own mount namespace: scratch workspace read-write, skills read-only, no
      `$HOME`, no `~/.ssh`, no `~/.config`, no vault. Network stays available —
      the agent legitimately needs it.
      *Acceptance:* a security test runs a probe **through the real execute
      path** and asserts `~/.ssh/atticus_vault` and `~/.config/ai/env` are
      unreadable, and that the vault is not writable.

- [x] **A3. Correct every overstated isolation claim.**
      `execute.py`'s docstring ("sees … nothing else"), `README.md`, `SPEC.md`,
      `CLAUDE.md`, ADR-003. State what is actually true after A2, and what is
      still not guaranteed.
      *Acceptance:* no claim of isolation in the repo that a test does not back.

- [ ] **A4. Bound the transcript deterministically before it reaches the agent.**
      The preamble asks the model to ignore trailing speech; that is not a
      control. Extract a command segment: after the wake phrase, stop at
      `ATTICUS_MAX_COMMAND_CHARS` (default 600) or a sentence boundary past it.
      Keep the full transcript in the vault; pass only the segment.
      *Acceptance:* a 389-word ambient transcript yields a prompt containing the
      command and not the trailing conversation.

- [ ] **A5. Treat agent-generated HTML as untrusted when serving it.**
      Strip `<script>`, inline handlers, and external references at publish
      time; serve under a restrictive CSP. Note forgeserve also renders
      Markdown, and the origin is shared with other published sites.
      *Acceptance:* a doc containing `<script>` and a remote `<img>` is served
      with both neutralised.

- [ ] **A6. Per-recording cost ceiling.**
      Nothing currently bounds what one spoken sentence can spend. Wall-clock is
      capped at 30 min; add a token/turn bound and alarm on breach.
      *Acceptance:* the limit is configurable and recorded in the record's
      metadata.

## B — Correctness. Blocks trusting it unattended.

- [ ] **B1. Push failures stop the transition.**
      `commit_push` raises `VaultSyncError` rather than returning `False` into a
      void. **Also fix the second hole:** a clean tree returns success without
      checking whether local is ahead of remote, so a stranded commit is never
      retried by a later pass either.
      *Acceptance:* a test with an unreachable remote asserts the record does not
      advance and the pass exits non-zero; a second test asserts an
      ahead-but-clean repo still pushes.

- [ ] **B2. `retryable` actually retries.**
      New `retry_wait` state with `next_attempt_at`, backoff 5 min → 20 min →
      2 h → permanent. Add `--retry ID` and `--retry-all`.
      *Acceptance:* a simulated 503 lands in `retry_wait`, is skipped before its
      deadline, and is picked up after it.

- [ ] **B3. Never silently skip a record.**
      `load_records()` and `load_seen()` currently `continue` past malformed
      JSON, contradicting success criterion S5. Quarantine, log, alarm, and exit
      non-zero.
      *Acceptance:* a corrupt metadata file produces an alarm and a non-zero
      exit, not silence.

- [ ] **B4. Path containment and ID sanitisation.**
      Upstream IDs flow into filesystem stems unsanitised. Require
      `audio_filename == Path(audio_filename).name`, resolve and assert
      containment, and normalise IDs to a conservative charset while keeping the
      original in metadata.
      *Acceptance:* a metadata file with `../` in `audio_filename` is rejected.

- [ ] **B5. Single-instance lock per role.**
      systemd prevents overlap for one unit; manual runs and second hosts do not.
      *Acceptance:* a second concurrent invocation exits cleanly rather than
      double-processing.

- [ ] **B6. Verify downloaded audio is audio.**
      Size > 0 is not proof; a saved HTML error page passes. `ffprobe` it, record
      `detected_codec` and `verified_duration_seconds`, and alarm when the
      duration disagrees materially with upstream metadata.
      *Acceptance:* a text file served as audio is refused at ingest.

- [ ] **B7. Stream the checksum** instead of `read_bytes()` on whole files.
      *Acceptance:* hashing a 9.5 MB file does not load it into memory.

## C — Reproducibility. Blocks anyone else succeeding.

- [ ] **C1. `pyproject.toml` + lockfile + project-owned venv.**
      Stop depending on a differently-named external `claude-fetchers` venv.
      Extras: `ingest`, `processor`, `dev`.
      *Acceptance:* a clean clone installs and runs from the lockfile alone.

- [ ] **C2. Test suite** — `unit/`, `integration/`, `security/`.
      Fake fetcher, fake OpenAI, fake agent. The security tests are the point.
      *Acceptance:* `pytest` green from a clean clone.

- [ ] **C3. CI** — ruff, pytest, `systemd-analyze verify`, shellcheck, secret scan.
      *Acceptance:* a pull request runs them and can fail.

- [ ] **C4. `atticus doctor`** — one command that checks every precondition the
      installer checks, plus live vault push and session health.
      *Acceptance:* it detects a missing key, a dead session, and an unpushable
      vault, distinctly.

## D — Alpha polish

- [ ] **D1. LICENSE** ⚠️ *needs your decision*
- [ ] **D2. SECURITY.md** — threat model, ambient-audio sensitivity, prompt
      injection, third-party data exposure, unofficial-endpoint fragility,
      disclosure process.
- [ ] **D3. Documentation hierarchy.** `README` = current behaviour only;
      `docs/architecture.md`, `configuration.md`, `operations.md`,
      `threat-model.md`; move superseded reasoning to `docs/history/`.
      Reconcile the confirmed contradictions: `SPEC.md` contains **both**
      `gpt-4o-mini-transcribe` and `gpt-4o-transcribe`, and its diagram still
      shows "Wi-Fi, while charging" as a live path that testing disproved.
- [ ] **D4. Generated configuration table** so defaults cannot drift from code.
- [ ] **D5. Notification detail setting** —
      `ATTICUS_NOTIFICATION_DETAIL=title|summary|full`, defaulting to `title`
      for anyone but the author. Transcript text currently reaches lock screens,
      watches, and phone backups.
- [ ] **D6. Dead-man heartbeat.** Alarm on *absence*: no successful ingest, no
      successful processor pass, oldest `raw` record too old, vault diverged.
      The existing alarms all fire on a *recognised* failure; a disabled timer,
      a full disk, or an import error is still silent. A stalled timer has
      already happened once.
- [ ] **D7. Retention policy** ⚠️ *needs your decision*
- [ ] **D8. Re-tag as `v0.1.0-alpha`** ⚠️ *needs your decision* — deleting a
      published tag is outward-facing.

## E — Deferred, with reasons

- **`src/atticus/` package restructure.** Right eventually; wrong now. High
  churn across every import, unit file, and installer path, and you want tests
  pinning behaviour *before* moving code.
- **Pydantic.** Hand-rolled validation plus quarantine achieves the same end
  without a dependency, and stdlib-leanness is part of this project's character.
- **Splitting transcription into its own subprocess.** Transcription already runs
  in the parent; A1 + A2 subsume the benefit.
- **`CODE_OF_CONDUCT.md`.** Ceremony for a single-operator alpha. LICENSE and
  SECURITY.md are the load-bearing ones.
- **Chunking long recordings**, **granting the agent web access**, **changing the
  wake word.** Each is a decision, not a defect.

---

## Needs a decision from the operator

| # | Question | Why it cannot be defaulted |
|---|---|---|
| D1 | Which licence? MIT, Apache-2.0, or none yet | It is your work; a public repo with no licence grants nobody permission to use it |
| D7 | Audio retention — indefinite, or expire after N days? | Privacy call about permanently committing ambient audio to git history |
| D8 | Delete the published `v1.0.0` tag and re-cut `v0.1.0-alpha`? | Outward-facing and mildly destructive |
| — | Grant the agent `WebSearch`/`WebFetch`? | Real trade-off against everything section A protects |
| — | Change the wake word? | 3 of 9 attempts misheard; the alias list treats symptoms |
