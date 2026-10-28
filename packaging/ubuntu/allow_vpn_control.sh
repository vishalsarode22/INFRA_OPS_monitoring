#!/usr/bin/env bash
# Let the dashboard's Settings page connect / disconnect the client VPN(s)
# run by the ibops-vpn watchdog. Run ONCE on the Ubuntu VM:
#
#   sudo bash packaging/ubuntu/allow_vpn_control.sh            # all VPNs (/etc/openfortivpn, /etc/ibops-vpn)
#   sudo bash packaging/ubuntu/allow_vpn_control.sh pune_ttml  # just this one
#
# It allows the dashboard user (the one who ran sudo) to run EXACTLY these
# commands without a password, and nothing else:
#   systemctl start ibops-vpn@<name>
#   systemctl stop  ibops-vpn@<name>
#   /usr/local/bin/ibops-vpn reset <name>
# It also installs the current watchdog script (it now reports "off" when
# the service is stopped). The VPN password stays root-only in
# /etc/openfortivpn/<name>.conf; the dashboard never sees it.
# Remove with: sudo rm /etc/sudoers.d/ibops-vpn-control
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
# The user the dashboard service runs as (User= in its unit); else whoever ran sudo.
USER_NAME="$(systemctl show -p User --value ibops-dashboard.service 2>/dev/null || true)"
[ -n "$USER_NAME" ] || USER_NAME="${SUDO_USER:-}"
[ -n "$USER_NAME" ] && [ "$USER_NAME" != "root" ] || { echo "Cannot tell the dashboard's user. Run it with sudo from the dashboard user's account (e.g. ibops)."; exit 1; }
id "$USER_NAME" >/dev/null 2>&1 || { echo "User $USER_NAME does not exist."; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "$#" -gt 0 ]; then NAMES="$*"; else
  NAMES="$(ls /etc/openfortivpn/*.conf /etc/ibops-vpn/*.conf 2>/dev/null | xargs -r -n1 basename | sed 's/\.conf$//' | sort -u || true)"
fi
[ -n "$NAMES" ] || { echo "No VPN found in /etc/openfortivpn or /etc/ibops-vpn. Install the watchdog first (packaging/vpn)."; exit 1; }

SYSTEMCTL="/usr/bin/systemctl"; [ -x "$SYSTEMCTL" ] || SYSTEMCTL="/bin/systemctl"
CMDS=""
for n in $NAMES; do
  [[ "$n" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "skipping odd name: $n"; continue; }
  CMDS="$CMDS, $SYSTEMCTL start ibops-vpn@$n, $SYSTEMCTL stop ibops-vpn@$n, /usr/local/bin/ibops-vpn reset $n"
done
CMDS="${CMDS#, }"

TMP="$(mktemp)"
cat > "$TMP" <<RULE
# InfraBeatOps: the dashboard may switch its client VPN(s) -- nothing else.
# Written by packaging/ubuntu/allow_vpn_control.sh $(date +%F)
$USER_NAME ALL=(root) NOPASSWD: $CMDS
RULE
visudo -cf "$TMP" >/dev/null || { echo "STOP: the rule did not validate; nothing changed."; rm -f "$TMP"; exit 2; }
install -m 440 -o root -g root "$TMP" /etc/sudoers.d/ibops-vpn-control
rm -f "$TMP"
echo "== allowed for $USER_NAME:"
sed -n '3p' /etc/sudoers.d/ibops-vpn-control | tr ',' '\n' | sed 's/^/   /'

# Current watchdog script (reports "off" when stopped). Takes effect at the
# next start of the service -- the running tunnel is NOT restarted here.
if [ -f "$HERE/../vpn/ibops_vpn_watchdog.py" ] && [ -d /opt/ibops-vpn ]; then
  install -m 755 "$HERE/../vpn/ibops_vpn_watchdog.py" /opt/ibops-vpn/ibops_vpn_watchdog.py
  echo "== watchdog script updated (used from the next VPN start; the tunnel was not touched)"
fi
echo "Done. Open Settings on the dashboard."
