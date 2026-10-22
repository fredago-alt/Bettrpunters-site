#!/usr/bin/env bash
# Make the PAPER TRADING bot and dashboard start automatically after a reboot
# and restart within a minute if either stops. Safe to run more than once.
#   bash scripts/install-autostart.sh            install
#   bash scripts/install-autostart.sh --remove   uninstall (does not stop running processes)
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
KEEP="$DIR/scripts/keepalive.sh"
chmod +x "$KEEP"
MARK="# paperbot-keepalive"
AUTOSTART="$HOME/.config/autostart/paperbot-keepalive.desktop"

if [ "${1:-}" = "--remove" ]; then
  if command -v crontab >/dev/null; then
    (crontab -l 2>/dev/null | grep -v "$MARK") | crontab -
  fi
  rm -f "$AUTOSTART"
  echo "Autostart removed. Stop running processes with: pkill -f 'paperbot scan'; pkill -f 'streamlit run'"
  exit 0
fi

# 1. cron: at boot and every minute.
if ! command -v crontab >/dev/null && command -v apt-get >/dev/null; then
  echo "Installing cron..."
  (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cron) >/dev/null 2>&1 \
    || echo "  could not install cron (continuing with desktop autostart only)"
fi
if command -v crontab >/dev/null; then
  (crontab -l 2>/dev/null | grep -v "$MARK"
   echo "@reboot sleep 30 && $KEEP $MARK"
   echo "* * * * * $KEEP $MARK") | crontab -
  # Containers often have no init system starting cron; start it now if needed.
  if ! pgrep -x cron >/dev/null && ! pgrep -x crond >/dev/null; then
    (service cron start || cron) >/dev/null 2>&1
  fi
  if pgrep -x cron >/dev/null || pgrep -x crond >/dev/null; then
    echo "cron: installed and running (checks every minute)"
  else
    echo "cron: installed but the cron service is NOT running"
  fi
fi

# 2. Desktop login autostart (covers machines where cron does not start at boot).
mkdir -p "$(dirname "$AUTOSTART")"
cat > "$AUTOSTART" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Paperbot keepalive (PAPER TRADING ONLY)
Exec=$KEEP --loop
X-GNOME-Autostart-enabled=true
NoDisplay=true
DESKTOP
echo "desktop autostart: $AUTOSTART"

# 3. Make sure something is checking right now.
nohup "$KEEP" --loop >/dev/null 2>&1 < /dev/null &
sleep 3
echo
echo "Scanner running:   $(pgrep -fc 'python -m paperbot scan')"
echo "Dashboard running: $(pgrep -fc 'streamlit run dashboard/app.py')"
echo "Restart log:       $DIR/data/keepalive.log"
