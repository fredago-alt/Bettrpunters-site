#!/usr/bin/env bash
# Start the PAPER TRADING scanner, the sniper (if SNIPER_ENABLED=true in .env)
# and the dashboard if they are not running.
#   scripts/keepalive.sh          check once (used by cron every minute)
#   scripts/keepalive.sh --loop   check every 60 s forever (used at desktop login)
# Restarting is safe: the virtual balance and positions live in SQLite.
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p data

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> data/keepalive.log; }

# Start a long-running program detached, WITHOUT inheriting fd 9 (the loop's lock).
# Otherwise the child would keep holding the lock after the loop exits, and a new
# loop (e.g. after an update) could never start.
spawn() { local out="$1"; shift; nohup "$@" >> "$out" 2>&1 < /dev/null 9>&- & }

check() {
  if ! pgrep -f "python -m paperbot scan" >/dev/null; then
    spawn data/scan.log .venv/bin/python -m paperbot scan
    log "scanner was not running - started (pid $!)"
  fi
  if grep -qs '^SNIPER_ENABLED=true' .env; then
    # '$' anchors the match so the main sniper and each variant are checked separately.
    if ! pgrep -f "python -m paperbot sniper\$" >/dev/null; then
      spawn data/sniper.log .venv/bin/python -m paperbot sniper
      log "sniper was not running - started (pid $!)"
    fi
    # Side-by-side test variants: one per variants/<name>.env file.
    for f in variants/*.env; do
      [ -e "$f" ] || continue
      name="$(basename "$f" .env)"
      if ! pgrep -f "python -m paperbot sniper --variant $name\$" >/dev/null; then
        spawn "data/sniper-$name.log" .venv/bin/python -m paperbot sniper --variant "$name"
        log "sniper variant $name was not running - started (pid $!)"
      fi
    done
  fi
  if ! pgrep -f "streamlit run dashboard/app.py" >/dev/null; then
    spawn data/dashboard.log .venv/bin/streamlit run dashboard/app.py --server.port 8501 --server.headless true
    log "dashboard was not running - started (pid $!)"
  fi
}

if [ "${1:-}" = "--loop" ]; then
  # Only one loop at a time, even if login autostart fires more than once.
  exec 9> data/keepalive.lock
  flock -n 9 || exit 0
  while true; do check; sleep 60 9>&-; done
else
  check
fi
