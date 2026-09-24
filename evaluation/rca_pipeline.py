"""
Performance RCA pipeline.

    trigger/button -> capture (SAP GUI) -> OCR -> Gemini -> PDF -> email

Each stage is a function that takes what the previous one produced, so a
stage can be re-run on its own -- re-analysing a saved capture with a better
prompt, or re-sending a report -- without driving SAP GUI again. That
matters more here than in the routine sweep: an RCA capture is taken during
an incident and cannot be repeated once the incident is over.

GATES
Three switches have to be on for the full path to run, and each is checked
where it applies rather than once at the top, so a disabled tail does not
silently discard the capture:

    IBO_ENABLE_RCA     (default 0)  the feature at all
    IBO_ENABLE_AI      (default 0)  the Gemini call
    IBO_ENABLE_EMAIL   (default 0)  the dispatch

With RCA on and the other two off, the sweep still captures and writes the
PDF with a rules-based analysis; it just does not phone anyone.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from core.models import MetricResult
from utils.logger import get_logger
from utils.paths import BASE_DIR

log = get_logger(__name__, "application")

RCA_CONFIG_PATH = Path(BASE_DIR) / "config" / "rca.yaml"


def load_rca_config(path: Path | str = RCA_CONFIG_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        log.warning(f"rca.yaml not found at {path}; RCA disabled")
        return {"enabled": False}


def rca_enabled(cfg: dict) -> bool:
    env = os.environ.get("IBO_ENABLE_RCA", "0").strip().lower() in ("1", "true", "yes")
    return env and bool(cfg.get("enabled", True))


def _env_on(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes")


# --------------------------------------------------------------------------
# Stage 1: capture
# --------------------------------------------------------------------------

def rca_tasks(cfg: dict) -> list[dict]:
    return [{"tcode": t["tcode"], "action": t["action"]} for t in (cfg.get("tasks") or [])]


def db_hint_for(cfg: dict, system: str) -> str:
    dbs = (cfg.get("database") or {})
    return str((dbs.get("systems") or {}).get(system) or dbs.get("default") or "detect").lower()


def capture(system_cfg: dict, cfg: dict, rfc: Optional[dict] = None) -> list[MetricResult]:
    """
    Open SAP Logon, log in to THIS system, run the RCA task list, close SAP.

    Same sequence as the routine pipeline in main.py (kill -> launch ->
    select connection -> login -> scripting session), because an RCA that
    attaches to whatever session happens to be open captures whichever
    system the operator last looked at. It closes what it opened; a GUI
    left behind is the next sweep's stuck session.
    """
    import sap_gui.rca_actions as rca_actions
    from sap_gui.tcode_actions import ACTIONS
    from collectors.sap_gui_collector import collect_tcode_evidence

    system = system_cfg.get("name", "UNKNOWN")
    client = str(system_cfg.get("client") or "")
    stad = cfg.get("stad") or {}
    rca_actions.register(ACTIONS,
                         stad_min_ms=int(stad.get("min_response_ms", 0)),
                         stad_lookback=int(stad.get("lookback_minutes", 30)),
                         db_hint=db_hint_for(cfg, system))
    rca_actions.RUN_CONTEXT.clear()
    rca_actions.RUN_CONTEXT.update({
        "client": client,
        "instances": dict((rfc or {}).get("perf_response_by_instance") or {}),
        "checks": list((rfc or {}).get("checks") or []),
    })

    if system_cfg.get("has_gui_access") is False:
        log.warning(f"[{system}] RCA: has_gui_access=false; no GUI capture possible")
        return []

    from core.config_loader import get_launch_config
    from sap_gui.launcher import kill_sap_processes, launch_saplogon, select_connection
    from sap_gui.connection import login
    import time as _t

    log.info(f"[{system}] RCA: opening SAP Logon and logging in to {system_cfg.get('connection_name')}")
    try:
        kill_sap_processes()
        launch_cfg = get_launch_config(connection_name=system_cfg["connection_name"])
        launch_saplogon(launch_cfg["exe_path"])
        select_connection(launch_cfg["connection_name"])
        _t.sleep(2)
        result = login(client=client, username=system_cfg["username"], password=system_cfg["password"],
                       language=system_cfg.get("language", "EN"), submit=True, verify=True)
        if not result.get("success"):
            log.error(f"[{system}] RCA: SAP GUI login failed; nothing captured")
            return []
        _t.sleep(2)
        log.info(f"[{system}] RCA capture starting: {[t['tcode'] for t in rca_tasks(cfg)]}")
        return collect_tcode_evidence(rca_tasks(cfg), system=system, client=client)
    finally:
        try:
            kill_sap_processes()
            log.info(f"[{system}] RCA: SAP GUI closed")
        except Exception as e:  # noqa: BLE001
            log.warning(f"[{system}] RCA: could not close SAP GUI: {type(e).__name__}: {e}")


def rfc_context(system_cfg: dict) -> dict:
    """
    The RFC-side facts for the same moment: live dialog response, per-instance
    figures, top reports by DB time, lock ages. These are the structured truth
    the screenshots illustrate. Best-effort; an RCA with no RFC context is still
    an RCA.
    """
    try:
        from collectors.rfc_live import read_live
        p = read_live(system_cfg.get("name"), system_cfg, use_cache=True)
        keep = ("dialog_response_ms", "perf_response_by_instance", "perf_response_window",
                "instances", "work_processes", "checks", "cpu", "memory", "uptime")
        return {k: p.get(k) for k in keep if k in p}
    except Exception as e:  # noqa: BLE001
        log.info(f"RCA rfc_context unavailable: {type(e).__name__}: {e}")
        return {}


# --------------------------------------------------------------------------
# Stage 2: OCR
# --------------------------------------------------------------------------

def ocr_evidence(gui_results: list[MetricResult], cfg: dict) -> list[dict]:
    """
    One entry per screenshot: which T-code, which capture, the OCR text, and
    the structured facts the action already extracted. OCR here is
    unconditional (unlike the routine sweep) because the model gets both --
    a table the action could not parse is still readable in the text.
    """
    from sap_gui.ocr_extractor import run_ocr
    limit = int((cfg.get("analysis") or {}).get("ocr_chars_per_screen", 6000))
    out = []
    for m in gui_results:
        shots = list(getattr(m, "screenshot_paths", None) or [])
        if not shots and getattr(m, "screenshot_path", None):
            shots = [m.screenshot_path]
        facts = {k: v for k, v in (m.extra_data or {}).items()
                 if k not in ("ocr_excerpt", "ocr_lines", "screenshot", "screenshots")}
        for shot in shots:
            text = ""
            if shot and os.path.exists(shot):
                try:
                    text = run_ocr(shot) or ""
                except Exception as e:  # noqa: BLE001
                    text = f"[OCR failed: {type(e).__name__}]"
            out.append({
                "tcode": m.tcode, "screen": Path(shot).stem if shot else m.tcode,
                "path": shot, "ocr": text[:limit], "facts": facts,
                "captured_at": (m.extra_data or {}).get("finished_at", ""),
                "evidence_id": (m.extra_data or {}).get("evidence_id", ""),
            })
    return out


# --------------------------------------------------------------------------
# Stage 3: analysis
# --------------------------------------------------------------------------

RCA_SCHEMA = {
    "severity": "CRITICAL|WARNING|NORMAL",
    "headline": "one sentence: what is happening and who/what is causing it",
    "summary": "3-6 sentences a Basis consultant reads first: the state of the system in this window, in plain words, "
               "citing the figures (dialog ms, DB/CPU share, lock counts, top users/programs, DB memory)",
    "per_screen": {"SM50": "one or two sentences on what this screen shows", "SM66": "...", "ST03N": "...",
                   "SM12": "...", "STAD": "...", "ST04": "..."},
    "culprits": [{"type": "user|sql|report|lock|database|workprocess|unknown",
                  "name": "user id / statement fragment / report / table+key",
                  "evidence": "the screen and figure that implicates it",
                  "impact": "what it is doing to response time"}],
    "root_cause": "the mechanism, in Basis terms, not a restatement of the symptom",
    "contributing": ["secondary factors"],
    "actions_now": [{"step": "concrete action a Basis consultant does now", "tcode": "where"}],
    "actions_later": ["follow-ups after the incident"],
    "confidence": "high|medium|low",
    "confidence_reason": "what would raise it",
    "not_supported": ["hypotheses the evidence does not support, to stop people chasing them"],
}


# A single step this long owns a work process for that whole time.
RCA_LONG_STEP_CRITICAL_S = 600

# Programs IBOPS itself runs while collecting.
RCA_MONITOR_PROGRAMS = ("CL_SERVER_INFO",)

# Findings measured over the window (STAD) come before a point-in-time
# snapshot (SM50/SM66): PS4's 3-hour step was listed fifth, below a work
# process that happened to be busy at capture time.
_CULPRIT_ORDER = {"STAD": 0, "SM12": 1, "ST03N": 2, "SM50": 3, "SM66": 3}


def _culprit_rank(entry: dict) -> int:
    token = re.split(r"[ :]", str(entry.get("evidence") or "").strip(), maxsplit=1)[0].upper()
    return _CULPRIT_ORDER.get(token, 9)


def _rules_verdict(facts_by_screen: dict, culprits: list) -> tuple:
    """
    Severity, a root-cause sentence and the first action, from the measured
    screens. The fallback used to read "Rules-based: see culprits", grade
    every finding WARNING, and leave the heaviest program out of the actions.
    """
    from sap_gui.rca_actions import _ms_h

    stad = facts_by_screen.get("STAD") or facts_by_screen.get("stad") or {}
    top = (stad.get("top_by_response") or [{}])[0]
    user = str(top.get("user") or "")
    programs = ", ".join(top.get("programs") or []) if isinstance(top.get("programs"), (list, tuple, set)) else str(top.get("programs") or "")
    worst_ms = float(top.get("worst_ms") or top.get("resp_ms") or 0)
    cpu_ms, db_ms = float(top.get("cpu_ms") or 0), float(top.get("db_ms") or 0)
    # PRIV mode stays WARNING here, as it is in the SM66 monitoring row.
    severity = "CRITICAL" if worst_ms >= RCA_LONG_STEP_CRITICAL_S * 1000 else (
        "WARNING" if culprits else "NORMAL")
    if not user or worst_ms <= 0:
        return severity, "Rules-based: see the culprits below; model analysis unavailable.", None

    share = "CPU-bound" if cpu_ms > db_ms else "database-bound"
    root_cause = (f"{user} ran {programs or 'a program'} as a single step of {_ms_h(worst_ms)} "
                  f"({_ms_h(cpu_ms)} CPU, {_ms_h(db_ms)} database), which held a work process for "
                  f"that time and dominates the window. The step is {share}.")
    lead = {"step": (f"{programs or 'the top program'}: single step of {_ms_h(worst_ms)} by {user}, {share}. "
                     + ("Profile the ABAP (SAT) -- CPU time this high is loop or internal-table work, not the database."
                        if cpu_ms > db_ms else
                        "Trace the SQL (ST05 / SQLM) and check the indexes it uses.")
                     + " A step this long belongs in the background, not in a dialog work process."),
            "tcode": "STAD / SAT"}
    return severity, root_cause, lead


def build_prompt(system: str, trigger_reason: str, evidence: list[dict], rfc: dict) -> str:
    """
    The analyst brief. Written so the model has to point at a screen for every
    claim -- 'culprit' with no evidence field is the failure mode this format
    is built to prevent.
    """
    lines = [
        "You are a Principal SAP Basis Engineer called in during a live performance incident.",
        f"System: {system}. Trigger: {trigger_reason}.",
        "",
        "You have the screens below, captured minutes ago: the tables and fields the capture",
        "script extracted (primary), plus OCR text (secondary). Determine WHO or WHAT is causing",
        "the performance spike:",
        "culprit users, expensive SQL, memory-heavy reports, PRIV-mode work processes, or",
        "lock bottlenecks. Then the root cause mechanism, then what to do right now.",
        "",
        "Rules:",
        "- Every culprit must cite the screen and the figure that implicates it. If you cannot",
        "  point at evidence, it is a hypothesis: put it in not_supported, not culprits.",
        "- Distinguish cause from symptom. High DB time is a symptom; the statement causing",
        "  it is the cause. A user in PRIV mode is a symptom; the report that allocated the",
        "  memory is the cause.",
        "- OCR text is imperfect: SIDs and hostnames may be mangled (q->g, case lost).",
        "  Do not treat a garbled token as a finding.",
        "- Prefer the extracted fields over OCR where they disagree.",
        "- Thresholds a Basis consultant applies: DB time > 40% of response = SQL-side; CPU > 40% = ABAP/host;",
        "  wait > 50 ms = work-process exhaustion; roll > 50 ms = memory/roll area; enqueue > 50 ms = lock waits;",
        "  a user holding > 50 locks or a lock older than 60 min = SM13/hung session; a step over 60 s = long-running.",
        "- Write summary and per_screen in plain, human-readable sentences with the numbers in them.",
        "- Reply with ONLY a JSON object matching the schema. No prose outside it.",
        "",
        "Schema:",
        json.dumps(RCA_SCHEMA, indent=2),
        "",
        "=== RFC context (structured, same moment) ===",
        json.dumps(rfc, indent=1, default=str)[:6000] if rfc else "(unavailable)",
        "",
    ]
    for e in evidence:
        lines += [
            f"=== {e['tcode']} :: {e['screen']} (captured {e.get('captured_at','')}) ===",
            "-- extracted fields --",
            json.dumps(e["facts"], indent=1, default=str)[:4000] or "{}",
            "-- OCR text --",
            e["ocr"] or "(no readable text)",
            "",
        ]
    return "\n".join(lines)


class _GeminiWithImages:
    """
    Wraps the project's GeminiProvider to add inline images to the request.
    Subclasses only _payload; generate() -- retries, quota blocking, key
    rotation -- is inherited unchanged. Images are opt-in via rca.yaml
    analysis.send_images because they export production screens.
    """

    def __init__(self, image_paths: list[str]):
        from evaluation.providers.gemini import GeminiProvider

        class _P(GeminiProvider):
            def _payload(inner, prompt: str) -> dict:  # noqa: N805
                payload = super()._payload(prompt)
                parts = payload["contents"][0]["parts"]
                for p in image_paths:
                    try:
                        with open(p, "rb") as fh:
                            parts.append({"inline_data": {
                                "mime_type": "image/png",
                                "data": base64.b64encode(fh.read()).decode("ascii")}})
                    except OSError:
                        continue
                return payload

        self._p = _P()

    def generate(self, prompt: str) -> str:
        return self._p.generate(prompt)


def _parse_rca_json(raw: str) -> dict:
    txt = (raw or "").strip()
    txt = txt.replace("```json", "").replace("```", "").strip()
    start, end = txt.find("{"), txt.rfind("}")
    if start >= 0 and end > start:
        txt = txt[start:end + 1]
    return json.loads(txt)


def _num(v) -> float:
    from sap_gui.rca_actions import _num as n
    return n(v)


def _secs(v) -> float:
    from sap_gui.rca_actions import _secs as sec
    return sec(v)


def human_per_screen(facts: dict, rfc: dict) -> dict:
    """One plain paragraph per T-code, written from the extracted facts. Used when the model is off."""
    out = {}
    def ms(v):
        from sap_gui.rca_actions import _ms_h
        return _ms_h(v)

    for tag in ("SM50", "SM66"):
        f = facts.get(tag) or {}
        if not f:
            continue
        where = "on the instance" if tag == "SM50" else "across all instances"
        p = f"{tag} shows {f.get('total_processes', '?')} work processes {where}, {f.get('active_count', '?')} in use. "
        lr = f.get("long_running") or []
        lr = [r for r in lr if _secs(r.get("elapsed")) >= 60 or _secs(r.get("cpu")) >= 300][:3]
        if lr:
            p += "Long-running: " + "; ".join(
                f"{r.get('user')} running {r.get('program')} in WP {r.get('wp')} "
                + (f"for {r.get('elapsed')} s on its current step" if _secs(r.get("elapsed")) >= 60 else f"with {r.get('cpu')} of CPU time")
                for r in lr) + ". "
        else:
            p += "Nothing has been on one step for more than a minute. "
        p += f"{f['priv_count']} work process(es) are in PRIV mode." if f.get("priv_count") else "No work process is in PRIV mode."
        out[tag] = p

    f = facts.get("ST03N") or {}
    if f:
        bd = f.get("breakdown") or {}
        p = f"ST03N for {f.get('instance', 'the instance')} in the window {f.get('window', '')}: "
        if f.get("dialog_avg_resp_ms") is not None:
            p += (f"average dialog response {f['dialog_avg_resp_ms']:.0f} ms over {f.get('dialog_steps', 0):.0f} steps, of which "
                  f"{bd.get('db_pct', 0)}% database and {bd.get('cpu_pct', 0)}% CPU; wait {bd.get('wait_ms', 0):.0f} ms, roll {bd.get('roll_ms', 0):.0f} ms, "
                  f"enqueue {bd.get('lock_ms', 0):.0f} ms. ")
            for v in bd.get("verdicts") or []:
                if "no single component" not in v:
                    p += v.rstrip(".") + ". "
        else:
            p += "no dialog row could be read. "
        for v in (f.get("task_types") or {}).get("verdicts") or []:
            if "no abnormal" not in v:
                p += v.rstrip(".") + ". "
        if f.get("top_users"):
            u = f["top_users"][0]
            p += f"Heaviest user in the window: {u.get('user')} ({u.get('total_resp_ms')} ms over {u.get('steps')} steps). "
        if f.get("top_transactions"):
            t = f["top_transactions"][0]
            p += f"Heaviest transaction: {t.get('tcode')} ({t.get('total_resp_ms')} ms)."
        out["ST03N"] = p.strip()

    f = facts.get("SM12") or {}
    if f:
        out["SM12"] = " ".join(l.rstrip(".") + "." for l in f.get("focus") or [])

    f = facts.get("STAD") or {}
    if f:
        out["STAD"] = " ".join(l.rstrip(".") + "." for l in f.get("focus") or [])

    f = facts.get("ST04") or {}
    if f:
        ov = f.get("overview") or {}
        p = f"ST04 ({f.get('database', 'database')}): "
        if ov.get("memory_used_of_limit"):
            p += f"database memory {ov['memory_used_of_limit']} of the allocation limit"
            p += f", CPU usage {ov['cpu_usage']}" if ov.get("cpu_usage") else ""
            p += f"; data volume {ov['data_volume']}" if ov.get("data_volume") else ""
            p += f", log volume {ov['log_volume']}" if ov.get("log_volume") else ""
            p += ". "
        p += (f"{f['alerts_header']}. " if f.get("alerts_header") else (f"{f['overview_alerts']}. " if f.get("overview_alerts") else ""))
        for name in ("expensive_statements", "active_statements", "blocked_transactions"):
            rows = f.get(name) or []
            label = name.replace("_", " ")
            if rows:
                p += f"{len(rows)} {label} listed; the top one: " + ", ".join(v for v in rows[0][:3] if v)[:160] + ". "
            elif name in (f.get("screens") or []):
                p += f"No {label}. "
        out["ST04"] = p.strip()
    return out


def rules_based_analysis(evidence: list[dict], rfc: dict) -> dict:
    """
    What the report says when the model is off or fails. Never invents:
    every line traces to an extracted field.
    """
    from sap_gui.rca_actions import _ms_h

    culprits, actions = [], []
    facts = {e["tcode"]: e["facts"] for e in evidence}

    def wp_culprits(tag, f):
        for p in (f.get("priv") or [])[:3]:
            culprits.append({"type": "workprocess", "name": f"WP {p.get('wp')} {p.get('user','')}",
                             "evidence": f"{tag}: PRIV mode, {p.get('program','')}, elapsed {p.get('elapsed','')}s",
                             "impact": "holds a dialog work process exclusively; other users queue"})
        for r in (f.get("long_running") or [])[:3]:
            # IBOPS's own collection runs in a dialog work process under the
            # monitoring user; it was listed as the top culprit on PS4.
            if str(r.get("program", "")).upper().startswith(RCA_MONITOR_PROGRAMS):
                continue
            if _secs(r.get("elapsed")) >= 300 and str(r.get("type", "")).upper().startswith("DIA"):
                culprits.append({"type": "user", "name": f"{r.get('user')} ({r.get('program')})",
                                 "evidence": f"{tag} long-running: WP {r.get('wp')} DIA {r.get('elapsed')}s on current step, {r.get('action','')}".strip(),
                                 "impact": "dialog work process held for minutes; one fewer for everyone else"})
        for e in (f.get("program_user") or [])[:1]:
            if e.get("processes", 0) >= 4 and not str(e.get("user", "")).upper().startswith(("SAP_", "DDIC", "BATCH")):
                culprits.append({"type": "user", "name": e["user"],
                                 "evidence": f"{tag}: {e['processes']} active work processes ({', '.join(e.get('programs', []))})",
                                 "impact": "one user occupying a large share of the work process pool"})
        if f.get("priv_count"):
            actions.append({"step": f"{f['priv_count']} work process(es) in PRIV mode: identify report and user, decide whether to cancel", "tcode": tag})

    wp_culprits("SM50", facts.get("SM50") or {})
    if not (facts.get("SM50") or {}).get("priv_count"):
        wp_culprits("SM66", facts.get("SM66") or {})

    st03 = facts.get("ST03N") or {}
    bd = st03.get("breakdown") or {}
    v15 = st03.get("dialog_avg_resp_ms")
    if isinstance(v15, (int, float)) and v15 >= 1000:
        culprits.append({"type": "workprocess", "name": f"{st03.get('instance')} dialog",
                         "evidence": f"ST03N {st03.get('window','')} on {st03.get('instance')}: avg dialog {v15:.0f} ms over "
                                     f"{st03.get('dialog_steps', 0):.0f} steps; DB {bd.get('db_pct', 0)}%, CPU {bd.get('cpu_pct', 0)}%, "
                                     f"wait {bd.get('wait_ms', 0):.0f} ms, roll {bd.get('roll_ms', 0):.0f} ms",
                         "impact": "the instance is slow for every dialog user in this window"})
    for v in bd.get("verdicts") or []:
        if "no single component" not in v and "no dialog steps" not in v:
            actions.append({"step": v, "tcode": "ST03N"})
    for v in (st03.get("task_types") or {}).get("verdicts") or []:
        if "no abnormal" not in v:
            actions.append({"step": v, "tcode": "ST03N"})
    for u in (st03.get("top_users") or [])[:2]:
        if _num(u.get("total_resp_ms")) > 0:
            culprits.append({"type": "user", "name": u.get("user"),
                             "evidence": f"ST03N user profile: total response {u.get('total_resp_ms')} ms over {u.get('steps')} steps",
                             "impact": "highest response-time consumer in the window"})
    for t in (st03.get("top_transactions") or [])[:2]:
        name = str(t.get("tcode", ""))
        if name and _num(t.get("total_resp_ms")) > 0:
            culprits.append({"type": "report", "name": name,
                             "evidence": f"ST03N transaction profile: total {t.get('total_resp_ms')} ms, avg {t.get('avg_resp_ms')} ms, DB {t.get('db_ms')} ms",
                             "impact": ("custom code at the top of the profile" if name[:1].upper() in "ZY" else "top transaction by response time")})

    sm12 = facts.get("SM12") or {}
    for k in (sm12.get("contended_keys") or [])[:3]:
        culprits.append({"type": "lock", "name": f"{k.get('table')} {k.get('argument')}",
                         "evidence": f"SM12: same table+argument held by {', '.join(k.get('users', []))}",
                         "impact": "users serialised on one enqueue entry"})
    for u in (sm12.get("top_users") or [])[:1]:
        if u.get("locks", 0) >= 50:
            culprits.append({"type": "lock", "name": u["user"],
                             "evidence": f"SM12: {u['locks']} of {sm12.get('lock_count', '?')} locks held by one user",
                             "impact": "a batch or stuck session holding a large lock set; LOCK_TABLE_OVERFLOW risk"})
    for sl in (sm12.get("stale_locks") or [])[:2]:
        culprits.append({"type": "lock", "name": f"{sl.get('user')} on {sl.get('table')}",
                         "evidence": f"SM12: lock {sl.get('age_min')} min old, argument {sl.get('argument')}",
                         "impact": "stale lock -- failed update (SM13) or hung session still holding it"})
    if sm12.get("contended_key_count"):
        actions.append({"step": "Find the session holding the contended enqueue key (owner column) and check whether it is a terminated session", "tcode": "SM12"})
    if sm12.get("stale_count"):
        actions.append({"step": f"{sm12['stale_count']} lock(s) older than 60 min: check SM13 for failed updates before deleting", "tcode": "SM12 / SM13"})

    stad = facts.get("STAD") or {}
    comp = stad.get("components") or {}
    for u in (stad.get("top_by_response") or [])[:2]:
        culprits.append({"type": "user", "name": u.get("user"),
                         "evidence": f"STAD {stad.get('lookback_minutes', '?')} min: {u.get('steps')} steps, "
                                     f"total {_ms_h(u.get('total_resp_ms', 0))}, worst {_ms_h(u.get('worst_ms', 0))}, "
                                     f"{', '.join(u.get('programs', []))}",
                         "impact": "highest total response time in the window"})
    for pgm in (stad.get("top_programs") or [])[:1]:
        culprits.append({"type": "report", "name": pgm.get("program"),
                         "evidence": f"STAD: {pgm.get('steps')} steps, total {_ms_h(pgm.get('total_resp_ms', 0))}, "
                                     f"DB {_ms_h(pgm.get('db_ms', 0))}, CPU {_ms_h(pgm.get('cpu_ms', 0))}",
                         "impact": "top program by response time in the window"})
    for u in (stad.get("top_by_memory") or [])[:1]:
        if u.get("max_memory"):
            culprits.append({"type": "report", "name": f"{u.get('user')} {', '.join(u.get('programs', []))}",
                             "evidence": f"STAD: peak memory {u['max_memory']:.0f} in one step", "impact": "largest memory allocation in the window"})
    if comp.get("max_wait_ms", 0) > 50:
        actions.append({"step": f"STAD wait time up to {comp['max_wait_ms']:.0f} ms -- work process exhaustion; check SM50 free DIA count", "tcode": "SM50"})
    if comp.get("max_lock_ms", 0) > 50:
        actions.append({"step": f"STAD enqueue time up to {comp['max_lock_ms']:.0f} ms -- lock waits; match against SM12 contention", "tcode": "SM12"})

    resp = rfc.get("dialog_response_ms")
    headline = (f"Dialog response {resp:.0f} ms. " if isinstance(resp, (int, float)) else "") + (
        f"{len(culprits)} candidate cause(s) from the captured screens." if culprits
        else "No single cause stands out in the captured screens; see the evidence.")
    per_screen = human_per_screen(facts, rfc)
    summary = " ".join(per_screen.values())
    culprits.sort(key=_culprit_rank)
    severity, root_cause, lead = _rules_verdict(facts, culprits)
    if lead:
        actions.insert(0, lead)
    return {"severity": severity, "headline": headline, "summary": summary, "per_screen": per_screen,
            "culprits": culprits, "root_cause": root_cause,
            "contributing": [], "actions_now": actions, "actions_later": [],
            "confidence": "low", "confidence_reason": "rules only; model analysis was off or failed",
            "not_supported": [], "source": "rules"}


def analyze(system: str, trigger_reason: str, evidence: list[dict], rfc: dict, cfg: dict) -> dict:
    prompt = build_prompt(system, trigger_reason, evidence, rfc)
    if not _env_on("IBO_ENABLE_AI"):
        log.info(f"[{system}] RCA: IBO_ENABLE_AI=0, using rules-based analysis")
        return rules_based_analysis(evidence, rfc)
    an = cfg.get("analysis") or {}
    try:
        if an.get("send_images"):
            imgs = [e["path"] for e in evidence if e.get("path") and os.path.exists(e["path"])]
            provider = _GeminiWithImages(imgs[: int(an.get("max_images", 6))])
        else:
            from evaluation.ai_analyzer import get_provider
            provider = get_provider()
        raw = provider.generate(prompt)
        result = _parse_rca_json(raw)
        result["source"] = getattr(provider, "name", "model")
        return result
    except Exception as e:  # noqa: BLE001
        log.error(f"[{system}] RCA model analysis failed: {type(e).__name__}: {e}")
        fb = rules_based_analysis(evidence, rfc)
        fb["confidence_reason"] = f"model call failed ({type(e).__name__}); rules-based fallback"
        return fb


# --------------------------------------------------------------------------
# Stage 4 + 5: report and dispatch
# --------------------------------------------------------------------------

def report_paths(system: str, when: datetime) -> tuple[Path, Path]:
    from reporting.system_paths import system_report_dir, safe_system_name
    d = system_report_dir(system, when) / "rca"
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{safe_system_name(system)}_RCA_{when:%Y%m%d_%H%M%S}"
    return d / f"{stem}.pdf", d / f"{stem}.json"


def dispatch(system_cfg: dict, cfg: dict, pdf_path: Path, analysis: dict, trigger_reason: str) -> bool:
    if not (cfg.get("deliver") or {}).get("email", True):
        return False
    if not _env_on("IBO_ENABLE_EMAIL"):
        log.info(f"[{system_cfg.get('name')}] RCA: IBO_ENABLE_EMAIL=0, report written but not sent")
        return False
    from core.config_loader import get_smtp_config
    from notifications.rca_email import send_rca_report
    smtp = get_smtp_config(system_cfg)
    recipients = list((cfg.get("deliver") or {}).get("recipients") or []) \
        or list(smtp.get("to_emails") or [])
    if not recipients:
        log.warning("RCA: no recipients configured; report not sent")
        return False
    subject = (f"{(cfg.get('deliver') or {}).get('subject_prefix', '[InfraBeatOps RCA]')} "
               f"{system_cfg.get('name')} -- {analysis.get('severity', '?')}: {analysis.get('headline', '')[:90]}")
    return send_rca_report(smtp, recipients, subject, str(pdf_path), analysis, trigger_reason)


def run_rca(system_cfg: dict, trigger_reason: str, cfg: Optional[dict] = None,
            gui_results: Optional[list[MetricResult]] = None) -> dict:
    """
    The whole pipeline for one system. gui_results may be passed to re-run
    analysis and reporting on an existing capture without touching SAP GUI.
    """
    cfg = cfg or load_rca_config()
    system = system_cfg.get("name", "UNKNOWN")
    started = datetime.now()
    log.info(f"[{system}] RCA run starting: {trigger_reason}")

    rfc = rfc_context(system_cfg)
    if gui_results is None:
        gui_results = capture(system_cfg, cfg, rfc)
    evidence = ocr_evidence(gui_results, cfg)
    analysis = analyze(system, trigger_reason, evidence, rfc, cfg)

    from reporting.rca_report import build_rca_pdf
    pdf_path, json_path = report_paths(system, started)
    build_rca_pdf(str(pdf_path), system=system, client=str(system_cfg.get("client") or ""),
                  started=started, trigger_reason=trigger_reason, analysis=analysis,
                  evidence=evidence, rfc=rfc)
    json_path.write_text(json.dumps({
        "system": system, "started": started.isoformat(), "trigger": trigger_reason,
        "analysis": analysis, "rfc": rfc,
        "evidence": [{k: v for k, v in e.items() if k != "ocr"} | {"ocr_chars": len(e["ocr"])} for e in evidence],
    }, indent=1, default=str), encoding="utf-8")

    sent = dispatch(system_cfg, cfg, pdf_path, analysis, trigger_reason)
    log.info(f"[{system}] RCA run complete: {analysis.get('severity')} -- {analysis.get('headline','')[:120]} "
             f"(pdf={pdf_path.name}, emailed={sent})")
    return {"system": system, "pdf": str(pdf_path), "json": str(json_path),
            "severity": analysis.get("severity"), "headline": analysis.get("headline"),
            "culprits": analysis.get("culprits", []), "emailed": sent,
            "screens": len(evidence), "source": analysis.get("source")}
