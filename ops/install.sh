#!/usr/bin/env bash
# Atticus deployment. Idempotent — safe to re-run.
#
#   ./ops/install.sh ingest       Plaud Cloud → vault        (needs a Plaud session)
#   ./ops/install.sh processor    vault → transcribe/route/execute → vault
#   ./ops/install.sh all          both, on one host
#
# Roles are CAPABILITIES, not hostnames. `forge` and `wardog` are accepted as
# legacy aliases for `processor` and `ingest`, but nothing here assumes a
# particular machine — the point is that you can clone this repo, point
# ATTICUS_VAULT_PATH at your own vault, and run it.
#
# Installs user-level systemd units. No sudo, no system services.
set -euo pipefail

ROLE="${1:-}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNITS="$HOME/.config/systemd/user"
AI_ENV="$HOME/.config/ai/env"
FETCHER_VENV="$HOME/.local/share/claude-fetchers/venv/bin/python"

die(){ printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
ok(){  printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

case "$ROLE" in
  forge)  ROLE=processor ;;
  wardog) ROLE=ingest ;;
esac
[[ "$ROLE" == ingest || "$ROLE" == processor || "$ROLE" == all ]] \
  || die "usage: install.sh {ingest|processor|all}"

DO_INGEST=0; DO_PROCESSOR=0; SEEDED=1
[[ "$ROLE" == ingest    || "$ROLE" == all ]] && DO_INGEST=1
[[ "$ROLE" == processor || "$ROLE" == all ]] && DO_PROCESSOR=1

echo "Installing role '$ROLE' from $REPO"

# ---- shared preflight -----------------------------------------------------
echo
echo "Preflight"
command -v git >/dev/null || die "git not found"
ok "git $(git --version | awk '{print $3}')"

PY=$(command -v python3) || die "python3 not found"
"$PY" - <<'EOF' || die "python 3.11+ required"
import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)
EOF
ok "python $("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

if [[ -f "$AI_ENV" ]]; then
  P=$(stat -c%a "$AI_ENV")
  [[ "$P" == 600 ]] && ok "$AI_ENV (600)" || warn "$AI_ENV is mode $P — want 600"
  grep -qE '^\s*(export\s+)?OPENAI_API_KEY=' "$AI_ENV" \
    && ok "OPENAI_API_KEY present" || die "OPENAI_API_KEY missing from $AI_ENV"
else
  die "shared credential file not found: $AI_ENV"
fi

if [[ -f "$REPO/ops/.env" ]]; then
  ok "ops/.env present"
  VAULT=$(grep -E '^\s*ATTICUS_VAULT_PATH=' "$REPO/ops/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'" )
  VAULT="${VAULT/#\~/$HOME}"
  if [[ -n "${VAULT:-}" && -d "$VAULT/.git" ]]; then
    ok "vault: $VAULT"
  else
    warn "vault not found at '${VAULT:-unset}' — clone your vault and set ATTICUS_VAULT_PATH"
  fi

  # A push that cannot authenticate is the worst failure mode this system has:
  # work is committed, the journal looks clean, and nothing reaches the other
  # half. Check it here rather than discovering it in production.
  if [[ -n "${VAULT:-}" && -d "$VAULT/.git" ]]; then
    if git -C "$VAULT" remote | grep -q .; then
      if GIT_SSH_COMMAND="ssh -F $HOME/.ssh/config" \
         git -C "$VAULT" push --dry-run >/dev/null 2>&1; then
        ok "vault push authenticates"
      else
        warn "cannot push to the vault remote — check the deploy key"
      fi
    else
      warn "vault has no remote; commits will stay local"
    fi
  fi
else
  warn "no ops/.env — copy ops/.env.example and fill it in"
fi

# ---- helpers --------------------------------------------------------------

# Append the vault to the unit's ACTIVE ReadWritePaths line.
#
# The guard deliberately ignores comments. It used to grep the whole file, and
# the unit template shipped a commented example line containing a vault path —
# so the guard matched the comment, decided the work was done, and left the
# real line alone. The unit then ran with a read-only vault and every commit
# failed. Anchor to ^ReadWritePaths= and nothing else.
grant_vault() {
  local unit="$1"
  if [[ -z "${VAULT:-}" ]]; then
    warn "vault path unknown — edit ReadWritePaths in $unit by hand"
    return
  fi
  if grep -qE "^ReadWritePaths=.*(^| )${VAULT}( |$)" "$unit"; then
    ok "vault already writable by $(basename "$unit")"
    return
  fi
  sed -i "s|^ReadWritePaths=\(.*\)$|ReadWritePaths=\1 $VAULT|" "$unit"
  grep -qE "^ReadWritePaths=.* ${VAULT}( |$)" "$unit" \
    && ok "granted $(basename "$unit") write access to the vault" \
    || die "failed to patch ReadWritePaths in $unit"
}

install_units() {
  mkdir -p "$UNITS"
  for u in "$@"; do
    sed "s|%h/atticus|$REPO|g" "$REPO/ops/$u" > "$UNITS/$u"
    ok "installed $u"
  done
}

# ---- ingest ---------------------------------------------------------------
if (( DO_INGEST )); then
  echo
  echo "Role: ingest"

  [[ -x "$FETCHER_VENV" ]] && ok "fetchers venv" \
    || die "missing $FETCHER_VENV — create it: uv venv --python 3.13 \
$HOME/.local/share/claude-fetchers/venv && uv pip install \
--python $FETCHER_VENV playwright && $FETCHER_VENV -m playwright install chromium"
  "$FETCHER_VENV" -c 'import playwright' 2>/dev/null && ok "playwright" \
    || die "playwright not in the fetchers venv"
  "$FETCHER_VENV" - <<'EOF' 2>/dev/null && ok "chromium present" \
    || warn "chromium missing — run: $FETCHER_VENV -m playwright install chromium"
from playwright.sync_api import sync_playwright
import os, sys
with sync_playwright() as p:
    sys.exit(0 if os.path.exists(p.chromium.executable_path) else 1)
EOF

  SESS="${PLAUD_SESSION_ROOT:-$HOME/.local/share/claude-fetchers/sessions}/plaud"
  if [[ -d "$SESS" ]] && [[ -n "$(ls -A "$SESS" 2>/dev/null)" ]]; then
    ok "Plaud session seeded"
  else
    # Install the units but leave the timer off. An enabled timer with no
    # session is 96 auth failures a day that tell you nothing you did not
    # already know, and it buries the real alarm when one arrives.
    SEEDED=0
    warn "no Plaud session at $SESS — seed it with:"
    warn "    $FETCHER_VENV $REPO/ingest/plaud_web.py login"
    warn "  (needs a display; on a headless host, rsync a seeded session dir in)"
    warn "leaving the ingest timer DISABLED until then; re-run this to enable it"
  fi

  install_units atticus-ingest.service atticus-ingest.timer
  grant_vault "$UNITS/atticus-ingest.service"
fi

# ---- processor ------------------------------------------------------------
if (( DO_PROCESSOR )); then
  echo
  echo "Role: processor"

  command -v claude >/dev/null && ok "claude $(claude --version 2>/dev/null | head -1)" \
    || die "claude CLI not found — the processor cannot execute without it"
  # The unit's PATH is not your shell's. This is exactly how "claude not found"
  # got past a green preflight once already.
  [[ -x "$HOME/.local/bin/claude" || -x /usr/local/bin/claude || -x /usr/bin/claude ]] \
    || warn "claude is not in ~/.local/bin, /usr/local/bin or /usr/bin — the unit's \
PATH will not find it; add its directory to Environment=PATH in the service"

  "$PY" -c 'import requests' 2>/dev/null && ok "python requests" \
    || warn "python 'requests' missing — the transcribe stage needs it: pip install requests"

  install_units atticus-processor.service atticus-processor.timer
  grant_vault "$UNITS/atticus-processor.service"

  # The heartbeat watches everything else. It alarms on ABSENCE, which is the
  # one failure mode none of the other alarms can see.
  install_units atticus-heartbeat.service atticus-heartbeat.timer
fi

# ---- enable ---------------------------------------------------------------
echo
systemctl --user daemon-reload
if (( DO_INGEST )); then
  if (( SEEDED )); then
    systemctl --user enable --now atticus-ingest.timer
    ok "ingest timer enabled (every 15 min)"
  else
    warn "ingest timer installed but NOT enabled — no Plaud session yet"
  fi
fi
if (( DO_PROCESSOR )); then
  systemctl --user enable --now atticus-processor.timer
  ok "processor timer enabled (every 5 min)"
  systemctl --user enable --now atticus-heartbeat.timer
  ok "heartbeat enabled (hourly)"
fi
echo
systemctl --user list-timers 'atticus-*' --no-pager 2>/dev/null | head -5

cat <<EOF

Done.

  ingest logs      journalctl --user -u atticus-ingest -f
  processor logs   journalctl --user -u atticus-processor -f
  ledger           $FETCHER_VENV $REPO/ingest/poller.py --status
  session health   $FETCHER_VENV $REPO/ingest/poller.py --health
  queue            python3 $REPO/processor/pipeline.py --status
EOF
