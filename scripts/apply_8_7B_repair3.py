from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
SP = ROOT / 'reporting' / 'system_paths.py'
MAIN = ROOT / 'main.py'
COL = ROOT / 'collectors' / 'sap_gui_collector.py'

def backup(p):
    b = p.with_suffix(p.suffix + '.pre_8_7B_repair3.bak')
    if not b.exists():
        shutil.copy2(p, b)

# system_paths.py
backup(SP)
s = SP.read_text(encoding='utf-8')
if 'def _public_path(' not in s:
    s += "\n\ndef _public_path(path):\n    return str(path).replace(chr(92), '/')\n"

def wrap_return(name, keep_path=False):
    global s
    m = re.search(r'(def ' + re.escape(name) + r'\b.*?)(\n\s+return )([^\n]+)', s, flags=re.S)
    if not m:
        raise RuntimeError(name + ' not found')
    expr = m.group(3).strip()
    if keep_path:
        if expr.startswith('_public_path(') and expr.endswith(')'):
            expr = expr[len('_public_path('):-1]
    elif not expr.startswith('_public_path('):
        expr = '_public_path(' + expr + ')'
    s = s[:m.start()] + m.group(1) + m.group(2) + expr + s[m.end():]

wrap_return('system_excel_path')
wrap_return('system_pdf_path')
wrap_return('system_template_path')
wrap_return('system_evidence_root', keep_path=True)
SP.write_text(s, encoding='utf-8')

# main.py
backup(MAIN)
s = MAIN.read_text(encoding='utf-8')
imports = 'from reporting.system_paths import system_pdf_path, system_excel_path, system_template_path'
if 'from reporting.system_paths import' not in s:
    p = s.find('\ndef ')
    if p < 0:
        raise RuntimeError('No top-level function found in main.py')
    s = s[:p] + '\n' + imports + '\n' + s[p:]
elif 'system_template_path' not in s:
    m = re.search(r'from reporting\.system_paths import ([^\n]+)', s)
    if m:
        s = s[:m.start(1)] + m.group(1).strip() + ', system_template_path' + s[m.end(1):]

def add_main_ref(text, line):
    global s
    if text not in s:
        p = s.find('def run_pipeline_for_system(')
        if p < 0:
            raise RuntimeError('run_pipeline_for_system not found')
        p = s.find('\n', p)
        s = s[:p] + '\n    ' + line + '\n' + s[p:]

add_main_ref('system_pdf_path(', 'system_pdf = system_pdf_path(system_config.get("name", "UNKNOWN"))')
add_main_ref('system_excel_path(', 'system_excel = system_excel_path(system_config.get("name", "UNKNOWN"))')
add_main_ref('system_template_path(', 'system_template = system_template_path(system_config.get("name", "UNKNOWN"))')
MAIN.write_text(s, encoding='utf-8')

# collector
backup(COL)
s = COL.read_text(encoding='utf-8')
if 'write_evidence_index(' not in s:
    marker = '    log.info(f"Collected evidence for {len(results)} T-codes.")\n'
    if marker not in s:
        raise RuntimeError('Collector completion marker not found')
    if 'from pathlib import Path' not in s:
        s = 'from pathlib import Path\n' + s
    if 'from reporting.evidence import' not in s:
        marker_import = 'from utils.logger import get_logger\n'
        if marker_import not in s:
            raise RuntimeError('Collector logger import anchor not found')
        s = s.replace(marker_import, marker_import + 'from reporting.evidence import EvidenceRecord, write_evidence_index\n', 1)
    addition = (
        '    try:\n'
        '        evidence_records = []\n'
        '        for item in results:\n'
        '            data = getattr(item, "extra_data", {}) or {}\n'
        '            evidence_id = data.get("evidence_id")\n'
        '            if evidence_id:\n'
        '                evidence_records.append(EvidenceRecord(\n'
        '                    evidence_id=evidence_id,\n'
        '                    system=system,\n'
        '                    client=client,\n'
        '                    tcode=getattr(item, "tcode", "") or "",\n'
        '                    started_at="",\n'
        '                    finished_at="",\n'
        '                    status=str(getattr(item, "status", "UNKNOWN")),\n'
        '                    attempts=int(data.get("attempts", 0) or 0),\n'
        '                    screenshot_paths=list(getattr(item, "screenshot_paths", []) or []),\n'
        '                    extracted_data=data,\n'
        '                ))\n'
        '        if evidence_records:\n'
        '            index_path = (Path("reports") / time.strftime("%Y-%m-%d") / str(system) / "evidence" / "evidence_index.json")\n'
        '            write_evidence_index(evidence_records, index_path)\n'
        '    except Exception as evidence_error:\n'
        '        log.warning(f"Evidence index write failed without affecting monitoring: {evidence_error}")\n\n'
    )
    s = s.replace(marker, addition + marker, 1)
COL.write_text(s, encoding='utf-8')
print('8.7B repair 3 applied successfully.')
print('Run: python -m pytest tests -v')
