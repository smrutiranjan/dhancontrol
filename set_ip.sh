#!/bin/bash
# Ensure the Dhan PRIMARY static IP is set (runs set_ip.py; safe to run daily —
# it does nothing when the IP is already correct).
# Used with cron; optional DHANALGO_DIR override.
#
# Example crontab (IST on server — set TZ=Asia/Kolkata or adjust hours).
# Run after generate_token.sh so the token is fresh:
#   40 8 * * *  /bin/bash /var/www/algo/set_ip.sh
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

exec "$PYTHON" -u "$DIR/set_ip.py" >>"$LOG_DIR/set_ip.log" 2>&1
