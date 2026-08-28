#!/bin/sh
# One-time setup for running ru-anki reliably on this Mac.
#   sh deploy/install.sh                 # local pieces
#   sh deploy/install.sh git@github.com:you/ru-anki-data.git   # + off-machine git backup
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
AS="$HOME/Library/Application Support/ru-anki"
GITDIR="$AS/data-git"

mkdir -p "$AS/backups" "$HOME/Library/Logs"

# --- off-machine git backup ---
if [ -n "$1" ]; then
  if [ ! -d "$GITDIR/.git" ]; then
    git init -q "$GITDIR"
    git -C "$GITDIR" commit -q --allow-empty -m "init"
    git -C "$GITDIR" branch -M main
  fi
  git -C "$GITDIR" remote remove origin 2>/dev/null || true
  git -C "$GITDIR" remote add origin "$1"
  git -C "$GITDIR" push -u origin main 2>&1 | tail -2 || \
    echo "  (push failed — create the repo first, then re-run)"
  echo "git backup -> $1"
else
  echo "no git remote given — off-machine backup NOT set up (see RELIABILITY.md)"
fi

# --- launchd agent: auto-start, auto-restart, keep the Mac awake ---
PLIST="$HOME/Library/LaunchAgents/com.ru-anki.server.plist"
cp "$HERE/deploy/com.ru-anki.server.plist" "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
sleep 5
if curl -sf http://127.0.0.1:8000/health >/dev/null; then
  echo "server: running (launchd, auto-restarts on crash and login)"
else
  echo "server: NOT healthy yet — check ~/Library/Logs/ru-anki.log"
fi

cat <<EOF

Still to do by hand (see deploy/RELIABILITY.md):
  sudo pmset -a autorestart 1 disksleep 0        # survive power blips
  System Settings > Users & Groups > auto-login  # recover after reboot unattended
  Tailscale: enable "serve", then: tailscale serve --bg 8000
  Leave the Mac plugged in, lid open.
EOF
