#!/bin/sh
# Launch the pipeline server. Run from anywhere; cds to its own dir.
cd "$(dirname "$0")" || exit 1
exec ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 "$@"
