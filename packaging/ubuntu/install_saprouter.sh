#!/usr/bin/env bash
# SAProuter on a VPN VM, so IBOPS on Windows can make RFC calls through the
# SSH tunnel (the client SAP gateways refuse requests addressed to 127.0.0.x).
#
#   sudo bash packaging/ubuntu/install_saprouter.sh <folder with the saprouter binary> host:port [host:port ...]
#   e.g. sudo bash packaging/ubuntu/install_saprouter.sh ~/saprouter 198.51.100.31:3300 198.51.100.57:3300
#
# The saprouter binary comes from SAP (SAP for Me -> Software Downloads ->
# SAPROUTER, Linux on x86_64, plus SAPCAR):  ./SAPCAR -xvf saprouter_*.SAR
#
# Security:
#   * saprouttab allows ONLY source 127.0.0.1 (the SSH tunnel arrives from this
#     VM itself) to ONLY the listed host:port pairs; everything else is denied.
#   * if ufw is active, port 3299 is closed to the network (localhost only).
#   * runs as its own system user, restarted by systemd.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
SRC="${1:?usage: install_saprouter.sh <folder with saprouter> host:port [...]}"; shift
[ "$#" -ge 1 ] || { echo "give at least one host:port (the SAP gateways, port 33NN)"; exit 1; }
for t in "$@"; do
  [[ "$t" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}:[0-9]{1,5}$ ]] || { echo "not ip:port: $t"; exit 1; }
done
BIN="$SRC"; [ -d "$SRC" ] && BIN="$SRC/saprouter"
[ -f "$BIN" ] || { echo "saprouter not found at $BIN -- extract it with SAPCAR first"; exit 1; }

id saprouter >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --home-dir /opt/saprouter saprouter
install -d -m 755 -o root -g root /opt/saprouter
install -m 755 "$BIN" /opt/saprouter/saprouter
[ -f "$SRC/niping" ] && install -m 755 "$SRC/niping" /opt/saprouter/niping || true
install -d -m 750 -o saprouter -g saprouter /var/log/saprouter

{
  echo "# IBOPS SAProuter -- written by install_saprouter.sh $(date +%F)"
  echo "# Only the SSH tunnel (source 127.0.0.1) to the listed SAP gateways."
  for t in "$@"; do echo "P 127.0.0.1 ${t%:*} ${t#*:}"; done
  echo "D * * *"
} > /opt/saprouter/saprouttab
chmod 644 /opt/saprouter/saprouttab

cat > /etc/systemd/system/saprouter.service <<UNIT
[Unit]
Description=SAProuter for IBOPS (RFC through the SSH tunnel)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=saprouter
WorkingDirectory=/var/log/saprouter
ExecStart=/opt/saprouter/saprouter -r -S 3299 -R /opt/saprouter/saprouttab -G /var/log/saprouter/saprouter.log -T /var/log/saprouter/dev_rout
ExecStop=/opt/saprouter/saprouter -s -S 3299
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw deny 3299/tcp >/dev/null && echo "ufw: 3299 closed to the network (the tunnel uses localhost)"
fi
systemctl daemon-reload
systemctl enable --now saprouter
sleep 2
systemctl --no-pager --lines=5 status saprouter || true
echo
echo "saprouttab:"; cat /opt/saprouter/saprouttab
echo
echo "Check: ss -ltnp | grep 3299"
