#!/usr/bin/env bash
# Allow the Windows VM's tunnel key to log in -- for the tunnel ONLY.
#   bash packaging/ubuntu/add_tunnel_key.sh ~/ibops_tunnel.pub
# The key may forward to 127.0.0.1:8000 and nothing else: no shell, no
# commands, no other ports. A stolen copy of it cannot open a terminal here.
set -euo pipefail
PUB="${1:?usage: add_tunnel_key.sh <public key file>}"
KEY="$(tr -d '\r\n' < "$PUB")"
case "$KEY" in ssh-ed25519\ *|ssh-rsa\ *|ecdsa-*) ;; *) echo "not a public key: $PUB"; exit 1;; esac
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
if grep -qF "$KEY" ~/.ssh/authorized_keys; then echo "key already present"; exit 0; fi
echo "restrict,port-forwarding,permitopen=\"127.0.0.1:8000\" $KEY" >> ~/.ssh/authorized_keys
echo "tunnel key added (forwarding to 127.0.0.1:8000 only)"
