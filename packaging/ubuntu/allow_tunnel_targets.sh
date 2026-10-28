#!/usr/bin/env bash
# Let the Windows tunnel key forward to these SAP targets as well -- and to
# nothing else. Replaces the permitopen list on the key's line.
#   bash packaging/ubuntu/allow_tunnel_targets.sh 192.0.2.127:3200 192.0.2.127:3300 \
#        192.0.2.123:3200 192.0.2.123:3300
# The dashboard target 127.0.0.1:8000 is always kept.
set -euo pipefail
[ "$#" -ge 1 ] || { echo "usage: $0 host:port [host:port ...]"; exit 1; }
for t in "$@"; do
  [[ "$t" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}:[0-9]{1,5}$ ]] || { echo "not host:port: $t"; exit 1; }
done
AK="$HOME/.ssh/authorized_keys"
[ -f "$AK" ] || { echo "no $AK -- run add_tunnel_key.sh first"; exit 1; }
cp "$AK" "$AK.bak.$(date +%Y%m%d%H%M%S)"
python3 - "$AK" "$@" <<'PY'
import re, sys
path, targets = sys.argv[1], sys.argv[2:]
lines = open(path, encoding="utf-8").read().splitlines()
opts = ",".join(["restrict", "port-forwarding", 'permitopen="127.0.0.1:8000"'] +
                [f'permitopen="{t}"' for t in targets])
done = 0
for i, line in enumerate(lines):
    if line.rstrip().endswith("ibops-tunnel") and "restrict" in line:
        key = re.search(r"(ssh-ed25519|ssh-rsa|ecdsa-\S+) \S+ ibops-tunnel\s*$", line)
        if key:
            lines[i] = f"{opts} {key.group(0).strip()}"
            done += 1
if done != 1:
    sys.exit(f"expected exactly one tunnel key line (comment 'ibops-tunnel'), found {done}; nothing changed")
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("tunnel key may now forward to: 127.0.0.1:8000 " + " ".join(targets))
PY
chmod 600 "$AK"
