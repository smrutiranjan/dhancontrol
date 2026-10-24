#!/bin/bash
# Stop trading_controls.py (and legacy trade_count_killswitch.py wrapper) processes.
#
# - Kills PID from trade_count_killswitch.pid when present (started via start_trade_count_killswitch.sh).
# - Always runs a cmdline sweep so cron/orphans/manual runs still terminate even without a pidfile.
# - Pattern matches trading_controls.py and trade_count_killswitch.py (not other monitors).
#
# Env: DHANALGO_DIR=/path/to/DhanAlgo (default /var/www/algo)

DIR="${DHANALGO_DIR:-/var/www/algo}"
LOG_DIR="$DIR/log"
PIDFILE="$DIR/trade_count_killswitch.pid"
DAILY_LOG="$LOG_DIR/trade_count_killswitch_$(date +%Y%m%d).log"

# Regex on full cmdline: space or slash before script name (matches relative + absolute paths).
PKILL_PATTERN='[ /](trading_controls|trade_count_killswitch)\.py'

append_log() {
  mkdir -p "$LOG_DIR" || true
  printf '%s [stop_trade_count_killswitch.sh] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >>"$DAILY_LOG" 2>/dev/null || true
}

send_sigterm_pid() {
  local pid="$1"
  [ -z "${pid:-}" ] && return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    append_log "SIGTERM pid $pid (pidfile)"
    return 0
  fi
  return 0
}

append_log "Stop script invoked (cron-safe full sweep)."

pid_from_file=""
if [ -f "$PIDFILE" ]; then
  pid_from_file=$(tr -d ' \n\r' <"$PIDFILE" || true)
  send_sigterm_pid "$pid_from_file"
else
  append_log "No pid file at $PIDFILE — still sweeping matching Python processes."
fi

if command -v pkill >/dev/null 2>&1; then
  # Terminate every Python running this exact script (covers duplicates / stale pidfile).
  pkill -TERM -f "$PKILL_PATTERN" 2>/dev/null || true
  append_log "SIGTERM all matching cmdlines (pkill -TERM -f trading_controls.py / trade_count_killswitch.py)."
else
  append_log "WARN: pkill not in PATH — install procps or rely on pidfile PID only."
fi

sleep 2

used_kill9=0

if [ -n "${pid_from_file:-}" ] && kill -0 "$pid_from_file" 2>/dev/null; then
  kill -KILL "$pid_from_file" 2>/dev/null || true
  used_kill9=1
  append_log "SIGKILL pid $pid_from_file (still alive after SIGTERM)."
fi

if command -v pkill >/dev/null 2>&1; then
  if pkill -KILL -f "$PKILL_PATTERN" 2>/dev/null; then
    used_kill9=1
    append_log "SIGKILL remaining matches (pkill -KILL -f trading_controls.py / trade_count_killswitch.py)."
  fi
fi

rm -f "$PIDFILE"

if [ "$used_kill9" = 1 ]; then
  msg="trading_controls.py fully stopped (SIGKILL used for one or more processes)."
else
  msg="trading_controls.py stopped (SIGTERM; no SIGKILL needed)."
fi
echo "$msg"
append_log "Stop script: $msg — pidfile removed; Python OS process(es) terminated (all threads exit with process)."
exit 0
