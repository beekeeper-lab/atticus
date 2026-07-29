#!/usr/bin/env bash
# Atticus deployment. Idempotent — safe to re-run.
#
#   ./ops/install.sh forge     processor: transcribe → route → execute → publish
#   ./ops/install.sh wardog    ingest: Plaud → vault   (not yet implemented)
#
# Installs user-level systemd units. No sudo, no system services.
set -euo pipefail

ROLE="${1:-}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNITS="$HOME/.config/systemd/user"
AI_ENV="$HOME/.config/ai/env"

die(){ printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
ok(){  printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

[[ "$ROLE" == forge || "$ROLE" == wardog ]] \
  || die "usage: install.sh {forge|wardog}"

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

"$PY" -c 'import requests' 2>/dev/null && ok "python requests" \
  || warn "python 'requests' missing — pip install requests"

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
    warn "vault not found at '${VAULT:-unset}' — clone atticus-vault and set ATTICUS_VAULT_PATH"
  fi
else
  warn "no ops/.env — copy ops/.env.example and fill it in"
fi

# ---- role-specific --------------------------------------------------------
echo
echo "Role: $ROLE"

if [[ "$ROLE" == forge ]]; then
  command -v claude >/dev/null && ok "claude $(claude --version 2>/dev/null | head -1)" \
    || die "claude CLI not found — the processor cannot execute without it"

  mkdir -p "$UNITS"
  for u in atticus-processor.service atticus-processor.timer; do
    sed "s|%h/atticus|$REPO|g" "$REPO/ops/$u" > "$UNITS/$u"
    ok "installed $u"
  done

  if [[ -n "${VAULT:-}" ]]; then
    if ! grep -q "ReadWritePaths=.*$VAULT" "$UNITS/atticus-processor.service"; then
      sed -i "s|^ReadWritePaths=\(.*\)$|ReadWritePaths=\1 $VAULT|" "$UNITS/atticus-processor.service"
      ok "granted the unit write access to the vault"
    fi
  else
    warn "vault path unknown — edit ReadWritePaths in $UNITS/atticus-processor.service by hand"
  fi

  systemctl --user daemon-reload
  systemctl --user enable --now atticus-processor.timer
  ok "timer enabled (every 5 min)"
  echo
  systemctl --user list-timers atticus-processor.timer --no-pager 2>/dev/null | head -3

else
  warn "wardog ingest is not implemented yet — blocked on the Plaud transport"
  warn "decision (SPEC §2.2.1): direct BLE, Android bridge, or the web fetcher"
  exit 0
fi

cat <<EOF

Done.

  status   systemctl --user status atticus-processor.service
  logs     journalctl --user -u atticus-processor -f
  queue    python3 $REPO/processor/pipeline.py --status
  run now  systemctl --user start atticus-processor.service
EOF
