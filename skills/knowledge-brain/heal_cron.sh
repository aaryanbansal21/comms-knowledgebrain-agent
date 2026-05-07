#!/bin/zsh
# Self-healing cron script for the knowledge brain.
# Scheduled to run every 2 days via crontab.
# API keys and the repo path are read from ~/.hourglass/env.sh — never committed.

ENV_FILE="$HOME/.hourglass/env.sh"

if [ ! -f "$ENV_FILE" ]; then
  echo "[kb-heal] ERROR: $ENV_FILE not found. See README for setup instructions." >&2
  exit 1
fi

source "$ENV_FILE"

if [ -z "$HOURGLASS_DIR" ]; then
  echo "[kb-heal] ERROR: HOURGLASS_DIR not set in $ENV_FILE." >&2
  exit 1
fi

cd "$HOURGLASS_DIR/skills/knowledge-brain" || exit 1
python3 scripts/heal.py all >> /tmp/kb_heal.log 2>&1
