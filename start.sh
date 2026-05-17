#!/bin/bash
# Native background launcher for local-dev runs.
# Pattern:
#   nohup ./start.sh > /tmp/coach-dev.log 2>&1 & disown
# so the server survives terminal close + IDE / agent session resets.
#
# Production runs go through docker-compose (which sets DATA_DIR=/data and
# APP_PASSWORD from .env); this script is just for hacking locally.
# APP_PORT default 8508 keeps it from colliding with sibling worktrees on 8507.

# Resolve the script's own directory so this works from any clone path.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$SCRIPT_DIR"

# No venv in this worktree by default — uses system python3. Install deps
# system-wide or shadow this with a venv if you need an isolated environment.
exec env APP_PORT="${APP_PORT:-8508}" \
         DATA_DIR="${DATA_DIR:-$SCRIPT_DIR/data/local}" \
         LOCAL_SEED_DIR="${LOCAL_SEED_DIR:-$SCRIPT_DIR/data/seed}" \
         python3 coach_app.py
