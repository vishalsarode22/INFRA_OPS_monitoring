#!/usr/bin/env bash
# InfraBeatOps -- the Ubuntu VM never sleeps, and everything that carries the
# client VPNs and the dashboard runs as a system service (not in a desktop
# session), so locking the screen or logging out of the desktop changes
# nothing.
#
#   sudo bash ~/app/packaging/ubuntu/keep_awake.sh          # apply
#   sudo bash ~/app/packaging/ubuntu/keep_awake.sh --undo   # allow sleep again
#
# A sleeping VM runs nothing (no VPN, no tunnel end, no dashboard), so sleep,
# suspend and hibernate are switched off -- including the desktop's "suspend
# when idle". Nothing else is changed.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
TARGETS="sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target"

if [ "${1:-}" = "--undo" ]; then
  systemctl unmask $TARGETS
  rm -f /etc/systemd/logind.conf.d/ibops-no-sleep.conf
  echo "Sleep allowed again (takes effect for new sessions / after: systemctl restart systemd-logind)."
  exit 0
fi

echo "== 1. no sleep / suspend / hibernate (system wide)"
systemctl mask $TARGETS >/dev/null
echo "   masked: $TARGETS"

echo "== 2. no idle or lid action"
install -d -m 755 /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/ibops-no-sleep.conf <<'CONF'
# InfraBeatOps: this VM carries client VPNs and must never sleep.
[Login]
IdleAction=ignore
HandleSuspendKey=ignore
HandleHibernateKey=ignore
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
CONF
echo "   /etc/systemd/logind.conf.d/ibops-no-sleep.conf written (used from the next boot;"
echo "   not restarting systemd-logind now -- that would end desktop sessions)"

echo "== 3. desktop: no automatic suspend (GNOME), for the login screen and for ${SUDO_USER:-root}"
if command -v gsettings >/dev/null; then
  for u in gdm "${SUDO_USER:-}"; do
    [ -n "$u" ] && id "$u" >/dev/null 2>&1 || continue
    sudo -u "$u" dbus-run-session -- gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' 2>/dev/null || true
    sudo -u "$u" dbus-run-session -- gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing' 2>/dev/null || true
  done
  echo "   done (masking in step 1 already blocks it even where this could not be set)"
else
  echo "   no desktop settings tool -- nothing to do"
fi

echo "== 4. services that must survive lock / log-out / reboot (all system services)"
for s in ibops-dashboard strongswan-starter; do
  if systemctl list-unit-files "$s.service" >/dev/null 2>&1 && systemctl cat "$s.service" >/dev/null 2>&1; then
    systemctl enable "$s" >/dev/null 2>&1 || true
    printf "   %-28s %s, starts at boot: %s\n" "$s" "$(systemctl is-active "$s" || true)" "$(systemctl is-enabled "$s" || true)"
  fi
done
for f in /etc/ibops-vpn/*.conf /etc/openfortivpn/*.conf; do
  [ -e "$f" ] || continue
  n="$(basename "$f" .conf)"
  printf "   %-28s %s, starts at boot: %s\n" "ibops-vpn@$n" "$(systemctl is-active "ibops-vpn@$n" || true)" \
         "$(systemctl is-enabled "ibops-vpn@$n" 2>/dev/null || true)"
done
echo "   (a VPN shown 'disabled' is paused on purpose -- e.g. pune_ttml -- and is left as it is)"
echo
echo "Done. Check: systemctl status sleep.target   (should say: masked)"
