#!/bin/bash
# Generate a fresh Dhan token and save it to cred.yml (runs generate_token.py).
# Used with cron; optional DHANALGO_DIR override.
#
# Example crontab (IST on server — set TZ=Asia/Kolkata or adjust hours):
#   30 8 * * *  /bin/bash /var/www/algo/generate_token.sh
#
# Env: DHANALGO_DIR=/path/to/DhanAlgo (default /var/www/algo)
set -euo pipefail
DIR="${DHANALGO_DIR:-/var/www/algo}"
cd "$DIR"
export PYTHONUNBUFFERED=1
LOG_DIR="$DIR/log"
mkdir -p "$LOG_DIR"

PYTHON="${DIR}/venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
  PYTHON="${DIR}/.venv/bin/python3"
fi
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3)"
fi

exec "$PYTHON" -u "$DIR/generate_token.py" >>"$LOG_DIR/generate_token.log" 2>&1
