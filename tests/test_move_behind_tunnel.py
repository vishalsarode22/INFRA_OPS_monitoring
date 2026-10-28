import importlib
import os
import sys

import yaml

SYS = """systems:
# PS4 -- Metro
- name: PS4
  client: '100'
  rfc:
    ashost: 198.51.100.31
    sysnr: '00'
    client: '100'
  basis_emails: a@infrabeat.com
- name: CARFOUR PRD
  network:
    name: old
  rfc:
    ashost: 198.51.100.212
    sysnr: '01'
- name: CARFOUR PRDDUCTION
  rfc:
    ashost: 198.51.100.212
    sysnr: '01'
- name: CEAT S4P
  rfc:
    ashost: 127.0.0.9
    sysnr: '10'
- name: GRP
  rfc:
    mshost: 10.0.0.1
"""


def _setup(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import pause_system as P
    importlib.reload(P)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "systems.yaml").write_text(SYS, encoding="utf-8")
    (cfg / "tunnel_forwards.txt").write_text("127.0.0.9:3210:10.250.20.150:3210  # CEAT S4P  SAP GUI\n", encoding="utf-8")
    (cfg / "vpn_keeper.yaml").write_text("vpns: []\n", encoding="utf-8")
    for k, v in {"CONFIG": str(cfg), "LIVE": str(cfg / "systems.yaml"), "PAUSED": str(cfg / "systems_paused.yaml"),
                 "FORWARDS": str(cfg / "tunnel_forwards.txt")}.items():
        monkeypatch.setattr(P, k, v)
    import move_behind_tunnel as M
    importlib.reload(M)
    monkeypatch.setattr(M, "MOVES", str(cfg / "tunnel_moves.json"))
    monkeypatch.setattr(M, "KEEPER", str(cfg / "vpn_keeper.yaml"))
    return M, cfg


def test_move_and_undo(tmp_path, monkeypatch, capsys):
    M, cfg = _setup(tmp_path, monkeypatch)
    assert M.main(["PS4", "CARFOUR PRD", "CARFOUR PRDDUCTION", "GRP", "CEAT S4P"]) == 0
    data = {s["name"]: s for s in yaml.safe_load((cfg / "systems.yaml").read_text())["systems"]}
    assert data["PS4"]["rfc"]["ashost"] == "127.0.0.10" and data["PS4"]["basis_emails"] == "a@infrabeat.com"
    assert data["CARFOUR PRD"]["rfc"]["ashost"] == data["CARFOUR PRDDUCTION"]["rfc"]["ashost"] == "127.0.0.11"
    assert data["CARFOUR PRD"]["network"] == {"name": "AG", "vpn": True, "check": "remote:ubuntu/ag"}
    assert data["GRP"]["rfc"] == {"mshost": "10.0.0.1"} and data["CEAT S4P"]["rfc"]["ashost"] == "127.0.0.9"
    fw = (cfg / "tunnel_forwards.txt").read_text()
    assert "127.0.0.10:3300:198.51.100.31:3300" in fw and fw.count("127.0.0.11:3201:") == 1
    out = capsys.readouterr().out
    assert "10.250.20.150:3210 198.51.100.31:3200" in out and "message server" in out
    assert not (cfg / "vpn_keeper.yaml").exists()
    assert M.main(["--undo"]) == 0
    assert (cfg / "systems.yaml").read_text() == SYS
    assert "198.51.100" not in (cfg / "tunnel_forwards.txt").read_text() and (cfg / "vpn_keeper.yaml").exists()


def test_env_placeholders_are_resolved_and_tunnelled_systems_skipped(tmp_path, monkeypatch, capsys):
    """28.10.2026: '${PS4_RFC_HOST}' was written as the tunnel target; PS4 could not log on."""
    M, cfg = _setup(tmp_path, monkeypatch)
    import pause_system as P
    monkeypatch.setattr(P, "ROOT", str(tmp_path))
    (cfg / "systems.yaml").write_text("""systems:
- name: CAR PRD
  rfc:
    ashost: ${CAR_PRD_RFC_HOST}
    sysnr: '00'
- name: CARFOUR QAS
  rfc:
    ashost: ${CARFOUR_QAS_RFC_HOST}
    sysnr: '00'
- name: PS4
  rfc:
    ashost: ${PS4_RFC_HOST}
    sysnr: '00'
  network:
    name: Infrabeat VPN
    probe: 127.0.10.2:3200
    vpn: true
""", encoding="utf-8")
    (tmp_path / ".env").write_text("CAR_PRD_RFC_HOST=10.20.30.40\nPS4_RFC_HOST=10.9.9.9\n", encoding="utf-8")
    monkeypatch.delenv("CARFOUR_QAS_RFC_HOST", raising=False)
    assert M.main([]) == 0                         # the default AG list, PS4 not named
    out = capsys.readouterr().out
    fw = (cfg / "tunnel_forwards.txt").read_text()
    assert ":3200:10.20.30.40:3200" in fw and "${" not in fw
    assert "CARFOUR QAS: its address ${CARFOUR_QAS_RFC_HOST} is not set" in out
    assert "PS4: already reached through a local tunnel" in out
    data = {s["name"]: s for s in yaml.safe_load((cfg / "systems.yaml").read_text())["systems"]}
    assert data["PS4"]["rfc"]["ashost"] == "${PS4_RFC_HOST}"


def test_second_vm_uses_its_own_forwards_and_a_probe(tmp_path, monkeypatch, capsys):
    M, cfg = _setup(tmp_path, monkeypatch)
    import pause_system as P
    monkeypatch.setattr(P, "ROOT", str(tmp_path))
    (cfg / "systems.yaml").write_text("""systems:
- name: CAR PRD
  rfc:
    ashost: ${CAR_PRD_RFC_HOST}
    sysnr: '00'
- name: CEAT S4P
  rfc:
    ashost: 127.0.0.9
    sysnr: '10'
""", encoding="utf-8")
    (tmp_path / ".env").write_text("CAR_PRD_RFC_HOST=10.20.30.40\n", encoding="utf-8")
    main_before = (cfg / "tunnel_forwards.txt").read_text()
    assert M.main(["CAR PRD", "--vm", "ag"]) == 0
    assert (cfg / "tunnel_forwards.txt").read_text() == main_before          # CEAT tunnel untouched
    ag = (cfg / "tunnel_forwards_ag.txt").read_text()
    assert "127.0.0.10:3200:10.20.30.40:3200" in ag and "127.0.0.10:3300:10.20.30.40:3300" in ag
    data = {s["name"]: s for s in yaml.safe_load((cfg / "systems.yaml").read_text())["systems"]}
    assert data["CAR PRD"]["network"]["probe"] == "127.0.0.10:3200"
    assert data["CAR PRD"]["rfc"]["ashost"] == "${CAR_PRD_RFC_HOST}"            # real server kept
    assert data["CAR PRD"]["rfc"]["gwhost"] == "127.0.0.10" and data["CAR PRD"]["rfc"]["gwserv"] == "3300"
    assert M.main(["--undo"]) == 0
    assert "10.20.30.40" not in (cfg / "tunnel_forwards_ag.txt").read_text()
    data = {s["name"]: s for s in yaml.safe_load((cfg / "systems.yaml").read_text())["systems"]}
    assert data["CAR PRD"]["rfc"]["ashost"] == "${CAR_PRD_RFC_HOST}"


def test_a_named_system_with_a_leftover_probe_is_moved(tmp_path, monkeypatch, capsys):
    M, cfg = _setup(tmp_path, monkeypatch)
    import pause_system as P
    monkeypatch.setattr(P, "ROOT", str(tmp_path))
    (cfg / "systems.yaml").write_text("""systems:
- name: PS4
  rfc:
    ashost: ${PS4_RFC_HOST}
    sysnr: '00'
  network:
    name: Infrabeat VPN
    probe: 127.0.10.2:3200
    vpn: true
""", encoding="utf-8")
    (tmp_path / ".env").write_text("PS4_RFC_HOST=198.51.100.31\n", encoding="utf-8")
    (cfg / "tunnel_forwards_ag.txt").write_text("127.0.0.10:3200:198.51.100.213:3200  # CARFOUR QAS\n", encoding="utf-8")
    assert M.main(["PS4", "--vm", "ag"]) == 0
    ag = (cfg / "tunnel_forwards_ag.txt").read_text()
    assert "127.0.0.11:3200:198.51.100.31:3200" in ag and "127.0.0.10:3200:198.51.100.213" in ag
    data = {s["name"]: s for s in yaml.safe_load((cfg / "systems.yaml").read_text())["systems"]}
    assert data["PS4"]["network"] == {"name": "AG", "vpn": True, "probe": "127.0.0.11:3200"}
    assert data["PS4"]["rfc"]["gwhost"] == "127.0.0.11"


def test_gateway_fix_turns_an_ashost_move_into_a_gateway(tmp_path, monkeypatch, capsys):
    """28.10.2026: AG gateways rejected 'connection request ... to 127.0.0.13:sapdp00'."""
    import json
    M, cfg = _setup(tmp_path, monkeypatch)
    before = "- name: PS4\n  rfc:\n    ashost: ${PS4_RFC_HOST}\n    sysnr: '00'\n"
    (cfg / "systems.yaml").write_text("""systems:
- name: PS4
  rfc:
    ashost: 127.0.0.13
    sysnr: '00'
  network:
    name: AG
    vpn: true
    probe: 127.0.0.13:3200
""", encoding="utf-8")
    (cfg / "tunnel_moves.json").write_text(json.dumps({"PS4": {"before": before, "local": "127.0.0.13",
        "real": "198.51.100.31", "sysnr": "00", "forwards": "tunnel_forwards_ag.txt"}}), encoding="utf-8")
    assert M.main(["--gateway-fix", "--vm", "ag", "--dry-run"]) == 0
    assert "ashost: 127.0.0.13" in (cfg / "systems.yaml").read_text()
    assert M.main(["--gateway-fix", "--vm", "ag"]) == 0
    rfc = yaml.safe_load((cfg / "systems.yaml").read_text())["systems"][0]["rfc"]
    assert rfc == {"ashost": "${PS4_RFC_HOST}", "gwhost": "127.0.0.13", "gwserv": "3300", "sysnr": "00"}
    assert M.main(["--gateway-fix", "--vm", "ag"]) == 0                     # second time: nothing
    assert M.main(["--undo"]) == 0
    assert yaml.safe_load((cfg / "systems.yaml").read_text())["systems"][0]["rfc"]["ashost"] == "${PS4_RFC_HOST}"


def test_reachability_tests_the_tunnel_gateway_not_the_real_server():
    from core import reachability as R
    cfg = {"rfc": {"ashost": "198.51.100.31", "sysnr": "00", "gwhost": "127.0.0.13", "gwserv": "3300"}}
    assert R._sap_ports(cfg) == ("127.0.0.13", 3300, 3200)
    assert R._sap_ports({"rfc": {"ashost": "10.0.0.5", "sysnr": "10"}}) == ("10.0.0.5", 3310, 3210)


def test_router_fix_sends_rfc_through_a_saprouter(tmp_path, monkeypatch, capsys):
    """28.10.2026: AG gateways reject requests to 127.0.0.x even with gwhost."""
    import json
    M, cfg = _setup(tmp_path, monkeypatch)
    before = "- name: PS4\n  rfc:\n    ashost: ${PS4_RFC_HOST}\n    sysnr: '00'\n"
    (cfg / "systems.yaml").write_text("""systems:
- name: PS4
  rfc:
    ashost: ${PS4_RFC_HOST}
    gwhost: 127.0.0.13
    gwserv: '3300'
    sysnr: '00'
  network:
    name: AG
    vpn: true
    probe: 127.0.0.13:3200
""", encoding="utf-8")
    (cfg / "tunnel_forwards_ag.txt").write_text("127.0.0.13:3200:198.51.100.31:3200  # PS4\n", encoding="utf-8")
    (cfg / "tunnel_moves.json").write_text(json.dumps({"PS4": {"before": before, "local": "127.0.0.13",
        "real": "198.51.100.31", "sysnr": "00", "forwards": "tunnel_forwards_ag.txt"}}), encoding="utf-8")
    assert M.main(["--router-fix", "--vm", "ag", "--dry-run"]) == 0
    assert "gwhost" in (cfg / "systems.yaml").read_text()
    assert M.main(["--router-fix", "--vm", "ag"]) == 0
    rfc = yaml.safe_load((cfg / "systems.yaml").read_text())["systems"][0]["rfc"]
    assert rfc == {"ashost": "${PS4_RFC_HOST}", "saprouter": "/H/127.0.0.20/S/3299", "sysnr": "00"}
    fw = (cfg / "tunnel_forwards_ag.txt").read_text()
    assert "127.0.0.20:3299:127.0.0.1:3299" in fw and fw.count("3299:127.0.0.1") == 1
    out = capsys.readouterr().out
    assert "install_saprouter.sh ~/saprouter 198.51.100.31:3300" in out and "127.0.0.1:3299" in out
    assert M.main(["--router-fix", "--vm", "ag"]) == 0                     # again: same router, one forward
    assert (cfg / "tunnel_forwards_ag.txt").read_text().count("3299:127.0.0.1") == 1


def test_reachability_and_probe_use_the_router_first_hop():
    from core import reachability as R
    assert R.router_first_hop("/H/127.0.0.20/S/3299") == ("127.0.0.20", 3299)
    assert R.router_first_hop("") is None
    cfg = {"rfc": {"ashost": "198.51.100.31", "sysnr": "00", "saprouter": "/H/127.0.0.20/S/3299"}}
    assert R._sap_ports(cfg) == ("127.0.0.20", 3299, 3200)
