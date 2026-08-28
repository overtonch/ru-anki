#!/bin/sh
# Launched by the LaunchAgent. `caffeinate -is` keeps the machine (and its
# network) awake for as long as the server runs.
cd "$(dirname "$0")/.." || exit 1
exec /usr/bin/caffeinate -is ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
