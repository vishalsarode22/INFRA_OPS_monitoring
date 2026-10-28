#!/usr/bin/env bash
# Install a FortiGate IPsec VPN (pre-shared key + XAuth user/password, what
# FortiClient calls "IPsec VPN") under the InfraBeatOps VPN watchdog.
# openfortivpn cannot do IPsec, so this uses strongSwan. Same rules as the
# SSL-VPN watchdog: 3 tries then stop; never push another session off.
#
#   sudo bash packaging/vpn/install_ipsec_vpn.sh ceat 203.0.113.189
#
# Optional, when you know them (FortiClient > Edit > Advanced Settings);
# otherwise the common FortiClient settings are tried one by one:
#   IBOPS_IPSEC_MODE=main|aggressive    IBOPS_IPSEC_DH=2|5|14
#   IBOPS_IPSEC_PFS=yes|no              IBOPS_IPSEC_LOCALID=<Local ID>
#   IBOPS_IPSEC_REMOTE=10.250.0.0/16     (only this network goes through the VPN)
#   IBOPS_VPN_PROBES="10.250.10.135:3310 10.250.20.132:3300"
#
# The pre-shared key and the password are typed here (hidden) and stored
# root-only in /etc/ipsec.d/ibops-<name>.secrets (mode 600). Nothing else
# on this machine -- not the dashboard -- can read them.
set -euo pipefail

NAME="${1:-}"
GW="${2:-}"
REMOTE="${IBOPS_IPSEC_REMOTE:-10.250.0.0/16}"
PROBES="${IBOPS_VPN_PROBES:-10.250.10.135:3310 10.250.20.132:3300 10.250.20.153:3300 10.250.20.140:3300}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CONN="ibops-$NAME"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
[ -n "$NAME" ] && [ -n "$GW" ] || { echo "Usage: sudo bash $0 <name> <gateway-ip>"; exit 1; }
[[ "$NAME" =~ ^[a-z0-9_]{1,30}$ ]] || { echo "name: lower-case letters, digits, _ only"; exit 1; }
[[ "$GW" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "gateway must be an IP address"; exit 1; }
[[ "$REMOTE" =~ ^[0-9./,]+$ ]] || { echo "IBOPS_IPSEC_REMOTE must be networks like 10.250.0.0/16"; exit 1; }
case "$REMOTE" in *0.0.0.0/0*) echo "STOP: 0.0.0.0/0 would send ALL traffic (and the Windows tunnel) into the VPN."; exit 1;; esac

echo "== 1. root password check"
python3 - <<'PY'
import sys, warnings
warnings.filterwarnings("ignore")
try:
    import crypt
except ImportError:
    print("   (cannot check here) -- make sure root's password is strong."); sys.exit(0)
line = next((l for l in open("/etc/shadow") if l.startswith("root:")), "")
h = line.split(":")[1] if line else ""
if h in ("", "!", "*", "!*", "!!") or h.startswith("!"):
    print("   root has no usable password (locked): OK"); sys.exit(0)
for w in ["root", "toor", "password", "Password", "admin", "ibops", "ubuntu", "123456", "12345678",
          "Welcome@123", "Admin@123", "root123", "Root@123"]:
    if crypt.crypt(w, h) == h:
        print("   STOP: root's password is a known weak one. Change it first:  sudo passwd root"); sys.exit(2)
print("   root password is not a known weak one: OK")
PY

echo "== 2. install strongSwan"
XAUTH_SO=/usr/lib/ipsec/plugins/libstrongswan-xauth-generic.so
if ! command -v ipsec >/dev/null || [ ! -f "$XAUTH_SO" ]; then
  apt-get update -qq
  apt-get install -y -qq strongswan strongswan-starter
  # XAuth (the user/password step) is a separate plugin package.
  apt-get install -y -qq libcharon-extauth-plugins || apt-get install -y -qq libcharon-extra-plugins || true
fi
[ -f "$XAUTH_SO" ] || { echo "   STOP: strongSwan's XAuth plugin is missing ($XAUTH_SO)."; exit 2; }
ipsec --version | head -1 || true

echo "== 3. strongSwan settings for this VPN"
install -d -m 755 /etc/strongswan.d
cat > /etc/strongswan.d/ibops-vpn.conf <<'CONF'
# InfraBeatOps: give up a connection attempt after ~50 s (the watchdog does
# the retrying, by its rules), and never let the VPN change this machine's DNS.
charon {
    retransmit_tries = 3
    retransmit_timeout = 4.0
    retransmit_base = 1.8
    plugins {
        resolve {
            load = no
        }
    }
}
CONF
grep -q "^include /etc/ipsec.d/ibops-\*.conf" /etc/ipsec.conf 2>/dev/null || \
  printf '\n# InfraBeatOps client VPNs\ninclude /etc/ipsec.d/ibops-*.conf\n' >> /etc/ipsec.conf
touch /etc/ipsec.secrets; chmod 600 /etc/ipsec.secrets
grep -q "^include /etc/ipsec.d/ibops-\*.secrets" /etc/ipsec.secrets || \
  printf '\n# InfraBeatOps client VPNs\ninclude /etc/ipsec.d/ibops-*.secrets\n' >> /etc/ipsec.secrets
install -d -m 755 /etc/ipsec.d

echo "== 4. VPN login (typed here, stored root-only)"
read -r -p "   VPN user [Infrabeat-basis2]: " VUSER; VUSER="${VUSER:-Infrabeat-basis2}"
[[ "$VUSER" =~ ^[A-Za-z0-9._@-]{1,64}$ ]] || { echo "   unusual characters in the user name"; exit 1; }
while true; do
  read -r -s -p "   VPN password: " P1; echo
  read -r -s -p "   again: " P2; echo
  [ -n "$P1" ] && [ "$P1" = "$P2" ] && break
  echo "   empty or different, try again"
done
while true; do
  read -r -s -p "   Pre-shared key: " K1; echo
  read -r -s -p "   again: " K2; echo
  [ -n "$K1" ] && [ "$K1" = "$K2" ] && break
  echo "   empty or different, try again"
done
umask 077
# base64 (0s...) so any character in the key or password is stored exactly.
PSK64="$(printf '%s' "$K1" | base64 -w0)"; PW64="$(printf '%s' "$P1" | base64 -w0)"
unset P1 P2 K1 K2
cat > "/etc/ipsec.d/$CONN.secrets" <<SEC
# InfraBeatOps -- $NAME. Root-only. Written by install_ipsec_vpn.sh $(date +%F)
: PSK 0s$PSK64
"$VUSER" : XAUTH 0s$PW64
SEC
chmod 600 "/etc/ipsec.d/$CONN.secrets"; chown root:root "/etc/ipsec.d/$CONN.secrets"
unset PSK64 PW64
umask 022

# Watchdog settings (no secrets): which strongSwan connection, probe targets.
install -d -m 755 /etc/ibops-vpn
{
  echo "# InfraBeatOps IPsec VPN $NAME -- no secrets in this file."
  echo "conn = $CONN"
  echo "gateway = $GW"
  echo "# Connectivity probes: 'up' means at least one answers (tested every minute)."
  for p in $PROBES; do echo "# ibops-probe = $p"; done
} > "/etc/ibops-vpn/$NAME.conf"
chmod 644 "/etc/ibops-vpn/$NAME.conf"

# Watchdog program + service + command (same as the SSL-VPN one).
install -d -m 755 /opt/ibops-vpn
install -m 755 "$HERE/ibops_vpn_watchdog.py" /opt/ibops-vpn/ibops_vpn_watchdog.py
install -m 644 "$HERE/ibops-vpn@.service" /etc/systemd/system/ibops-vpn@.service
cat > /usr/local/bin/ibops-vpn <<'WRAP'
#!/bin/sh
exec /usr/bin/python3 /opt/ibops-vpn/ibops_vpn_watchdog.py "$@"
WRAP
chmod 755 /usr/local/bin/ibops-vpn
systemctl daemon-reload
systemctl stop "ibops-vpn@$NAME" 2>/dev/null || true
systemctl enable --now strongswan-starter >/dev/null 2>&1 || systemctl enable --now strongswan >/dev/null 2>&1 || true
sleep 2

write_conn() {   # mode ike esp
  local localid="    # leftid: not set (FortiClient Local ID empty)"
  if [ -n "${IBOPS_IPSEC_LOCALID:-}" ]; then localid="    leftid=@${IBOPS_IPSEC_LOCALID}"; fi
  cat > "/etc/ipsec.d/$CONN.conf" <<CONF
# InfraBeatOps -- $NAME (FortiGate IPsec, PSK + XAuth). Written by install_ipsec_vpn.sh $(date +%F)
# Started and stopped ONLY by the watchdog (ibops-vpn@$NAME): auto=add, one keying try,
# dead peer -> clear (no automatic re-dial behind the watchdog's back).
conn $CONN
    keyexchange=ikev1
    aggressive=$1
    ike=$2!
    esp=$3!
    ikelifetime=24h
    keylife=8h
    keyingtries=1
    left=%defaultroute
    leftsourceip=%config
    leftauth=psk
    leftauth2=xauth
    xauth=client
    xauth_identity="$VUSER"
$localid
    right=$GW
    rightid=%any
    rightauth=psk
    rightsubnet=$REMOTE
    dpddelay=30s
    dpdtimeout=150s
    dpdaction=clear
    auto=add
CONF
  chmod 644 "/etc/ipsec.d/$CONN.conf"
  ipsec reload >/dev/null 2>&1 || ipsec restart >/dev/null 2>&1
  ipsec rereadsecrets >/dev/null 2>&1 || true
  sleep 1
}

g() { case "$1" in 2) echo modp1024;; 5) echo modp1536;; 14) echo modp2048;; 15) echo modp3072;; 19) echo ecp256;; 20) echo ecp384;; 21) echo ecp521;; *) echo "";; esac; }
ike_for() { local out="" grp; for grp in "$@"; do out="$out,aes256-sha256-$grp,aes128-sha256-$grp,aes256-sha1-$grp,aes128-sha1-$grp,3des-sha1-$grp"; done; echo "${out#,}"; }
esp_for() { if [ -n "$1" ]; then echo "aes256-sha256-$1,aes128-sha256-$1,aes256-sha1-$1,aes128-sha1-$1,3des-sha1-$1"; else echo "aes256-sha256,aes128-sha256,aes256-sha1,aes128-sha1,3des-sha1"; fi; }

# Attempts: mode | ike | esp. FortiClient's own defaults first.
ATTEMPTS=()
MODE="${IBOPS_IPSEC_MODE:-auto}"; DH="${IBOPS_IPSEC_DH:-auto}"; PFS="${IBOPS_IPSEC_PFS:-auto}"
if [ "$DH" != "auto" ]; then GRPS="$(g "$DH")"; [ -n "$GRPS" ] || { echo "unknown DH group $DH"; exit 1; }; else GRPS="modp1536 modp2048 modp1024"; fi
for grp in $GRPS; do
  for pfs in yes no; do
    if [ "$PFS" != "auto" ] && [ "$PFS" != "$pfs" ]; then continue; fi
    if [ "$pfs" = yes ]; then e="$(esp_for "$grp")"; else e="$(esp_for "")"; fi
    if [ "$MODE" = "auto" ] || [ "$MODE" = "main" ]; then ATTEMPTS+=("no|$(ike_for $grp)|$e|main, DH $grp, PFS $pfs"); fi
    if [ "$MODE" = "auto" ] || [ "$MODE" = "aggressive" ]; then ATTEMPTS+=("yes|$(ike_for $grp)|$e|aggressive, DH $grp, PFS $pfs"); fi
  done
done

echo "== 5. finding the gateway's settings (${#ATTEMPTS[@]} combinations at most, ~1 min each)"
echo "   A combination the gateway does not accept is refused BEFORE the password is sent."
echo "   A wrong password or key stops here at once (no repeated tries -- no account lockout)."
FOUND=""
i=0
for a in "${ATTEMPTS[@]}"; do
  i=$((i+1))
  IFS='|' read -r aggr ike esp label <<<"$a"
  echo "   [$i/${#ATTEMPTS[@]}] $label ..."
  write_conn "$aggr" "$ike" "$esp"
  set +e
  OUT="$(/usr/local/bin/ibops-vpn try "$NAME" 2>&1)"; set -e
  RES="$(echo "$OUT" | sed -n 's/^RESULT=//p' | tail -1)"
  case "$RES" in
    up)       echo "      CONNECTED -- using: $label"; FOUND="$label"; break;;
    auth)     echo "$OUT" | tail -8 | sed 's/^/      /'
              echo "   STOP: the gateway rejected the login (user, password or pre-shared key)."
              echo "   Check them in FortiClient, then run this script again. Not retrying (account lockout)."
              exit 5;;
    in_use)   echo "   STOP: the VPN account is in use by another session. Not pushing it off."
              echo "   Run this script again when nobody else is connected with $VUSER."
              exit 6;;
    *)        echo "      not accepted ($RES): $(echo "$OUT" | grep -iE 'NO_PROPOSAL|fail|giving up|INVALID' | tail -1 | cut -c1-140)";;
  esac
done
if [ -z "$FOUND" ]; then
  echo
  echo "   STOP: none of the common FortiClient settings were accepted."
  echo "   Send a screenshot of FortiClient > $NAME connection > Edit > Advanced Settings"
  echo "   (VPN Settings, Phase 1, Phase 2) and run again with e.g.:"
  echo "   sudo IBOPS_IPSEC_MODE=aggressive IBOPS_IPSEC_DH=14 IBOPS_IPSEC_PFS=yes bash $0 $NAME $GW"
  echo "   Last output:"; echo "$OUT" | tail -15 | sed 's/^/      /'
  exit 7
fi

echo "== 6. service"
systemctl enable --now "ibops-vpn@$NAME"
echo "   waiting for the tunnel..."
for i in $(seq 1 60); do
  st="$(python3 -c "import json;print(json.load(open('/run/ibops-vpn/$NAME.json')).get('state',''))" 2>/dev/null || true)"
  [ "$st" = "up" ] && break
  sleep 2
done
ibops-vpn status "$NAME" || true
ip route show table 220 2>/dev/null | sed 's/^/   route: /' || true
echo
echo "Done. Settings that worked: $FOUND"
echo "Check:  journalctl -u ibops-vpn@$NAME -n 30     ibops-vpn status $NAME"
