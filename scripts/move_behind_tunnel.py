"""
Windows VM, once: reach SAP systems through the Ubuntu VM's VPN instead of
FortiClient on this VM.

    .venv\\Scripts\\python.exe scripts\\move_behind_tunnel.py            (AG: PS4, CAR PRD, CARFOUR x3)
    .venv\\Scripts\\python.exe scripts\\move_behind_tunnel.py --dry-run  (show, change nothing)
    .venv\\Scripts\\python.exe scripts\\move_behind_tunnel.py --undo     (put everything back)

    --vm ag   the VPN runs on ANOTHER machine with its own tunnel task
              ("IBOPS Tunnel AG", packaging\\windows\\install_second_tunnel.ps1):
              forwards go to config\\tunnel_forwards_ag.txt, and the network is
              checked with a probe through that tunnel (no remote source needed).

Path afterwards (the same way CEAT is reached):
    IBOPS on Windows -> 127.0.0.x -> SSH tunnel (IBOPS Tunnel task) -> Ubuntu
    -> openfortivpn (ibops-vpn@ag, reconnects by itself) -> SAP

For each system it
  1. changes rfc.ashost in config\\systems.yaml from the real address to a
     free local address 127.0.0.N (systems on the same server share one),
  2. sets network: {name: AG, vpn: true, check: remote:ubuntu/ag}, so a VPN
     drop is reported from the Ubuntu side,
  3. adds the SAP GUI (32NN) and RFC (33NN) forwards to
     config\\tunnel_forwards.txt,
  4. switches off config\\vpn_keeper.yaml here (renamed .off): the VPN is on
     Ubuntu now and the same VPN account must not be used twice.
Backups: <file>.bak-pause-<time>; what was changed is kept in
config\\tunnel_moves.json, which --undo uses. Nothing is written unless the
result is valid YAML with the same systems.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pause_system as P  # noqa: E402

AG = ["PS4", "CAR PRD", "CARFOUR PRD", "CARFOUR PRDDUCTION", "CARFOUR QAS"]
NETWORK = {"name": "AG", "remote": "ubuntu/ag"}
MOVES = os.path.join(P.CONFIG, "tunnel_moves.json")
KEEPER = os.path.join(P.CONFIG, "vpn_keeper.yaml")


def _used_loopbacks(*texts: str) -> set[str]:
    return set(re.findall(r"\b127\.0\.0\.\d{1,3}\b", "\n".join(texts)))


def _rfc_fields(block: list[str]) -> dict:
    """{'ashost': (index, value), 'sysnr': ..., 'mshost': ...} inside the rfc: sub-block."""
    out, in_rfc, rfc_indent = {}, False, 0
    for i, ln in enumerate(block):
        s = ln.rstrip("\r\n")
        ind = len(s) - len(s.lstrip(" "))
        m = re.match(r"^\s*rfc:\s*$", s)
        if m:
            in_rfc, rfc_indent = True, ind
            continue
        if in_rfc and s.strip() and ind <= rfc_indent:
            in_rfc = False
        if in_rfc:
            m = re.match(r"^\s*(ashost|sysnr|mshost|gwhost)\s*:\s*['\"]?([^'\"#\s]+)", s)
            if m:
                out[m.group(1)] = (i, m.group(2))
    return out


def _add_gateway(block: list[str], i: int, local: str, nr: str, nl: str) -> list[str]:
    """
    Keep ashost (the REAL server) and send RFC through the tunnel as a gateway:
        ashost: ${PS4_RFC_HOST}       <- unchanged
        gwhost: 127.0.0.13            <- the tunnel
        gwserv: '3300'
    28.10.2026: with ashost: 127.0.0.13 the AG gateways (sapxps4ci) answered
    "reject connection request ... to 127.0.0.13:sapdp00" -- the client asks
    the gateway to reach ashost, and 127.0.0.13 is not the SAP server itself.
    """
    ind = block[i][:len(block[i]) - len(block[i].lstrip(" "))]
    out = [ln for ln in block if not re.match(rf"^{ind}(gwhost|gwserv)\s*:", ln)]
    j = out.index(block[i])
    return out[:j + 1] + [f"{ind}gwhost: {local}{nl}", f"{ind}gwserv: '33{nr}'{nl}"] + out[j + 1:]


def gateway_fix(vm: str, dry: bool) -> int:
    """
    Systems moved earlier with ashost: 127.x into config/tunnel_forwards_<vm>.txt:
    put the real ashost back (from tunnel_moves.json) and use gwhost/gwserv.
    """
    if not os.path.isfile(MOVES):
        print("No moved systems (config\\tunnel_moves.json not found).")
        return 0
    moves = json.load(open(MOVES, encoding="utf-8"))
    mine = {n: m for n, m in moves.items() if m.get("forwards") == f"tunnel_forwards_{vm}.txt"}
    live = P._read(P.LIVE)
    nl = "\r\n" if "\r\n" in live else "\n"
    head, blks, tail = P.blocks(live)
    have = [n for n, _ in blks]
    out, done = [], []
    for name, block in blks:
        m = mine.get(name)
        f = _rfc_fields(block) if m else {}
        if not m or "ashost" not in f or f["ashost"][1] != m["local"]:
            out.append(block)
            continue
        before = _rfc_fields(m["before"].splitlines(keepends=True))
        if "ashost" not in before:
            print(f"   {name}: original ashost not recorded. Skipped.")
            out.append(block)
            continue
        b = list(block)
        i = f["ashost"][0]
        b[i] = re.sub(r"(ashost\s*:\s*)['\"]?[^'\"#\s]+['\"]?", lambda mm: mm.group(1) + before["ashost"][1], b[i], count=1)
        b = _add_gateway(b, i, m["local"], m["sysnr"], nl)
        out.append(b)
        done.append(name)
        print(f"   {name}: ashost {before['ashost'][1]}, gateway {m['local']}:33{m['sysnr']}")
    if not done:
        print("Nothing to change.")
        return 0
    new_live = "".join(head) + "".join("".join(b) for b in out) + "".join(tail)
    P._check(new_live, have, P.LIVE)
    import yaml
    data = {s["name"]: s for s in yaml.safe_load(new_live)["systems"]}
    for n in done:
        rfc = data[n].get("rfc") or {}
        if str(rfc.get("gwhost")) != mine[n]["local"] or str(rfc.get("ashost")).startswith("127."):
            raise SystemExit(f"STOP: {n} would not read back as expected. Nothing was changed.")
    if dry:
        print("\nDRY RUN -- nothing changed.")
        return 0
    P._write(P.LIVE, new_live, time.strftime("%Y%m%d-%H%M%S"))
    print("\nChanged. The dashboard picks it up by itself; SAP Logon entries stay on the 127.0.0.x addresses.")
    return 0


def router_fix(vm: str, dry: bool) -> int:
    """
    RFC for the systems moved into config/tunnel_forwards_<vm>.txt goes through
    a SAProuter on that VM (28.10.2026: the AG gateways reject any request
    addressed to 127.0.0.x, with or without gwhost). Per system:
        ashost: ${PS4_RFC_HOST}                 the real server (unchanged/restored)
        saprouter: /H/127.0.0.20/S/3299         the router's local end
    gwhost/gwserv are removed. One forward is added to the VM's file:
        127.0.0.20:3299:127.0.0.1:3299           SAProuter on that VM
    SAP GUI keeps using the 127.0.0.x entries in SAP Logon (dispatcher, no gateway).
    """
    if not os.path.isfile(MOVES):
        print("No moved systems (config\\tunnel_moves.json not found).")
        return 0
    moves = json.load(open(MOVES, encoding="utf-8"))
    mine = {n: m for n, m in moves.items() if m.get("forwards") == f"tunnel_forwards_{vm}.txt"}
    fwd_file = _fwd_path(vm)
    fw = P._read(fwd_file)
    live = P._read(P.LIVE)
    nl = "\r\n" if "\r\n" in live else "\n"
    fnl = "\r\n" if "\r\n" in fw else nl
    existing = re.search(r"^(127\.\d+\.\d+\.\d+):(\d+):127\.0\.0\.1:3299\b", fw, re.M)
    if existing:
        rhost, rport = existing.group(1), existing.group(2)
    else:
        used = _used_loopbacks(live, fw, P._read(P.FORWARDS))
        n = 20
        while f"127.0.0.{n}" in used:
            n += 1
        rhost, rport = f"127.0.0.{n}", "3299"
    route = f"/H/{rhost}/S/{rport}"
    head, blks, tail = P.blocks(live)
    have = [n for n, _ in blks]
    out, done, targets = [], [], []
    for name, block in blks:
        m = mine.get(name)
        if not m:
            out.append(block)
            continue
        f = _rfc_fields(block)
        before = _rfc_fields(m["before"].splitlines(keepends=True))
        if "ashost" not in f or "ashost" not in before:
            print(f"   {name}: no ashost found. Skipped.")
            out.append(block)
            continue
        i = f["ashost"][0]
        ind = block[i][:len(block[i]) - len(block[i].lstrip(" "))]
        b = list(block)
        b[i] = re.sub(r"(ashost\s*:\s*)['\"]?[^'\"#\s]+['\"]?",
                      lambda mm: mm.group(1) + before["ashost"][1], b[i], count=1)
        keep_line = b[i]
        b = [ln for ln in b if not re.match(rf"^{ind}(gwhost|gwserv|saprouter)\s*:", ln)]
        j = b.index(keep_line)
        b = b[:j + 1] + [f"{ind}saprouter: {route}{nl}"] + b[j + 1:]
        out.append(b)
        done.append(name)
        targets += [f"{m['real']}:33{m['sysnr']}"]
        print(f"   {name}: ashost {before['ashost'][1]}, RFC through SAProuter {rhost}:{rport}")
    if not done:
        print("Nothing to change.")
        return 0
    new_live = "".join(head) + "".join("".join(b) for b in out) + "".join(tail)
    P._check(new_live, have, P.LIVE)
    import yaml
    data = {s["name"]: s for s in yaml.safe_load(new_live)["systems"]}
    for n in done:
        rfc = data[n].get("rfc") or {}
        if str(rfc.get("saprouter")) != route or rfc.get("gwhost") or str(rfc.get("ashost")).startswith("127."):
            raise SystemExit(f"STOP: {n} would not read back as expected. Nothing was changed.")
    fwd_line = f"{rhost}:{rport}:127.0.0.1:3299     # SAProuter on the {vm} VM (RFC)"
    targets = sorted(set(targets))
    if dry:
        print(f"\nDRY RUN -- nothing changed. Forward that would be added: {'(present) ' if existing else ''}{fwd_line}")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    P._write(P.LIVE, new_live, stamp)
    if not existing:
        text = fw if (not fw or fw.endswith(("\n", "\r\n"))) else fw + fnl
        P._write(fwd_file, text + fwd_line + fnl, stamp)
    all_targets = []
    for ln in P._read(fwd_file).splitlines():
        mm = re.match(r"^\d+\.\d+\.\d+\.\d+:\d+:([^:\s]+):(\d+)", ln.split("#", 1)[0].strip())
        if mm and f"{mm.group(1)}:{mm.group(2)}" not in all_targets:
            all_targets.append(f"{mm.group(1)}:{mm.group(2)}")
    print(f"\nChanged (backups *.bak-pause-{stamp}). Left to do on the {vm} VM:")
    print("  a) SAProuter, allowed to reach only these gateways:")
    print("       sudo bash ~/app/packaging/ubuntu/install_saprouter.sh ~/saprouter " + " ".join(targets))
    print("  b) let the tunnel key reach the router too (the list replaces the old one):")
    print("       bash ~/app/packaging/ubuntu/allow_tunnel_targets.sh " + " ".join(all_targets))
    print(f"  then restart the IBOPS Tunnel {vm.upper()} task here.")
    return 0


def _env_values() -> dict:
    """KEY=VALUE pairs of the project's .env (read here only to look up ${VAR} addresses)."""
    out = {}
    try:
        with open(os.path.join(P.ROOT, ".env"), encoding="utf-8-sig") as fh:
            for ln in fh:
                m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", ln)
                if m:
                    out[m.group(1)] = m.group(2).strip().strip("'\"")
    except OSError:
        pass
    return out


def _resolve(value: str, env: dict) -> str | None:
    """'${PS4_RFC_HOST}' -> its address from .env / the environment; None when it cannot be found.
    28.10.2026: the placeholder itself was written as the tunnel target and PS4 could not log on."""
    m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
    if not m:
        return value.strip()
    v = os.environ.get(m.group(1)) or env.get(m.group(1)) or ""
    return v.strip() or None


def _set_network(block: list[str], nl: str, probe: str = "") -> list[str]:
    """Replace or add the network: sub-block right under '- name:'."""
    name_i = next(i for i, ln in enumerate(block) if P._name_of(ln) is not None)
    key_indent = " " * (len(block[name_i]) - len(block[name_i].lstrip(" ")) + 2)
    new = [f"{key_indent}network:{nl}",
           f"{key_indent}  name: {NETWORK['name']}{nl}",
           f"{key_indent}  vpn: true{nl}",
           (f"{key_indent}  probe: {probe}{nl}" if probe else
            f"{key_indent}  check: remote:{NETWORK['remote']}{nl}")]
    out, i = [], 0
    while i < len(block):
        ln = block[i]
        if re.match(rf"^{key_indent}network:", ln):
            i += 1
            while i < len(block) and (not block[i].strip() or
                                      len(block[i]) - len(block[i].lstrip(" ")) > len(key_indent)):
                i += 1
            continue
        out.append(ln)
        i += 1
    at = next(i for i, ln in enumerate(out) if P._name_of(ln) is not None) + 1
    return out[:at] + new + out[at:]


def _fwd_path(vm: str) -> str:
    return os.path.join(P.CONFIG, f"tunnel_forwards_{vm}.txt") if vm else P.FORWARDS


def move(names: list[str], dry: bool, vm: str = "", explicit: bool = False) -> int:
    """explicit: the systems were named on the command line -- a leftover
    'probe: 127.x' (PS4's 127.0.10.2, 28.10.2026) does not stop the move then."""
    live = P._read(P.LIVE)
    if not live:
        print("config\\systems.yaml not found -- run this from the IBOPS folder.")
        return 1
    fwd_file = _fwd_path(vm)
    fw = P._read(fwd_file)
    main_fw = P._read(P.FORWARDS) if vm else ""
    nl = "\r\n" if "\r\n" in live else "\n"
    fnl = "\r\n" if "\r\n" in fw else nl
    head, blks, tail = P.blocks(live)
    have = [n for n, _ in blks]
    missing = [n for n in names if n not in have]
    if missing:
        print(f"Not in systems.yaml (skipped): {', '.join(missing)}")
    used = _used_loopbacks(live, fw, main_fw) | {"127.0.0.1"}
    env = _env_values()
    by_real: dict[str, str] = {}
    moves, new_blocks, fwd_lines, targets = {}, [], [], []
    for name, block in blks:
        if name not in names:
            new_blocks.append(block)
            continue
        f = _rfc_fields(block)
        if "mshost" in f and "ashost" not in f:
            print(f"   {name}: logs on through a message server ({f['mshost'][1]}) -- a tunnel needs one "
                  f"application server. Put its ashost + sysnr on the Systems page first. Skipped.")
            new_blocks.append(block)
            continue
        if "ashost" not in f or "sysnr" not in f:
            print(f"   {name}: no rfc ashost/sysnr found. Skipped.")
            new_blocks.append(block)
            continue
        raw, nr = f["ashost"][1], f["sysnr"][1].zfill(2)
        real = _resolve(raw, env)
        if not real:
            print(f"   {name}: its address {raw} is not set in .env. Skipped.")
            new_blocks.append(block)
            continue
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", real):
            print(f"   {name}: address {real} is not an IP address -- put the server's IP in .env. Skipped.")
            new_blocks.append(block)
            continue
        if "network:" in "".join(block) and re.search(r"probe:\s*127\.", "".join(block)) and not explicit:
            print(f"   {name}: already reached through a local tunnel (network probe 127.x). Skipped.")
            new_blocks.append(block)
            continue
        if "gwhost" in f and f["gwhost"][1].startswith("127."):
            print(f"   {name}: already behind a tunnel (gateway {f['gwhost'][1]}). Skipped.")
            new_blocks.append(block)
            continue
        if real.startswith("127."):
            print(f"   {name}: already behind a tunnel ({real}). Skipped.")
            new_blocks.append(block)
            continue
        if real not in by_real:
            n = 10
            while f"127.0.0.{n}" in used:
                n += 1
            by_real[real] = f"127.0.0.{n}"
            used.add(by_real[real])
        local = by_real[real]
        b = list(block)
        i = f["ashost"][0]
        if vm:
            b = _add_gateway(b, i, local, nr, nl)
        else:
            b[i] = re.sub(r"(ashost\s*:\s*)['\"]?[^'\"#\s]+['\"]?", rf"\g<1>{local}", b[i], count=1)
        b = _set_network(b, nl, probe=f"{local}:32{nr}" if vm else "")
        moves[name] = {"before": "".join(block), "local": local, "real": real, "sysnr": nr,
                       "forwards": os.path.basename(fwd_file)}
        new_blocks.append(b)
        for port, what in ((f"32{nr}", "SAP GUI"), (f"33{nr}", "RFC")):
            line = f"{local}:{port}:{real}:{port}     # {name}  {what}"
            if f"{local}:{port}:" not in fw and line.split()[0] not in [x.split()[0] for x in fwd_lines]:
                fwd_lines.append(line)
            targets.append(f"{real}:{port}")
        print(f"   {name}: {real} instance {nr}  ->  {local}")
    if not moves:
        print("Nothing to change.")
        return 0
    new_live = "".join(head) + "".join("".join(b) for b in new_blocks) + "".join(tail)
    P._check(new_live, have, P.LIVE)
    import yaml
    data = {s["name"]: s for s in yaml.safe_load(new_live)["systems"]}
    for n, m in moves.items():
        rfc = data[n].get("rfc") or {}
        got = str(rfc.get("gwhost")) if vm else str(rfc.get("ashost"))
        if vm and str(rfc.get("gwserv")) != f"33{m['sysnr']}":
            got = "?"
        net = data[n].get("network") or {}
        ok_net = (str(net.get("probe")) == f"{m['local']}:32{m['sysnr']}") if vm else \
            (str(net.get("check")) == f"remote:{NETWORK['remote']}")
        if got != m["local"] or not ok_net:
            raise SystemExit(f"STOP: {n} would not read back as expected. Nothing was changed.")
    if dry:
        print("\nDRY RUN -- nothing changed. Forwards that would be added:")
        for ln in fwd_lines:
            print("   " + ln)
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    P._write(P.LIVE, new_live, stamp.replace("pause", "tunnel"))
    if fwd_lines:
        text = fw
        if text and not text.endswith(("\n", "\r\n")):
            text += fnl
        text += f"# AG (CAR, CARFOUR) through openfortivpn ibops-vpn@ag{fnl}"
        P._write(fwd_file, text + fnl.join(fwd_lines) + fnl, stamp)
    old = {}
    if os.path.isfile(MOVES):
        old = json.load(open(MOVES, encoding="utf-8"))
    old.update({n: {**m, "at": stamp} for n, m in moves.items()})
    json.dump(old, open(MOVES, "w", encoding="utf-8"), indent=1)
    if os.path.isfile(KEEPER):
        os.replace(KEEPER, KEEPER + ".off")
        print("   config\\vpn_keeper.yaml switched off (renamed .off): the VPN is on Ubuntu now")

    all_targets = []
    for ln in P._read(fwd_file).splitlines():
        body = ln.split("#", 1)[0].strip()
        m = re.match(r"^\d+\.\d+\.\d+\.\d+:\d+:([^:\s]+):(\d+)$", body)
        if m and f"{m.group(1)}:{m.group(2)}" not in all_targets:
            all_targets.append(f"{m.group(1)}:{m.group(2)}")
    print(f"\nChanged (backups *.bak-pause-{stamp}). Left to do -- see the instructions:")
    where = f"the {vm} VM" if vm else "Ubuntu"
    print(f"  a) {where}: the AG VPN service (install_vpn_watchdog.sh ag 203.0.113.74 10443)")
    print(f"  b) {where}: let the tunnel key reach ALL these targets (the list replaces the old one):")
    print("       bash ~/app/packaging/ubuntu/allow_tunnel_targets.sh " + " ".join(all_targets))
    print("  c) SAP Logon on this VM: change these entries to the local address, same instance:")
    for n, m in moves.items():
        print(f"       {n:<20} {m['real']:<16} -> {m['local']}  (instance {m['sysnr']})")
    print(f"  d) restart the {'IBOPS Tunnel ' + vm.upper() if vm else 'IBOPS Tunnel'} task;  "
          f"e) disconnect FortiClient AG on this VM")
    return 0


def undo() -> int:
    if not os.path.isfile(MOVES):
        print("Nothing to undo (config\\tunnel_moves.json not found).")
        return 0
    moves = json.load(open(MOVES, encoding="utf-8"))
    live = P._read(P.LIVE)
    head, blks, tail = P.blocks(live)
    have = [n for n, _ in blks]
    out = []
    for name, block in blks:
        m = moves.get(name)
        out.append([m["before"]] if m else block)
    new_live = "".join(head) + "".join("".join(b) for b in out) + "".join(tail)
    P._check(new_live, have, P.LIVE)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    P._write(P.LIVE, new_live, stamp)
    locals_ = {m["local"] for m in moves.values()}
    for path in {P.FORWARDS} | {os.path.join(P.CONFIG, m["forwards"]) for m in moves.values() if m.get("forwards")}:
        fw = P._read(path)
        if not fw:
            continue
        keep = [ln for ln in fw.splitlines(keepends=True)
                if not any(ln.startswith(lo + ":") for lo in locals_) and "ibops-vpn@ag" not in ln]
        P._write(path, "".join(keep), stamp)
    if os.path.isfile(KEEPER + ".off") and not os.path.isfile(KEEPER):
        os.replace(KEEPER + ".off", KEEPER)
    os.replace(MOVES, MOVES + f".undone-{stamp}")
    print(f"Put back: {', '.join(moves)}. Restart the IBOPS Tunnel task and point SAP Logon back at the "
          f"real addresses.")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if "--undo" in argv:
        return undo()
    vm = ""
    if "--vm" in argv:
        i = argv.index("--vm")
        vm = argv[i + 1] if i + 1 < len(argv) else ""
        if not re.fullmatch(r"[a-z0-9]{1,12}", vm):
            print("--vm needs a short lower-case name, e.g. --vm ag")
            return 1
        argv = argv[:i] + argv[i + 2:]
    if "--router-fix" in argv:
        return router_fix(vm or "ag", dry="--dry-run" in argv)
    if "--gateway-fix" in argv:
        return gateway_fix(vm or "ag", dry="--dry-run" in argv)
    named = [a for a in argv if not a.startswith("--")]
    return move(named or AG, dry="--dry-run" in argv, vm=vm, explicit=bool(named))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
