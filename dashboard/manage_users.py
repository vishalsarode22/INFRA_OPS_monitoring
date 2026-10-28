"""
Manage dashboard logins.

    python -m dashboard.manage_users add admin --role admin
    python -m dashboard.manage_users add shift1 --role operator --customers "Metro,Sarloha"
    python -m dashboard.manage_users add metro-noc --role viewer --customers Metro
    python -m dashboard.manage_users passwd shift1
    python -m dashboard.manage_users remove metro-noc
    python -m dashboard.manage_users list
    python -m dashboard.manage_users email admin staff.a@infrabeat.com

Run from the project folder (the one holding .env and dashboard/). Passwords
are always prompted with getpass -- never taken on the command line, where
they would land in shell history -- and stored as salted
PBKDF2-HMAC-SHA256 (200,000 iterations) in the dashboard state database.

Roles (projects = --customers)
    admin              everything, every project, users, system configuration
    manager            his projects: act + give access to them (team leader and below)
    team_lead          his projects: run/stop/snooze sweeps, alerts, incident actions
    senior_consultant  same as team_lead
    consultant         his projects, read-only
    viewer             his projects, read-only
    client             his own project, read-only
    operator           legacy name for team_lead

Day-to-day user management is on the dashboard's Team & access page; people
with an @infrabeat.com address can also sign up there and ask for access.

As soon as one user exists, every request must log in (loopback included).
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bootstrap() -> None:
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except Exception:
        pass


def _ask_password(auth, prompt=getpass.getpass) -> str:
    for _ in range(3):
        p1 = prompt("Password: ")
        p2 = prompt("Repeat password: ")
        if p1 != p2:
            print("Passwords differ, try again.")
            continue
        try:
            auth.check_password_policy(p1)
        except ValueError as exc:
            print(f"{exc}, try again.")
            continue
        return p1
    raise SystemExit("No password set.")


def main(argv=None, prompt=getpass.getpass) -> int:
    _bootstrap()
    from core import auth

    ap = argparse.ArgumentParser(prog="python -m dashboard.manage_users",
                                 description="Manage InfraBeatOps dashboard logins.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="create a user (or reset an existing one)")
    a.add_argument("username")
    a.add_argument("--role", default="viewer", choices=auth.ROLES)
    a.add_argument("--email", default="", help="e-mail address (sign-in name and password resets)")
    a.add_argument("--customers", default="*",
                   help="'*' (all) or a comma list of customer names from systems.yaml")
    p = sub.add_parser("passwd", help="change a user's password")
    p.add_argument("username")
    r = sub.add_parser("remove", help="delete a user and end their sessions")
    r.add_argument("username")
    sub.add_parser("list", help="list users (never shows hashes)")
    e = sub.add_parser("email", help="set the e-mail address of a user (sign-in name, password resets)")
    e.add_argument("username")
    e.add_argument("address")
    args = ap.parse_args(argv)

    if args.cmd == "add":
        name = auth.valid_username(args.username)
        auth.add_user(name, _ask_password(auth, prompt), args.role, args.customers)
        if args.email or "@" in name:
            from core import accounts, store
            accounts.ensure_schema()
            store.conn().execute("UPDATE users SET email=?, email_verified=1 WHERE username=?",
                                 (accounts.normalise_email(args.email or name), name))
        print(f"user '{name}' saved (role {args.role}, customers {args.customers}).")
    elif args.cmd == "passwd":
        name = auth.valid_username(args.username)
        if not any(u["username"] == name for u in auth.list_users()):
            print(f"no such user: {name}")
            return 1
        auth.set_password(name, _ask_password(auth, prompt))
        print("password changed.")
    elif args.cmd == "remove":
        name = auth.valid_username(args.username)
        print("removed, sessions ended." if auth.remove_user(name) else f"no such user: {name}")
    elif args.cmd == "email":
        from core import accounts, store
        accounts.ensure_schema()
        name = auth.valid_username(args.username)
        address = accounts.normalise_email(args.address)
        clash = store.conn().execute("SELECT username FROM users WHERE (email=? OR username=?) AND username<>?",
                                     (address, address, name)).fetchone()
        if clash:
            print(f"{address} already belongs to {clash['username']}.")
            return 1
        cur = store.conn().execute("UPDATE users SET email=?, email_verified=1 WHERE username=?", (address, name))
        print(f"{name} now signs in and resets its password with {address}." if cur.rowcount else f"no such user: {name}")
    else:
        users = auth.list_users()
        if not users:
            print("no users configured -- the dashboard serves loopback (127.0.0.1) only.")
        for u in users:
            print(f"{u['username']:<28} {u['role']:<9} {u['customers']:<30} "
                  f"{'DISABLED' if u['disabled'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
