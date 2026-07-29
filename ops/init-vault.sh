#!/usr/bin/env bash
# Scaffold a vault. Run this once, on your own private repo.
#
#   ./ops/init-vault.sh ~/my-vault
#
# Atticus names no vault. It reads ATTICUS_VAULT_PATH and expects four
# directories; this creates them so you do not have to guess the layout from
# the source. Safe to re-run — it only adds what is missing.
#
# It deliberately does NOT create the GitHub repo or the deploy key. Those are
# yours: make the repo private, generate a key per host, and grant it write
# access. Nothing here should ever hold a credential.
set -euo pipefail

DEST="${1:-}"
[[ -n "$DEST" ]] || { echo "usage: init-vault.sh <path-to-vault-checkout>" >&2; exit 2; }
DEST="${DEST/#\~/$HOME}"

ok(){ printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

mkdir -p "$DEST"
cd "$DEST"

if [[ ! -d .git ]]; then
  git init -q -b main
  ok "git repo initialised (remember: it must be PRIVATE)"
else
  ok "existing git repo"
fi

# inbox/   ingest writes here          } disjoint, so two hosts never conflict
# .state/  the per-host seen ledgers   }
# processed/ the processor writes here
# failures/  quarantined records
for d in inbox processed failures .state; do
  if [[ -d "$d" ]]; then
    ok "$d/"
  else
    mkdir -p "$d"
    # git will not track an empty directory, and the pipeline needs these to
    # exist on a fresh clone on the *other* host.
    touch "$d/.gitkeep"
    ok "created $d/"
  fi
done

if [[ ! -f .gitattributes ]]; then
  cat > .gitattributes <<'EOF'
# Audio is opaque binary — never let git try to diff or normalise it.
*.mp3  binary
*.wav  binary
*.opus binary
*.ogg  binary

# Machine-written, one JSON object per line, appended only. Treated as text so
# the benign-conflict union merge in processor/vault.py can operate on it.
*.jsonl text eol=lf
EOF
  ok "created .gitattributes"
else
  ok ".gitattributes"
fi

if [[ ! -f README.md ]]; then
  cat > README.md <<'EOF'
# vault

Private. Machine-written by [Atticus](https://github.com/beekeeper-lab/atticus).

| Path | Written by | Contents |
|------|-----------|----------|
| `inbox/YYYY/MM/` | ingest | original audio + a metadata JSON per recording |
| `processed/YYYY/MM/` | processor | transcripts, task prompts, agent output |
| `failures/YYYY/MM/` | processor | quarantined records with an error JSON |
| `.state/` | ingest | `seen-<host>.jsonl`, the append-only dedupe ledger |

**This repo is the queue, not just storage.** The two halves of the pipeline
communicate only through commits here, which is why commits land directly on
`main` with no PR — a PR per commit would add a merge step to every message.

Each recording's metadata JSON carries a `status` field, and that field *is* the
pipeline state: `raw → transcribed → routed → executed → published`, or
`failed`. Every stage commits, so a crash resumes instead of redoing work.

Contains audio recorded from a wearable. Keep it private.
EOF
  ok "created README.md"
else
  ok "README.md"
fi

echo
ok "vault scaffolded at $DEST"
cat <<EOF

Next:
  1. Create a PRIVATE repo and add it as 'origin'.
  2. Give each host a deploy key with WRITE access.
  3. Set ATTICUS_VAULT_PATH=$DEST in ops/.env
  4. ./ops/install.sh all
EOF
