#!/usr/bin/env bash
# Re-seed the Plaud browser session and put it on the ingest host.
#
# Run this ON A MACHINE WITH A DISPLAY (WarDog). Plaud's refresh token lasts
# ~30 days from an interactive login and is not extended by use, so this is a
# recurring chore, not a repair — the heartbeat nudges once a day for the last
# five days of each window.
#
#   ops/reseed-plaud.sh                 # seed here, copy to the default host
#   ops/reseed-plaud.sh --host forge    # ...to a named host
#   ops/reseed-plaud.sh --local         # seed here only, copy nowhere
#
# Why not do this on Forge over VNC and skip the copy: Forge runs the
# autonomous agent, and the sandbox shares its network namespace (SECURITY.md),
# so a VNC server displaying a live logged-in Plaud session would sit inside
# the blast radius of anything spoken near the pin. The copy is cheap; that
# trade is not.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${PLAUD_VENV:-$HOME/.local/share/claude-fetchers/venv/bin/python}"
SESSIONS="${PLAUD_SESSION_ROOT:-$HOME/.local/share/claude-fetchers/sessions}"
HOST="${ATTICUS_INGEST_HOST:-forge}"
LOCAL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --local) LOCAL=1; shift ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }

[ -x "$VENV" ] || die "no fetcher venv at $VENV — see docs/START-HERE.md §6"
[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || die \
    "no display. login opens a real browser; run this on a desktop, not over a plain ssh session."

echo "==> Opening the browser. Log in, then CLOSE THE WINDOW."
"$VENV" "$REPO/ingest/plaud_web.py" login

# Prove it before shipping it. A profile that exists is not a profile that
# works, and copying a broken one over a working one would turn a chore into
# an outage.
echo "==> Verifying locally"
"$VENV" "$REPO/ingest/plaud_web.py" whoami >/dev/null || die "the new session does not work; not copying it anywhere"
"$VENV" "$REPO/ingest/plaud_web.py" session-age

if [ "$LOCAL" = 1 ]; then
    echo "==> --local: done, copied nowhere."
    exit 0
fi

echo "==> Copying to $HOST"
# --delete because a Chromium profile is a coherent whole: leaving one host's
# stale leveldb entries interleaved with another's is how you get a profile
# that loads but cannot authenticate.
rsync -a --delete "$SESSIONS/plaud/" "$HOST:$SESSIONS/plaud/"

echo "==> Verifying on $HOST"
ssh "$HOST" "$VENV $REPO/ingest/plaud_web.py whoami" >/dev/null \
    || die "copied, but $HOST cannot use it — check PLAUD_SESSION_ROOT in ops/.env there"
ssh "$HOST" "$VENV $REPO/ingest/plaud_web.py session-age"

echo "==> Restarting ingest on $HOST"
# reset-failed first: a unit parked in `failed` after a crash-loop stays there,
# and the timer's next tick would otherwise be the first thing to clear it.
ssh "$HOST" "systemctl --user reset-failed atticus-ingest.service atticus-heartbeat.service 2>/dev/null; systemctl --user start atticus-ingest.service"
echo "==> Done. Ingest is running again on $HOST."
