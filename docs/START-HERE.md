# Start here — setting up Atticus from nothing

You are about to wire a wearable microphone to an autonomous agent that can file
issues, send messages and spend money on your behalf. This guide takes you from
owning no hardware to a working pipeline, in the order the pieces actually need
to exist.

**Who this is for.** Someone comfortable on a Linux command line who can read a
systemd unit, generate an SSH key, and register an OAuth app without being
walked through it. It does not explain what a git remote is. It *does* explain
every Atticus-specific decision, because those are the ones you cannot guess.

**Time.** About two hours for the core pipeline if nothing fights you. Each
optional integration is another 10–30 minutes, and several need a browser.

**Cost.** Roughly **$1–5/month** in OpenAI transcription for personal use, plus
a **Claude subscription** (Pro or Max) for the agent, plus **$0–$25** for a
Plaud device depending on model. Everything else here is free.

---

## 0. Decide whether you want this

Read this section properly. It is cheaper than discovering these in week three.

- **Sync is not hands-off.** Audio only leaves the pin while the Plaud phone app
  is *foregrounded*. Not while charging, not merely with the phone unlocked. You
  will open the app a few times a day. This was investigated to a conclusion and
  is closed — see [ADR-005](decisions/ADR-005-direct-device-access-is-closed.md).
- **Round trip is ~30 minutes**, of which ~13 is pipeline and the rest is sync
  and poll intervals. This is a system for things that can wait.
- **A wearable overhears people.** Every recording lands in a private git repo.
  Wake-word gating means most are never executed, but they are all *kept*.
- **You need an always-on Linux host.** A laptop that sleeps will work badly.
- **You are running an autonomous agent with a shell and network access.** It is
  sandboxed (`bwrap`, no vault, no SSH keys, no credentials) but it is not
  contained from the network. Read [`../SECURITY.md`](../SECURITY.md) before you
  enable anything that sends messages to other people.

If any of that is disqualifying, stop here — you will not be happier three steps
in.

---

## 1. Buy a device

Any Plaud recorder works, because Atticus talks to **Plaud's cloud**, not to the
hardware. What matters is that the model syncs to a Plaud account.

| model | shape | notes |
|---|---|---|
| **NotePin / NotePin S** | wearable pin, magnet/clip/lanyard | What this was built and tested on. Best for on-the-go capture, which is the design centre. |
| **Note / Note Pro** | card that sticks to a phone | Bigger battery, better for long-form. Same cloud, so Atticus does not care. |

**Do not buy the subscription.** The free **Starter** tier is all Atticus needs.
Plaud's paid tiers sell transcription and summarisation that this pipeline
deliberately does not use — Atticus transcribes with OpenAI instead, and their
AutoFlow only fires above ~200 words, so short commands arrive with no Plaud
transcript at all. You will consume zero of their minutes.

Set the device up with the Plaud phone app first, normally, and confirm a test
recording appears at **web.plaud.ai** in a browser. If it does not reach the web
app, Atticus cannot see it either, and that is a Plaud problem to solve before
you continue.

---

## 2. Prepare the host

An always-on Linux machine with a normal user account. No root is needed;
everything installs as user-level systemd units.

```bash
# Fedora / RHEL
sudo dnf install -y git python3 ffmpeg bubblewrap
# Debian / Ubuntu
sudo apt install -y git python3 python3-venv ffmpeg bubblewrap
```

You need:

| thing | why |
|---|---|
| **Python 3.11+** | the pipeline |
| **git** | the vault *is* a git repo, and git is the queue |
| **ffmpeg / ffprobe** | duration probing, truncation, chunking |
| **bubblewrap** (`bwrap`) | **contains the agent.** Without it the agent runs unsandboxed |
| **`claude` CLI** | the agent — [install it](https://claude.com/claude-code) and sign in once |
| **`gh` CLI** | only if you want the GitHub skill |
| **linger enabled** | so user units survive logout: `loginctl enable-linger $USER` |

Verify bubblewrap actually works, because on some distros an AppArmor policy
lets it install but not create namespaces:

```bash
bwrap --ro-bind /usr /usr --dev /dev --unshare-pid -- /bin/true && echo "bwrap OK"
```

If that fails, fix it before continuing. `ATTICUS_SANDBOX=off` exists but is a
genuinely bad idea: without the namespace, the agent can read every credential
on the box.

---

## 3. Create the two repositories

Atticus is **two repos on purpose**: code that is safe to share, and data that
never is.

```bash
git clone https://github.com/beekeeper-lab/atticus.git ~/atticus
cd ~/atticus
```

Now your own vault. **It must be private** — it will accumulate ambient audio
from a device you wear.

```bash
gh repo create my-atticus-vault --private
./ops/init-vault.sh ~/atticus-vault      # creates inbox/ processed/ failures/ .state/
cd ~/atticus-vault
git remote add origin git@github.com:YOU/my-atticus-vault.git
git push -u origin main
```

**Give the host a deploy key rather than using your personal SSH key** — one key
per host, write access, independently revocable:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/atticus_vault -C "atticus-$(hostname)" -N ""
gh repo deploy-key add ~/.ssh/atticus_vault.pub --repo YOU/my-atticus-vault --allow-write
cat >> ~/.ssh/config <<'EOF'
Host github-atticus-vault
  HostName github.com
  User git
  IdentityFile ~/.ssh/atticus_vault
  IdentitiesOnly yes
EOF
git -C ~/atticus-vault remote set-url origin git@github-atticus-vault:YOU/my-atticus-vault.git
git -C ~/atticus-vault push   # must succeed before you go further
```

That last push is not optional ceremony. A vault that cannot push is the worst
failure this system has: work commits locally, the journal looks clean, and
nothing ever reaches the other half.

---

## 4. Credentials

Two files, and **neither is in the repo**.

### 4a. `~/.config/ai/env` — machine-wide API keys

```bash
mkdir -p ~/.config/ai && touch ~/.config/ai/env && chmod 600 ~/.config/ai/env
```

```bash
# ~/.config/ai/env
OPENAI_API_KEY=sk-...
```

The OpenAI key is the only **required** one — it pays for transcription, at
about $0.006/minute with `gpt-4o-transcribe`. Get it from
platform.openai.com. **Set a spend limit on the OpenAI account**; it is a key on
a machine that processes whatever a microphone hears.

### 4b. The agent's own auth

The agent runs `claude -p`. Two ways to authenticate it, and the second is
strongly preferred:

```bash
# Sign in once, interactively — this alone is enough to test with.
claude
```

That works while you are actively using Claude Code on the host, because its
access token lives **8 hours** and interactive sessions refresh it. An idle
overnight box will fail every run until you show up. So mint a long-lived token:

```bash
claude setup-token           # browser approval, then paste the code back
# copy the sk-ant-oat01-... it prints, then:
umask 077 && cat > ~/.secrets/claude-code-oauth-token   # paste, Enter, Ctrl-D
head -c 12 ~/.secrets/claude-code-oauth-token           # should read sk-ant-oat01
```

Then set `ATTICUS_CLAUDE_TOKEN_FILE` in step 5. This is Anthropic's supported
path for headless subscription use, lasts about a year, and has a security
benefit: your personal credential is then never bind-mounted into the sandbox at
all.

---

## 5. Configure

```bash
cd ~/atticus
cp ops/.env.example ops/.env && chmod 600 ops/.env
```

`ops/.env.example` is heavily commented and is the real reference —
[`configuration.md`](configuration.md) is generated from the code and lists
every setting. **The minimum that must be right:**

```bash
ATTICUS_VAULT_PATH=/home/YOU/atticus-vault
ATTICUS_WAKE_PHRASE=atticus
ATTICUS_NOTIFY_URL=https://ntfy.sh/<YOUR-UNGUESSABLE-TOPIC>
ATTICUS_CLAUDE_TOKEN_FILE=/home/YOU/.secrets/claude-code-oauth-token
```

Three notes that matter more than they look:

- **Set `ATTICUS_NOTIFY_URL`.** A dead pipeline is otherwise completely silent —
  a broken Plaud session and a quiet weekend look identical. Pick an
  unguessable ntfy topic; it is a bearer capability, so anyone with the URL can
  read your alarms. Install the ntfy app and subscribe to the same topic.
- **Keep the wake phrase.** Without it, *every* recording is executed by an
  autonomous agent, including the meeting you walked past.
- `ops/.env` is gitignored, and `ops/pr.sh` refuses to commit it or anything
  credential-shaped regardless.

---

## 6. Seed the Plaud session

Atticus reads Plaud's web API using a real browser session. There is no password
stored anywhere; you log in once and the session persists.

```bash
uv venv --python 3.13 ~/.local/share/claude-fetchers/venv
uv pip install --python ~/.local/share/claude-fetchers/venv/bin/python playwright
~/.local/share/claude-fetchers/venv/bin/python -m playwright install chromium

# Opens a real browser. Log in, then CLOSE THE WINDOW.
~/.local/share/claude-fetchers/venv/bin/python ingest/plaud_web.py login
```

Then prove it works:

```bash
~/.local/share/claude-fetchers/venv/bin/python ingest/plaud_web.py list --days 2 --json
```

You should see your recordings. Practical session life is Plaud's **30-day
refresh window**, not 24 hours, because the browser refreshes the token for us.

*On a headless host*: `login` needs a display. Run it on a desktop and `rsync`
`~/.local/share/claude-fetchers/sessions/` across.

---

## 7. Install

```bash
./ops/install.sh all        # or: ingest / processor, to split across two hosts
```

It preflights python, the vault, a real `git push --dry-run`, the `claude` CLI,
Playwright and your credential file **before** touching systemd, and it is safe
to re-run. Then check everything:

```bash
python3 atticus_cli.py      # the doctor — checks every precondition
```

Fix anything red. A yellow "no spend ceiling set" is fine and deliberate.

---

## 8. Test without hardware

Do this before trusting the pin. It synthesises speech, so transcription and the
agent both genuinely run:

```bash
python3 processor/mkfixture.py --clean --say "Atticus, research what an agentic harness is and write it up as HTML"
ATTICUS_VAULT_PATH=$PWD/.scratch-vault python3 processor/pipeline.py
```

You should see: transcribe → wake gate → agent → published. If the agent fails
with empty output, that is almost always authentication — revisit step 4b.

Then the real thing: record *"Atticus, research X"* on the pin, open the Plaud
app until it syncs, and wait. Or force it:

```bash
systemctl --user start atticus-ingest.service
journalctl --user -u atticus-ingest.service -n 30
```

---

## 9. See the output (recommended)

The vault browser turns the repo into a private searchable site — and it is also
where the todo list, projects and cost accounting live. It runs from the **vault
repo**, not this one:

```bash
cd ~/atticus-vault && ./site/install.sh     # see site/README.md
```

Reachable over Tailscale at `http://<host>/atticus`. Set
`ATTICUS_SITE_BASE_URL` in `ops/.env` so notifications carry a tappable link.

---

## 10. Optional integrations

Everything below is off until configured, and **a skill whose credential is
missing is not even offered to the agent** — so an unconfigured integration
cannot produce a confident report about an action that never had a chance.

Enable them roughly in this order — blast radius ascending.

| integration | what you need | setting(s) |
|---|---|---|
| **Todo + reminders** | nothing at all | work out of the box; reminders need `atticus-reminders.timer` enabled |
| **GitHub** | `gh auth login` (already done if you use `gh`) | `ATTICUS_GITHUB_REPOS=owner/repo` |
| **Calendar alerts** | Microsoft 365 app registration with `Calendars.ReadWrite` | `ATTICUS_OUTLOOK_ACCOUNT` |
| **Outlook drafts** | the same app, plus `Mail.ReadWrite` | as above |
| **Slack** | a bot app with **only** `chat:write`, invited to each channel | `ATTICUS_SLACK_BOT_TOKEN`, `ATTICUS_SLACK_CHANNELS` |
| **Azure DevOps** | a PAT | `ATTICUS_ADO_PAT`, `_ORG`, `_PROJECT` |
| **Signal** | `signal-cli` linked to your number | `ATTICUS_SIGNAL_RECIPIENTS` |
| **Meeting mode** | read [ADR-008](decisions/ADR-008-recording-other-people.md) first | `ATTICUS_MEETING_MODE=on` |

**The allowlists are the actual controls.** A spoken request may only *select*
from `ATTICUS_GITHUB_REPOS` or `ATTICUS_SLACK_CHANNELS` — it can never name a
target outside them. That is what stands between a mishearing and a message in
the wrong channel, so keep the lists short.

**On risk classes.** Verbs are `internal` (only you see it), `tracked` (visible
but recoverable) or `outward` (a message to a person, not recallable). Tracked
and outward default to `confirm`, which currently means *held* — see
[ADR-009](decisions/ADR-009-approval-arrives-out-of-band.md) for the approval
queue and how to make `confirm` actually usable. To let a specific verb run
unattended:

```bash
ATTICUS_OUTBOX_VERB_GITHUB_ISSUE=auto
```

Prefer per-verb over opening a whole class: opening `tracked` to let GitHub
issues flow also opens calendar invites to other people.

---

## 11. Living with it

```bash
python3 atticus_cli.py                        # the doctor: every precondition
python3 processor/pipeline.py --status        # the queue
python3 processor/todos.py list               # your todo list
python3 processor/reminders.py --list         # pending reminders
journalctl --user -u atticus-processor -f     # watch it work
systemctl --user list-timers 'atticus-*'      # are the timers armed?
```

An hourly heartbeat watches the timers, the vault, the agent credential and the
path watchers, and alarms to your ntfy topic. **Two multi-day outages in this
system's history were both delivery failures rather than detection failures** —
so if you find alarms drowning in routine pushes, that is what
`ATTICUS_QUIET_HOURS` and the calendar-escalation settings in
[ADR-010](decisions/ADR-010-severity-decides-the-channel.md) are for.

### When something breaks

| symptom | usual cause |
|---|---|
| no new recordings, ever | Plaud session expired → re-run `plaud_web.py login`; or you have not foregrounded the phone app |
| `UPSTREAM CHANGED` in the journal | Plaud changed their API or the token went stale — try `plaud_web.py list --days 1` by hand |
| agent exits 1 with no output | authentication → step 4b |
| notification link 404s | the site build has not run yet; check `atticus-vault-site.path` is active |
| everything queued, nothing processes | the processor timer, or a stale lock in `<vault>/.git/atticus-processor.lock` |
| an action says "held" and never happens | that verb's gate is `confirm` and no approval queue is configured — see step 10 |

---

## What to read next

- [`../README.md`](../README.md) — what the system is and the ideas behind it
- [`../SECURITY.md`](../SECURITY.md) — what is and is not defended against.
  **Read this before enabling Signal or Slack.**
- [`configuration.md`](configuration.md) — every setting, generated from the code
- [`decisions/`](decisions/) — the ADRs, which explain *why* rather than *how*
- [`../CLAUDE.md`](../CLAUDE.md) — the orientation file, if you are going to
  modify the code (or point an agent at it)
