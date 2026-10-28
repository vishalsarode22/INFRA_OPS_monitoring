#!/usr/bin/env bash
# Run the Ubuntu dashboard as a service:
#   sudo bash packaging/ubuntu/install_dashboard_service.sh
# Starts at boot, restarts after a crash, restarts after 3 minutes of not
# answering, and never leaves processes behind holding port 8000.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
USER_NAME="${SUDO_USER:-ibops}"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
APP="${IBOPS_APP:-$HOME_DIR/app}"
VENV="${IBOPS_VENV:-$HOME_DIR/ibops/.venv}"
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$APP/dashboard/app.py" ] || { echo "No dashboard at $APP (set IBOPS_APP)"; exit 1; }
[ -x "$VENV/bin/python" ] || { echo "No virtualenv at $VENV (set IBOPS_VENV)"; exit 1; }
command -v fuser >/dev/null || apt-get install -y -qq psmisc
command -v curl  >/dev/null || apt-get install -y -qq curl

sed -e "s#__USER__#$USER_NAME#" -e "s#__APP__#$APP#" -e "s#__VENV__#$VENV#" \
    "$HERE/ibops-dashboard.service" > /etc/systemd/system/ibops-dashboard.service
install -m 644 "$HERE/ibops-dashboard-health.service" /etc/systemd/system/
install -m 644 "$HERE/ibops-dashboard-health.timer" /etc/systemd/system/
install -m 755 "$HERE/ibops-dashboard-health.sh" /usr/local/bin/ibops-dashboard-health

# Stop any dashboard started by hand, children included.
pkill -9 -u "$USER_NAME" -f "uvicorn dashboard.app" || true
sleep 2
systemctl daemon-reload
systemctl enable --now ibops-dashboard.service ibops-dashboard-health.timer
sleep 8
systemctl --no-pager --lines=5 status ibops-dashboard.service || true
curl -fsS -m 10 -o /dev/null http://127.0.0.1:8000/healthz && echo "dashboard answering on 127.0.0.1:8000"
