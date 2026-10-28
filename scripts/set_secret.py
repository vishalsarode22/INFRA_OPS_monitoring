"""
Store a secret in the OS credential store instead of .env.

    python scripts\set_secret.py QAS_RFC_PASSWORD          # prompts, hidden input
    python scripts\set_secret.py QAS_RFC_PASSWORD --delete
    python scripts\set_secret.py --list-hint

On Windows this is Credential Manager (Control Panel -> Credential Manager
-> Windows Credentials, entries named "infrabeatops"). The value is encrypted
for the account that stored it, so run this AS the service account that
runs the dashboard/sweep. config/systems.yaml keeps using ${QAS_RFC_PASSWORD};
core/config_loader.py checks .env first, then the credential store.

Requires: pip install keyring
"""

import argparse
import getpass
import sys

SERVICE = "infrabeatops"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?", help="variable name, e.g. QAS_RFC_PASSWORD")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--list-hint", action="store_true",
                    help="show where to see stored entries")
    args = ap.parse_args()

    if args.list_hint:
        print("Windows: Control Panel > Credential Manager > Windows Credentials, "
              f"look for entries named '{SERVICE}'.")
        return 0
    if not args.name:
        ap.error("name is required")

    try:
        import keyring
    except ImportError:
        print("keyring is not installed:  pip install keyring", file=sys.stderr)
        return 1

    if args.delete:
        keyring.delete_password(SERVICE, args.name)
        print(f"Deleted {args.name} from the credential store.")
        return 0

    value = getpass.getpass(f"Value for {args.name} (hidden): ")
    if not value:
        print("Nothing stored (empty value).")
        return 1
    keyring.set_password(SERVICE, args.name, value)
    print(f"Stored {args.name}. Remove any {args.name}= line from .env so this one is used.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
