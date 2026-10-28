#!/usr/bin/env bash
# Install the InfraBeatOps VPN watchdog for one client VPN (default: pune_ttml).
#
#   sudo bash packaging/vpn/install_vpn_watchdog.sh pune_ttml 203.0.113.2 <port>
#
# What it does, in order -- it stops at the first problem:
#   1. refuses to run while root's password is a known weak one
#   2. installs openfortivpn
#   3. fetches the gateway certificate and checks its SHA1 against the one
#      you approved (16:B7:...:7D:55:3A:99); pins its SHA256 for openfortivpn
#   4. asks for the VPN user and password and writes them to
#      /etc/openfortivpn/<name>.conf (root only, mode 600)
#   5. installs and starts ibops-vpn@<name> (systemd), disconnects FortiClient
set -euo pipefail

NAME="${1:-pune_ttml}"
HOST="${2:-203.0.113.2}"
PORT="${3:-}"
EXPECTED_SHA1="${IBOPS_VPN_EXPECTED_SHA1:-00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33}"
HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
[ -n "$PORT" ] || { echo "Usage: sudo bash $0 <name> <gateway-host> <gateway-port>"; exit 1; }

echo "== 1. root password check"
python3 - <<'PY'
import sys, warnings
warnings.filterwarnings("ignore")
try:
    import crypt
except ImportError:
    print("   (cannot check here: python crypt module missing) -- make sure root's password is strong.")
    sys.exit(0)
line = next((l for l in open("/etc/shadow") if l.startswith("root:")), "")
h = line.split(":")[1] if line else ""
if h in ("", "!", "*", "!*", "!!") or h.startswith("!"):
    print("   root has no usable password (locked): OK"); sys.exit(0)
weak = ["root", "toor", "password", "Password", "admin", "ibops", "ubuntu", "123456", "12345678",
        "Welcome@123", "Admin@123", "root123", "Root@123"]
for w in weak:
    if crypt.crypt(w, h) == h:
        print(f"   STOP: root's password is a known weak one. Change it first:  sudo passwd root")
        sys.exit(2)
print("   root password is not a known weak one: OK")
PY

echo "== 2. install openfortivpn"
command -v openfortivpn >/dev/null || { apt-get update -qq && apt-get install -y -qq openfortivpn openssl; }
openfortivpn --version || true

echo "== 3. gateway certificate"
CERT="$(echo | timeout 15 openssl s_client -connect "$HOST:$PORT" -servername "$HOST" 2>/dev/null | openssl x509)"
[ -n "$CERT" ] || { echo "   STOP: could not fetch a certificate from $HOST:$PORT"; exit 3; }
SHA1="$(echo "$CERT" | openssl x509 -noout -fingerprint -sha1 | cut -d= -f2)"
SHA256="$(echo "$CERT" | openssl x509 -noout -fingerprint -sha256 | cut -d= -f2 | tr -d ':' | tr 'A-F' 'a-f')"
echo "   SHA1 seen:     $SHA1"
echo "   SHA1 expected: $EXPECTED_SHA1"
[ "${SHA1^^}" = "${EXPECTED_SHA1^^}" ] || { echo "   STOP: certificate differs from the approved one. Not continuing."; exit 4; }
echo "   matches; pinning SHA256 $SHA256"

echo "== 4. VPN credentials (stored root-only in /etc/openfortivpn/$NAME.conf)"
read -r -p "   VPN user [infra1]: " VUSER; VUSER="${VUSER:-infra1}"
while true; do
  read -r -s -p "   VPN password: " P1; echo
  read -r -s -p "   again: " P2; echo
  [ -n "$P1" ] && [ "$P1" = "$P2" ] && break
  echo "   empty or different, try again"
done
install -d -m 700 /etc/openfortivpn
umask 077
cat > "/etc/openfortivpn/$NAME.conf" <<CONF
# InfraBeatOps -- $NAME. Root-only. Written by install_vpn_watchdog.sh $(date +%F)
host = $HOST
port = $PORT
username = $VUSER
password = $P1
trusted-cert = $SHA256
# Do not touch DNS (monitoring uses IP addresses; FortiClient's DNS step
# was what failed before). Routes to the client network are still set.
set-dns = 0
pppd-use-peerdns = 0
# Connectivity probes (the watchdog tests one of these every minute; "up"
# means at least one answers). IBOPS_VPN_PROBES="host:port host:port" at install.
$(for p in ${IBOPS_VPN_PROBES:-}; do echo "# ibops-probe = $p"; done)
CONF
chmod 600 "/etc/openfortivpn/$NAME.conf"; chown root:root "/etc/openfortivpn/$NAME.conf"
unset P1 P2

echo "== 5. service"
install -d -m 755 /opt/ibops-vpn
install -m 755 "$HERE/ibops_vpn_watchdog.py" /opt/ibops-vpn/ibops_vpn_watchdog.py
install -m 644 "$HERE/ibops-vpn@.service" /etc/systemd/system/ibops-vpn@.service
cat > /usr/local/bin/ibops-vpn <<'WRAP'
#!/bin/sh
exec /usr/bin/python3 /opt/ibops-vpn/ibops_vpn_watchdog.py "$@"
WRAP
chmod 755 /usr/local/bin/ibops-vpn

# FortiClient and openfortivpn must not hold the same account at once.
if [ -x /opt/forticlient/forticlient-cli ] && [ -n "${SUDO_USER:-}" ]; then
  sudo -u "$SUDO_USER" /opt/forticlient/forticlient-cli vpn disconnect >/dev/null 2>&1 || true
  echo "   FortiClient disconnected (leave it disconnected from now on)."
fi

systemctl daemon-reload
systemctl enable --now "ibops-vpn@$NAME"
echo "   waiting for the tunnel..."
for i in $(seq 1 30); do
  st="$(python3 -c "import json;print(json.load(open('/run/ibops-vpn/$NAME.json')).get('state',''))" 2>/dev/null || true)"
  [ "$st" = "up" ] && break
  sleep 2
done
ibops-vpn status "$NAME"
echo
echo "Done. Check: journalctl -u ibops-vpn@$NAME -n 30"
