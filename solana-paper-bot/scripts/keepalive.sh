#!/usr/bin/env bash
# Start the PAPER TRADING scanner and the dashboard if they are not running.
#   scripts/keepalive.sh          check once (used by cron every minute)
#   scripts/keepalive.sh --loop   check every 60 s forever (used at desktop login)
# Restarting is safe: the virtual balance and positions live in SQLite.
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p data

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> data/keepalive.log; }

check() {
  if ! pgrep -f "python -m paperbot scan" >/dev/null; then
    nohup .venv/bin/python -m paperbot scan >> data/scan.log 2>&1 < /dev/null &
    log "scanner was not running - started (pid $!)"
  fi
  if ! pgrep -f "streamlit run dashboard/app.py" >/dev/null; then
    nohup .venv/bin/streamlit run dashboard/app.py --server.port 8501 --server.headless true \
      >> data/dashboard.log 2>&1 < /dev/null &
    log "dashboard was not running - started (pid $!)"
  fi
}

if [ "${1:-}" = "--loop" ]; then
  # Only one loop at a time, even if login autostart fires more than once.
  exec 9> data/keepalive.lock
  flock -n 9 || exit 0
  while true; do check; sleep 60; done
else
  check
fi
