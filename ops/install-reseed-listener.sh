#!/usr/bin/env bash
# Install the "Re-login now" listener as a user service. Run on a machine WITH A
# DISPLAY (WarDog), not Forge. Needs ATTICUS_RESEED_TOPIC_URL in ops/.env.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
chmod +x "$REPO/ops/reseed-listener.py" "$REPO/ops/reseed-plaud.sh"
cat > "$UNIT_DIR/atticus-reseed-listener.service" <<EOF
[Unit]
Description=Atticus re-login listener — opens the Plaud login when the phone button is tapped
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 $REPO/ops/reseed-listener.py
Restart=always
RestartSec=10
SyslogIdentifier=atticus-reseed-listener

[Install]
WantedBy=graphical-session.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now atticus-reseed-listener.service
systemctl --user --no-pager status atticus-reseed-listener.service | head -5
