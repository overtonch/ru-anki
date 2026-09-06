#!/bin/sh
# Launched by the LaunchAgent. `caffeinate -is` keeps the machine (and its
# network) awake for as long as the server runs.
cd "$(dirname "$0")/.." || exit 1

# Secrets (API keys etc.) — KEY=value lines, not in git. Survives restarts.
SECRETS="$HOME/Library/Application Support/ru-anki/secrets.env"
if [ -f "$SECRETS" ]; then
  set -a
  . "$SECRETS"
  set +a
fi

exec /usr/bin/caffeinate -is ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
