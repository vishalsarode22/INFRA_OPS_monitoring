"""
Manage dashboard users.

    python scripts/ibops_user.py add admin --role admin
    python scripts/ibops_user.py add metro-noc --role viewer --customers Metro
    python scripts/ibops_user.py add shift1 --role operator --customers "Metro,Sarloha"
    python scripts/ibops_user.py passwd shift1
    python scripts/ibops_user.py disable metro-noc
    python scripts/ibops_user.py list

Roles: admin (everything), operator (read in scope + run/snooze/acknowledge),
viewer (read-only in scope). Customers: '*' or a comma list matching the
`customer:` value of systems in config/systems.yaml.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))

from core import auth  # noqa: E402


def _ask_password() -> str:
    while True:
        p1 = getpass.getpass("Password: ")
        p2 = getpass.getpass("Again: ")
        if p1 and p1 == p2:
            return p1
        print("Passwords differ or empty, try again.")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("username"); a.add_argument("--role", default="viewer",
                                                                            choices=auth.ROLES)
    a.add_argument("--customers", default="*"); a.add_argument("--password", default=None)
    p = sub.add_parser("passwd"); p.add_argument("username")
    d = sub.add_parser("disable"); d.add_argument("username")
    sub.add_parser("list")
    args = ap.parse_args()

    if args.cmd == "add":
        auth.add_user(args.username, args.password or _ask_password(), args.role, args.customers)
        print(f"user '{args.username}' ({args.role}, customers={args.customers}) saved")
    elif args.cmd == "passwd":
        auth.set_password(args.username, _ask_password()); print("password changed")
    elif args.cmd == "disable":
        auth.disable_user(args.username); print("disabled and sessions revoked")
    else:
        for u in auth.list_users():
            print(f"{u['username']:<20} {u['role']:<9} {u['customers']:<30} "
                  f"{'DISABLED' if u['disabled'] else ''}")


if __name__ == "__main__":
    main()
