# processor

The Forge half. Turns a recording in the vault into executed work.

**Runs on Forge** under a systemd timer, every 5 minutes. Learns of new work by
polling git — no webhook, no inbound port on the box that runs an autonomous
agent.

```
git pull --rebase
  → scan inbox/ for status:raw
  → transcribe   gpt-4o-transcribe             → transcribed
  → route        gate + build the prompt       → routed
  → execute      claude -p, scratch workspace  → executed
  → publish      commit output to the vault    → published
```

Each stage advances `status` in the recording's metadata JSON **and commits**.
A crash resumes rather than redoing work, and git history doubles as an
execution log.

| File | Role |
|------|------|
| `pipeline.py` | Orchestrator and CLI |
| `config.py` | `ops/.env` for settings, `~/.config/ai/env` for keys |
| `vault.py` | Records, status transitions, safe git push |
| `transcribe.py` | OpenAI STT + the sanity gate |
| `execute.py` | Scratch workspace + `claude -p` |
| `mkfixture.py` | Fake a recording for testing without hardware |

## Usage

```bash
python3 processor/pipeline.py            # one pass
python3 processor/pipeline.py --status   # show the queue, change nothing
python3 processor/pipeline.py --dry-run  # everything except the agent call
python3 processor/pipeline.py --once ID  # a single recording
```

Exit: `0` clean · `1` some records failed · `2` usage/config error.

## Testing without a pin

```bash
python3 processor/mkfixture.py --clean --say "Research X and write it up as HTML"
ATTICUS_VAULT_PATH=$PWD/.scratch-vault python3 processor/pipeline.py
```

`--say` synthesises speech with espeak-ng, so the real STT path runs.
`--audio FILE` uses a real recording. `--text` skips transcription entirely.

## Transcription

Deliberately the same path the machine's dictation already uses — same
endpoint and the same capitalization prompt as
hyprwhspr. One transcription stack, not two. Key comes from
`~/.config/ai/env`, never from a config file in this repo.

Guards borrowed from ScribeVault: 25 MB pre-upload size check, retry with
backoff, and **auth / quota / transient are distinguished** — a spend-limit
block is not retried, because retrying it accomplishes nothing.

## Routing is the model's job

There is no routing table here. `claude -p` runs in a workspace with
`.claude/skills/` linked to `skills/`, and the model picks a matching skill
from its description. **Adding a capability means adding a skill directory** —
no pipeline change.

No skill matches → it just does what was asked. That is the intended fallback,
not a failure.

## The sanity gate

The transcript becomes the prompt, so a bad transcript means an agent acting on
nonsense. Before executing:

- **Minimum word count** (`ATTICUS_MIN_WORDS`, default 3)
- **Optional wake phrase** (`ATTICUS_WAKE_PHRASE`) — if set, only transcripts
  starting with it are executed; everything else is filed as a note

`gpt-4o-transcribe` returns plain text with no confidence signal
(`verbose_json` with `no_speech_prob` is whisper-1 only), so the gate is
heuristic by necessity. A gated recording is **published as a note, not
failed** — it is a deliberate refusal to act, not an error.

The wake phrase matters most for a wearable: without it, an overheard meeting
becomes an instruction.

## Security

- **The agent never touches git.** It writes into a scratch dir; the pipeline
  copies and commits. The deploy key does not exist inside its namespace.
- It runs in a `bwrap` mount namespace with its own `HOME`: no `~/.ssh`, no
  `~/.config/ai/env`, no operator home, no vault path. It sees a copy of the
  skills dir and an empty output dir.
- **Env is an allowlist**, not a strip-list. Stripping `GIT_SSH_COMMAND` /
  `GIT_ASKPASS` / `SSH_AUTH_SOCK` was the old mechanism and was never a control
  at all — the deploy key was readable and the agent has a shell.
- Collection refuses symlinks and any path resolving outside `output/`, and caps
  file count and total bytes. That copy step is the exfiltration boundary.
- Wall-clock timeout (`ATTICUS_EXEC_TIMEOUT`, default 30 min) and a spend
  ceiling (`ATTICUS_MAX_BUDGET_USD`).
- Every executed prompt is committed, and the agent's stdout is saved as
  `agent-stdout.txt` beside its output — git history is the audit trail for the
  instruction *and* for what the agent did with it.

**Not covered.** The network namespace is shared with the host by default, so
the agent has full egress *and* can reach loopback services; and the Claude Code
credential is bound in so the CLI can authenticate. `ATTICUS_SANDBOX_NET=none`
closes the network half when no skill needs the internet. See `../SECURITY.md`.
