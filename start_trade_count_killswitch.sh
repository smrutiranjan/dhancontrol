#!/bin/bash
# Start trade_count_killswitch.py in the background.
# Used with cron; optional DHANALGO_DIR override.
#
# Example crontab (IST on server — set TZ=Asia/Kolkata or adjust hours):
#   10 9 * * 1-5  /bin/bash /var/www/algo/start_trade_count_killswitch.sh
#
# Env: DHANALGO_DIR=/path/to/DhanAlgo (default /var/www/algo)
set -euo pipefail
DIR="${DHANALGO_DIR:-/var/www/algo}"
cd "$DIR"
export PYTHONUNBUFFERED=1
LOG_DIR="$DIR/log"
mkdir -p "$LOG_DIR"
PIDFILE="$DIR/trade_count_killswitch.pid"

PYTHON="${DIR}/venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
  PYTHON="${DIR}/.venv/bin/python3"
fi
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3)"
fi

if [ -f "$PIDFILE" ]; then
  oldpid=$(tr -d ' \n\r' < "$PIDFILE" || true)
  if [ -n "${oldpid:-}" ] && kill -0 "$oldpid" 2>/dev/null; then
    echo "Already running (pid $oldpid). Exit."
    exit 0
  fi
  rm -f "$PIDFILE"
fi

# Supervisor subshell: runs Python, forwards SIGTERM/SIGINT to it, and removes
# the pidfile the moment Python exits — whether it exits on its own (daily
# 3:15 PM IST cutoff, kill-switch triggered, fatal error) or is stopped
# externally. Without this, a self-exit would leave a stale pidfile behind
# until the next cron start silently cleaned it up.
(
  trap '' HUP
  trap 'kill -TERM "$child_pid" 2>/dev/null || true' TERM INT
  nohup "$PYTHON" -u "$DIR/trade_count_killswitch.py" \
    >>"$LOG_DIR/cron_stdout.log" 2>&1 &
  child_pid=$!
  wait "$child_pid" || true
  rm -f "$PIDFILE"
) &
echo $! >"$PIDFILE"
echo "Started trade_count_killswitch.py pid $(tr -d ' \n\r' < "$PIDFILE") (supervisor; pidfile self-cleans on exit)"
