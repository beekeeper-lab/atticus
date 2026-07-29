# processor

The Forge half. Turns a recording in the vault into executed work.

**Runs on Forge** under a systemd timer, every 5 minutes. Learns of new work by
polling git — no webhook, no inbound port on the box that runs an autonomous
agent.

```
git pull --rebase
  → scan inbox/ for status:raw
  → transcribe   gpt-4o-mini-transcribe        → transcribed
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
endpoint, same `gpt-4o-mini-transcribe` model, same capitalization prompt as
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

`gpt-4o-mini-transcribe` returns plain text with no confidence signal
(`verbose_json` with `no_speech_prob` is whisper-1 only), so the gate is
heuristic by necessity. A gated recording is **published as a note, not
failed** — it is a deliberate refusal to act, not an error.

The wake phrase matters most for a wearable: without it, an overheard meeting
becomes an instruction.

## Security

- **The agent never touches git.** It writes into a scratch dir; the pipeline
  copies and commits. No deploy key in its environment.
- It runs in a temp workspace, not the vault — it sees the skills dir and an
  empty output dir, nothing else.
- `GIT_SSH_COMMAND`, `GIT_ASKPASS` and `SSH_AUTH_SOCK` are stripped from its env.
- Wall-clock timeout (`ATTICUS_EXEC_TIMEOUT`, default 30 min).
- Every executed prompt is committed — git history is the audit trail.
