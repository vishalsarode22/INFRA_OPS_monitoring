"""
Configuration as code: the whole set-up in one zip, to copy a landscape to
a new customer or a new VM, or to keep under version control.

EXPORT  every configuration file in config/ that defines behaviour (list
        below), with written-out secrets removed (password / token / secret /
        api key values that are not ${VAR} references are replaced by ""),
        plus env_keys.txt: every ${VAR} the files use and whether it is set
        here -- the names only, never the values.
IMPORT  a zip made by export. Checked first (dry run): only known file
        names, each YAML must parse, nothing outside config/, at most 5 MB.
        Applying backs every replaced file up to config/backups and writes
        it safely (core/config_editor.write_text_safely). A file whose
        secrets were removed on export is NOT applied (it would wipe the
        secret here) -- it is listed so it can be merged by hand.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from datetime import datetime

FILES = ["systems.yaml", "thresholds.yaml", "thresholds_systems.yaml", "monitoring_profiles.yaml", "monitoring_tasks.yaml", "ocr_patterns.yaml",
         "correlation_rules.yaml", "rca.yaml", "dashboard_settings.yaml", "runbooks.yaml", "oncall.yaml",
         "escalation.yaml", "checks.yaml", "capacity.yaml", "learned_baselines.yaml", "business_calendar.yaml",
         "action_policy.yaml", "sso.yaml", "remote_sources.yaml", "tunnel_forwards.txt"]
MARK = "# removed on export"
_SECRET_WORDS = r"(?:password|passwd|passphrase|pwd|token|secret|api_?key|authkey|credential|private_?key)"
_SECRET_KEY = re.compile(_SECRET_WORDS, re.I)
_SECRET_LINE = re.compile(r"^(\s*-?\s*[\"']?[\w.-]*" + _SECRET_WORDS + r"[\w.-]*[\"']?"
                          r"\s*:\s*)(?![\"']?\$\{)([\"']?)(\S[^#\n]*?)\2(\s*(#.*)?)$", re.I)
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
MAX_BYTES = 5 * 1024 * 1024


def _dir() -> str:
    from core.config_loader import CONFIG_DIR
    return CONFIG_DIR


_URL_CRED = re.compile(r"(://[^\s:/@'\"]+:)([^\s@/'\"]+)(@)")


def _line_redact(text: str) -> tuple[str, int]:
    out, n = [], 0
    for line in text.splitlines():
        m = _SECRET_LINE.match(line)
        if m and m.group(3).strip() not in ('""', "''", "null", "~"):
            out.append(f'{m.group(1)}""  {MARK}')
            n += 1
            continue
        if any("${" not in m.group(2) for m in _URL_CRED.finditer(line)):
            line, k = _URL_CRED.subn(r"\1REMOVED\3", line)
            n += k
            line = f"{line}  {MARK}" if "#" not in line else line.replace("#", f"{MARK} --", 1)
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), n


def _is_secret_value(v) -> bool:
    return v not in (None, "", []) and not (isinstance(v, str) and v.strip().startswith("${"))


def _walk_secrets(data, fix: bool = False) -> int:
    """Count (and with fix=True blank) secret-looking keys with a written-out value."""
    n = 0
    if isinstance(data, dict):
        for k in list(data):
            v = data[k]
            if _SECRET_KEY.search(str(k)) and not isinstance(v, (dict, list)) and _is_secret_value(v):
                n += 1
                if fix:
                    data[k] = ""
            elif _SECRET_KEY.search(str(k)) and isinstance(v, list) and \
                    any(not isinstance(x, (dict, list)) and _is_secret_value(x) for x in v):
                n += 1                                  # tokens: [abc, def]
                if fix:
                    data[k] = [x if isinstance(x, (dict, list)) or not _is_secret_value(x) else "" for x in v]
                    for x in data[k]:
                        _walk_secrets(x, fix)
            elif isinstance(v, str) and any(m.group(2) != "REMOVED" and "${" not in m.group(2)
                                            for m in _URL_CRED.finditer(v)):
                n += 1                                  # https://user:pass@host
                if fix:
                    data[k] = _URL_CRED.sub(r"\1REMOVED\3", v)
            else:
                n += _walk_secrets(v, fix)
    elif isinstance(data, list):
        for v in data:
            n += _walk_secrets(v, fix)
    return n


def redact(text: str, yaml_file: bool = True) -> tuple[str, int]:
    """Remove written-out secrets. Line by line first (keeps the comments);
    if the result still holds a secret when parsed (a flow mapping like
    {user: x, password: y}, a block scalar) or no longer parses, the file
    is exported from its parsed data instead (comments are lost then)."""
    clean, n = _line_redact(text)
    if not yaml_file:
        return clean, n
    import yaml
    try:
        left = _walk_secrets(yaml.safe_load(clean))
    except yaml.YAMLError:
        left = -1
    if left == 0:
        return clean, n
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        # Unparseable and maybe secret-bearing: export nothing of it.
        return f"{MARK}: this file did not parse as YAML and was left out\n", n + 1
    m = _walk_secrets(data, fix=True)
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return (f"{MARK} -- {m} secret value(s) blanked; the original's comments were dropped\n"
            f"# (its secrets were not all on lines of their own)\n{body}"), max(m, 1)


def export_zip() -> tuple[bytes, dict]:
    buf = io.BytesIO()
    summary = {"files": [], "secrets_removed": 0}
    refs: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            path = os.path.join(_dir(), name)
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            clean, n = redact(text, yaml_file=name.endswith(".yaml"))
            refs |= set(_ENV_REF.findall(text))
            z.writestr(f"config/{name}", clean)
            summary["files"].append(name)
            summary["secrets_removed"] += n
        env_lines = [f"{k}\t{'set' if os.getenv(k) else 'NOT SET'}" for k in sorted(refs)]
        z.writestr("env_keys.txt", "# variables referenced by these files (values are never exported)\n"
                   + "\n".join(env_lines) + "\n")
        z.writestr("README.txt", f"InfraBeatOps configuration export {datetime.now():%Y-%m-%d %H:%M}\n"
                   "Import it on the Systems page (admin), then set the variables in env_keys.txt in .env.\n")
    return buf.getvalue(), summary


def _shape_problem(name: str, data) -> str:
    """'' when the parsed file has the shape the dashboard expects, else why not."""
    if not isinstance(data, dict):
        return f"{name} must be a mapping (key: value) at the top, not {type(data).__name__}"
    if name == "systems.yaml":
        systems = data.get("systems")
        if not isinstance(systems, list) or not systems:
            return "systems.yaml needs a non-empty list under 'systems:'"
        names = []
        for i, e in enumerate(systems):
            if not isinstance(e, dict) or not str(e.get("name") or "").strip():
                return f"systems.yaml entry {i + 1} has no name"
            names.append(str(e["name"]).strip())
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            return f"systems.yaml names twice: {', '.join(dup)}"
    if name == "thresholds.yaml":
        bad = [k for k, v in data.items() if v is not None and not isinstance(v, dict)]
        if bad:
            return f"thresholds.yaml entries must be mappings: {', '.join(map(str, bad[:5]))}"
    return ""


def check_import(data: bytes) -> dict:
    """Dry run. {ok, files: [{name, action, note}], errors}."""
    import yaml
    out = {"ok": True, "files": [], "errors": []}
    if len(data) > MAX_BYTES:
        return {"ok": False, "files": [], "errors": ["zip is larger than 5 MB"]}
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return {"ok": False, "files": [], "errors": ["not a zip file"]}
    total = 0
    seen: set[str] = set()
    for info in z.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir() or name in ("env_keys.txt", "README.txt"):
            continue
        total += info.file_size
        if total > MAX_BYTES * 4:
            out["errors"].append("unpacked size too large")
            break
        base = name[len("config/"):] if name.startswith("config/") else name
        if "/" in base or base not in FILES:
            out["errors"].append(f"{name}: not a known configuration file -- ignored")
            continue
        if base in seen:
            out["errors"].append(f"{name}: in the zip twice -- refused")
            out["ok"] = False
            out["files"] = [f for f in out["files"] if f["name"] != base]
            continue
        seen.add(base)
        text = z.read(info).decode("utf-8", errors="replace")
        entry = {"name": base, "action": "replace" if os.path.exists(os.path.join(_dir(), base)) else "add",
                 "note": "", "text": text}
        if MARK in text:
            entry.update(action="skip", note="secrets were removed on export -- merge this file by hand")
        elif base.endswith(".yaml"):
            try:
                problem = _shape_problem(base, yaml.safe_load(text))
            except yaml.YAMLError as exc:
                problem = f"YAML error: {str(exc)[:150]}"
            if problem:
                entry.update(action="skip", note=problem if problem.startswith("YAML") else f"YAML error: {problem}")
                out["ok"] = False
        if entry["action"] == "replace":
            with open(os.path.join(_dir(), base), "r", encoding="utf-8", errors="replace") as fh:
                if fh.read() == text:
                    entry.update(action="same", note="identical")
        out["files"].append(entry)
    if out["errors"] and not out["files"]:
        out["ok"] = False
    return out


def _write_plain(name: str, text: str) -> None:
    """A non-YAML file (tunnel_forwards.txt): backup, temp file, atomic replace."""
    import tempfile
    from core import config_editor
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".txt", dir=_dir())
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        config_editor.backup(name)
        from utils.atomic import replace_or_overwrite   # Windows: file in use by readers
        replace_or_overwrite(tmp, os.path.join(_dir(), name))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def apply_import(data: bytes, by: str = "") -> dict:
    from core import config_editor, timeline
    from core.config_loader import invalidate_systems_cache
    res = check_import(data)
    if not res["ok"]:
        raise ValueError("; ".join(res["errors"] + [f"{f['name']}: {f['note']}" for f in res["files"]
                                                    if f["action"] == "skip" and "YAML" in f["note"]]) or "invalid")
    applied = []
    for f in res["files"]:
        if f["action"] in ("replace", "add"):
            if f["name"].endswith(".yaml"):
                def _check(parsed, _n=f["name"]):
                    problem = _shape_problem(_n, parsed)
                    if problem:
                        raise ValueError(problem)
                config_editor.write_text_safely(f["name"], f["text"], _check)
            else:
                _write_plain(f["name"], f["text"])
            applied.append(f["name"])
    invalidate_systems_cache()
    if applied:
        timeline.add("IBOPS", "config", f"Configuration imported: {', '.join(applied)}", actor=by or timeline.actor_name())
    return {"applied": applied, "skipped": [{"name": f["name"], "note": f["note"]} for f in res["files"]
                                             if f["action"] == "skip"]}
