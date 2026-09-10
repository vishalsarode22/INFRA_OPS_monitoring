"""
Regression tests for the RFC collector.

These are fakes -- no SAP system required -- which is the point: the bugs
they cover all shipped in a codebase whose tests all needed a live system,
so nobody ever ran them.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors import rfc_collector as rc
from core.models import Status


class FakeSession:
    def __init__(self, payload, cfg=None):
        self.p = payload
        self.ok = True
        self.cfg = cfg or {"client": "000"}

    def call(self, name, **kw):
        return self.p if name == "Z_GET_OBSERVABILITY_DATA" else None

    def read_table(self, *a, **k):
        return None


BASE = {
    "EV_SHORT_DUMPS": 0, "EV_LOCK_ENTRIES": 1, "EV_ACTIVE_USERS": 2,
    "EV_CPU_UTIL_PCT": 42, "EV_MEM_UTIL_PCT": 24, "EV_TOTAL_RAM_GB": 23,
    "EV_LOAD_1M": 0, "EV_FREE_DIA_WP": 3, "EV_TOTAL_DIA_WP": 10,
}


def _by_key(payload):
    return {m.name: m for m in rc._from_function_module(FakeSession(payload))}


class TestNoCommandDerivedOsMetrics:
    """
    CPU, memory and load must come from /SDF/SMON_HEADER, never from the
    function module's SXPG exports.

    Those exports are fed by SM69 external commands, which need S_LOG_COM --
    remote command execution on the application server. The grant has been
    removed, so a transported older FM may still return values for them and
    they must be ignored rather than silently displayed.
    """

    def test_fm_os_exports_are_not_mapped(self):
        from collectors.rfc_collector import _FM_METRICS
        for export in ("EV_CPU_UTIL_PCT", "EV_MEM_UTIL_PCT",
                       "EV_LOAD_1M", "EV_TOTAL_RAM_GB"):
            assert export not in _FM_METRICS, (
                f"{export} is command-derived and must not feed the dashboard")

    def test_stale_fm_values_do_not_reach_the_payload(self):
        import collectors.rfc_live as L

        class Session:
            ok = True
            cfg = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def call(self, name, **kw):
                if name == "Z_GET_OBSERVABILITY_DATA":
                    # An older transported FM still running the SXPG block.
                    return {"EV_CPU_UTIL_PCT": 99, "EV_MEM_UTIL_PCT": 99,
                            "EV_LOAD_1M": 99, "EV_ACTIVE_USERS": 3}
                return None
            def read_table(self, *a, **k): return None

        original = L.SapSession
        try:
            L.SapSession = lambda sid, cfg: Session()
            L._cache.clear()
            payload = L.read_live("TST", {"rfc": {"ashost": "x"}}, use_cache=False)
        finally:
            L.SapSession = original

        assert payload["cpu"] is None
        assert payload["memory"] is None
        assert payload["load_1m"] is None
        assert payload.get("os_hint"), "operator should be told to schedule SMON"

    def test_smon_supplies_the_values(self):
        import collectors.rfc_live as L

        class Session:
            ok = True
            cfg = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def call(self, name, **kw):
                return {"EV_ACTIVE_USERS": 3} if name == "Z_GET_OBSERVABILITY_DATA" else None
            def read_table(self, table, fields=None, *a, **k):
                if table == "/SDF/SMON_HEADER":
                    # One row in the order of collectors.rfc_live._SMON_FIELDS.
                    # Positional on purpose: RFC_READ_TABLE returns rows in the
                    # order of the FIELDS it was asked for, and the collector
                    # maps them back by name. Decimal comma as the RFC user's
                    # locale returns it ("0,18" must parse as 0.18, not 18).
                    row = {
                        "DATUM": "20260901", "TIME": "143100",
                        "SERVER": "nwtest_TST_02",
                        "IDLE_TOTAL": "95",          # cpu = 100 - 95 = 5
                        "FREE_MEM_PERC": "62,0",     # memory = 100 - 62 = 38
                        "FREE_MEM_MB": "4150", "FREE_MEM_MB_INC_FS": "5000",
                        "CPU_CONS": "0,18",          # load_1m
                        "DIAAVG60": "1", "DIAQ": "0", "UPDQ": "0",
                        "USERS": "3", "SESSIONS": "4",
                        "NETRTT": "2730", "AVAILCPUS": "4",
                    }
                    from collectors.rfc_live import _SMON_FIELDS
                    return [[row[f] for f in (fields or _SMON_FIELDS)]]
                return None

        original = L.SapSession
        try:
            L.SapSession = lambda sid, cfg: Session()
            L._cache.clear()
            payload = L.read_live("TST", {"rfc": {"ashost": "x"}}, use_cache=False)
        finally:
            L.SapSession = original

        assert payload["cpu"] == 5           # 100 - IDLE_TOTAL
        assert payload["memory"] == 38       # 100 - FREE_MEM_PERC
        assert payload["load_1m"] == 0.18    # "0,18" not 18
        assert payload["os_source"] == "SMON"
        assert payload["smon"]["dialog_queue"] == 0


class TestGrading:
    def test_dump_escalation(self):
        assert _by_key({**BASE, "EV_SHORT_DUMPS": 0})["sap.st22.dumps"].status is Status.NORMAL
        assert _by_key({**BASE, "EV_SHORT_DUMPS": 2})["sap.st22.dumps"].status is Status.WARNING
        assert _by_key({**BASE, "EV_SHORT_DUMPS": 9})["sap.st22.dumps"].status is Status.CRITICAL

    def test_wp_saturation_is_derived(self):
        # 3 free of 10 total = 70% busy.
        assert _by_key(BASE)["sap.sm66.wp_saturation_pct"].value == 70.0

    def test_absent_export_produces_no_metric(self):
        # A missing export must be ABSENT, never a fabricated zero.
        assert "sap.sm58.stuck_entries" not in _by_key(BASE)


class TestRouteString:
    def test_route_preserved_exactly(self):
        # Stripping the trailing "/H/" broke CEQ with
        # "NiPGetHostByName: H/172.16.1.50 not found".
        route = "/H/3.108.28.197/W/secret/H/"
        assert rc._format_saprouter(route) == route

    def test_leading_slash_added(self):
        assert rc._format_saprouter("H/1.2.3.4/H/") == "/H/1.2.3.4/H/"

    def test_empty_route(self):
        assert rc._format_saprouter("") == ""
        assert rc._format_saprouter(None) == ""


class TestCooldown:
    def setup_method(self, method=None):
        rc.clear_cooldown()

    def test_auth_failure_parks_longest(self):
        # Retrying a wrong password is how a monitoring account gets locked.
        assert rc._cooldown_for("Logon failed: password") == rc._COOLDOWN_AUTH
        assert rc._cooldown_for("WSAETIMEDOUT: timed out") == rc._COOLDOWN_TIMEOUT
        assert rc._cooldown_for("route permission denied") == rc._COOLDOWN_ROUTE

    def test_parked_system_short_circuits(self):
        rc._park("QAS", "WSAETIMEDOUT: Connection timed out")
        left, err = rc.cooldown_remaining("QAS")
        assert left > 0 and "timed out" in err
        metrics, error = rc.collect_rfc_metrics("QAS", {"rfc": {}})
        assert metrics == [] and "retrying in" in error

    def test_clear_cooldown_allows_retry(self):
        rc._park("QAS", "timed out")
        rc.clear_cooldown("QAS")
        assert rc.cooldown_remaining("QAS")[0] == 0


class TestConfig:
    def test_incomplete_config_returns_none(self):
        assert rc.build_rfc_params({"rfc": {"ashost": "h"}}) is None
        assert rc.build_rfc_params({}) is None

    def test_sysnr_zero_padded(self):
        p = rc.build_rfc_params({"rfc": {
            "ashost": "10.1.0.178", "sysnr": 2,
            "username": "MONITORING", "password": "x"}})
        assert p["sysnr"] == "02" and p["ashost"] == "10.1.0.178"

    def test_no_hardcoded_credentials_in_source(self):
        """Scans string literals, skipping docstrings so the explanatory
        comments describing removed values do not trip the check."""
        import ast
        path = os.path.join(os.path.dirname(__file__), "..",
                            "collectors", "rfc_collector.py")
        tree = ast.parse(open(path, encoding="utf-8").read())

        docstring_ids = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstring_ids.add(id(body[0].value))

        literals = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstring_ids):
                literals.append(node.value)
        blob = " ".join(literals)

        for leaked in ("Pass@123", "Init06", "172.16.1.", "10.1.0."):
            assert leaked not in blob, f"hardcoded {leaked!r} in rfc_collector"


class TestModuleIntegrity:
    """
    Guards against the failure that broke startup: an edit that replaced a
    block of a file and silently removed functions below it.

    core/production_health.py imports configuration_summary at module load,
    so losing it made `import dashboard.app` fail outright -- the app would
    not boot at all.
    """

    def test_config_loader_exports_everything_importers_need(self):
        import ast
        root = os.path.join(os.path.dirname(__file__), "..")
        target = os.path.join(root, "core", "config_loader.py")
        tree = ast.parse(open(target, encoding="utf-8").read())

        exported = {n.name for n in tree.body
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef))}

        required = {
            "get_systems", "get_systems_safe", "add_system", "delete_system",
            "get_thresholds", "get_smtp_config", "get_launch_config",
            "get_monitoring_tasks", "get_linux_ssh_credentials",
            "get_sap_instance_nr", "get_ocr_patterns",
            "get_scheduler_interval_minutes", "set_scheduler_interval_minutes",
            # Imported by core/production_health.py at module load.
            "configuration_summary", "validate_configuration",
            "validate_systems_configuration",
        }
        missing = sorted(required - exported)
        assert missing == [], f"config_loader lost: {missing}"

    def test_no_broken_local_imports(self):
        """Every `from <local module> import <name>` must resolve."""
        import ast
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

        defined = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".git", "venv", "tests")]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                mod = os.path.relpath(path, root).replace(os.sep, ".")[:-3]
                if mod.endswith(".__init__"):
                    mod = mod[:-9]
                try:
                    tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
                except SyntaxError:
                    continue
                names = set()
                for n in tree.body:
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.add(n.name)
                    elif isinstance(n, ast.Assign):
                        names.update(t.id for t in n.targets if isinstance(t, ast.Name))
                    elif isinstance(n, (ast.Import, ast.ImportFrom)):
                        names.update(a.asname or a.name.split(".")[0] for a in n.names)
                defined[mod] = names

        broken = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".git", "venv", "tests")]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.ImportFrom)
                            and node.level == 0 and node.module in defined):
                        continue
                    for alias in node.names:
                        # A submodule import (`from pkg import module`) is not
                        # a name lookup, so skip anything that is itself a module.
                        if f"{node.module}.{alias.name}" in defined:
                            continue
                        if alias.name != "*" and alias.name not in defined[node.module]:
                            broken.append(
                                f"{os.path.relpath(path, root)}: "
                                f"from {node.module} import {alias.name}")

        assert broken == [], "unresolved local imports:\n  " + "\n  ".join(broken)


class TestSapcontrolParser:
    """
    sapcontrol output shape varies by kernel release, locale and whether
    -format script was used. The old parser required a header line starting
    with "name," and returned nothing without it -- TST logged "collected
    successfully" and then "Could not find header line", losing dispatcher,
    ICM and gateway state even though sapcontrol had run fine.
    """

    @staticmethod
    def _parse(raw):
        import types
        pm = types.ModuleType("paramiko")
        pm.SSHClient = object
        pm.AutoAddPolicy = object
        sys.modules.setdefault("paramiko", pm)
        from collectors.sap_process_collector import parse_sap_process_list
        return parse_sap_process_list({"raw": raw})

    def test_csv_with_header(self):
        out = self._parse(
            "name, description, dispstatus, textstatus\n"
            "disp+work, Dispatcher, GREEN, Running\n"
            "icman, ICM, YELLOW, Starting")
        assert len(out) == 2
        assert out[0].status is Status.NORMAL
        assert out[1].status is Status.WARNING

    def test_csv_without_header(self):
        out = self._parse("disp+work, Dispatcher, GREEN, Running\n"
                          "icman, ICM, RED, Stopped")
        assert len(out) == 2 and out[1].status is Status.CRITICAL

    def test_script_format(self):
        out = self._parse("0 name: disp+work\n0 dispstatus: GREEN\n0 textstatus: Running\n"
                          "1 name: icman\n1 dispstatus: RED\n1 textstatus: Stopped")
        assert len(out) == 2 and out[1].status is Status.CRITICAL

    def test_unknown_colour_is_never_normal(self):
        # An unreadable process state must not render as healthy.
        out = self._parse("disp+work, Dispatcher, MAGENTA, ?")
        assert out == [] or out[0].status is Status.UNKNOWN

    def test_garbage_returns_nothing_rather_than_guessing(self):
        assert self._parse("some unrelated output\nno rows here") == []


class TestSnapshotCarryForward:
    """
    A sweep where SAP GUI failed used to overwrite a snapshot that HAD
    evidence with one that had none. TST captured 21 T-codes at 14:33; a
    15:34 run where the GUI lost focus replaced it with gui_evidence: 0.
    One bad run destroyed the last good screenshots.
    """

    @staticmethod
    def _harness():
        import os, tempfile
        from datetime import datetime
        import core.status_snapshot as S
        from core.models import MonitoringResult, MetricResult, Status

        tmp = tempfile.mkdtemp()
        S._snapshot_path = lambda n: os.path.join(tmp, f"{n}.json")

        def result(ts):
            r = MonitoringResult(system="TST", client="000",
                                 cycle_timestamp=datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"))
            r.metrics = [MetricResult(name="cpu", value=None,
                                      display_value="5%", status=Status.NORMAL)]
            r.events, r.incidents = [], []
            r.compute_overall_status()
            return r

        class Evidence:
            def __init__(self, tcode):
                self.tcode = tcode
                self.display_value = "captured (1)"
                self.extra_data = {"evidence_id": "EV-" + tcode}

        return S, result, Evidence

    def test_evidence_survives_a_failed_gui_cycle(self):
        S, result, Evidence = self._harness()
        S.save_snapshot(result("2026-08-31 14:33:00"),
                        gui_results=[Evidence("ST22"), Evidence("SM12")],
                        system_name="TST")
        S.save_snapshot(result("2026-08-31 15:34:25"), gui_results=[], system_name="TST")

        snap = S.load_snapshot("TST")
        assert len(snap["gui_evidence"]) == 2, "evidence was erased by a failed cycle"
        assert snap["gui_evidence_stale"] is True
        assert snap["gui_evidence"][0]["captured_at"] == "2026-08-31 14:33:00"

    def test_metrics_are_never_carried_forward(self):
        # Metrics are live readings. A stale one shown as current is exactly
        # the lie this project exists to remove.
        S, result, Evidence = self._harness()
        S.save_snapshot(result("2026-08-31 14:33:00"),
                        gui_results=[Evidence("ST22")], system_name="TST")
        S.save_snapshot(result("2026-08-31 15:34:25"), gui_results=[], system_name="TST")

        snap = S.load_snapshot("TST")
        assert snap["cycle_timestamp"] == "2026-08-31 15:34:25"

    def test_fresh_evidence_replaces_stale(self):
        S, result, Evidence = self._harness()
        S.save_snapshot(result("2026-08-31 14:33:00"),
                        gui_results=[Evidence("ST22"), Evidence("SM12")],
                        system_name="TST")
        S.save_snapshot(result("2026-08-31 15:34:25"), gui_results=[], system_name="TST")
        S.save_snapshot(result("2026-08-31 16:00:00"),
                        gui_results=[Evidence("ST22")], system_name="TST")

        snap = S.load_snapshot("TST")
        assert len(snap["gui_evidence"]) == 1
        assert snap["gui_evidence"][0]["stale"] is False
        assert not snap.get("gui_evidence_stale")
