import importlib
import os
import sys

import yaml

SYS = "systems:\n- name: CEAT S4P\n  rfc:\n    ashost: 1.1.1.1\n- name: CARFOUR QAS\n  customer: X\n- name: PS4\n"


def test_set_projects(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import pause_system as P
    importlib.reload(P)
    f = tmp_path / "systems.yaml"
    f.write_text(SYS, encoding="utf-8")
    monkeypatch.setattr(P, "LIVE", str(f))
    import set_projects as S
    importlib.reload(S)
    assert S.main(["ceat*=CEAT", "CARFOUR*=AG", "PS4=AG", "NOPE=Z"]) == 0
    d = {s["name"]: s for s in yaml.safe_load(f.read_text())["systems"]}
    assert d["CEAT S4P"]["customer"] == "CEAT" and d["CEAT S4P"]["rfc"]["ashost"] == "1.1.1.1"
    assert d["CARFOUR QAS"]["customer"] == "AG" and d["PS4"]["customer"] == "AG"
    assert "no system matches 'NOPE'" in capsys.readouterr().out
