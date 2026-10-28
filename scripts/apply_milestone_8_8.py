from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
COL = ROOT / "sap_gui" / "sap_gui_collector.py"

def backup(p):
    b = p.with_suffix(p.suffix + ".pre_8_8.bak")
    if not b.exists():
        shutil.copy2(p, b)

backup(MAIN)
s = MAIN.read_text(encoding="utf-8")
if "from reporting.production_reports import generate_system_reports" not in s:
    pos = s.find("\ndef run_full_pipeline")
    s = s[:pos] + "\nfrom reporting.production_reports import generate_system_reports\n" + s[pos:]

s = s.replace(
    "gui_results = collect_tcode_evidence(tasks, system_name=system_config.get('name'), client=system_config.get('client'))",
    "gui_results = collect_tcode_evidence(tasks, system=system, client=client)",
    1,
)

old = '''    report_root = f"reports/{time.strftime('%Y-%m-%d')}/{name}"
    pdf_path = generate_latex_pdf_report(
        result,
        gui_results=gui_results,
        output_path=f"{report_root}/{name}_Monitoring_Report.pdf",
    )
    excel_history_path = append_result_to_excel(
        result,
        path=f"{report_root}/{name}_Monitoring.xlsx",
    )
    metrobrands_path = fill_metrobrands_template(
        template_path=TEMPLATE_PATH,
        output_path=f"reports/{time.strftime('%Y-%m-%d')}/{name}_Monitoring Sheet.xlsx",
        gui_results=gui_results,
    )
'''
new = '''    report_paths = generate_system_reports(result, gui_results=gui_results)
    pdf_path = report_paths["pdf"]
    excel_history_path = report_paths["excel"]
    metrobrands_path = fill_metrobrands_template(
        template_path=TEMPLATE_PATH,
        output_path=str(system_template_path(name, result.cycle_timestamp)),
        gui_results=gui_results,
    )
'''
if old in s:
    s = s.replace(old, new, 1)
else:
    raise RuntimeError("Expected report block not found in main.py")

s = s.replace(
'''    except Exception as e:
        log.error(f"AI/intelligence analysis failed after final metric collection: {e}")
        result.errors.append(f"ai_analyzer: {e}")
    except Exception as e:
        log.error(f"AI analysis failed after final metric collection for {name}: {e}")
        result.errors.append(f"ai_analyzer: {e}")
''',
'''    except Exception as e:
        log.error(f"AI/intelligence analysis failed after final metric collection: {type(e).__name__}")
        result.errors.append(f"ai_analyzer: {type(e).__name__}")
''',
1,
)
MAIN.write_text(s,encoding="utf-8")

backup(COL)
s = COL.read_text(encoding="utf-8")
s = s.replace(
    "from reporting.evidence import create_evidence_id",
    "from reporting.evidence import EvidenceRecord, create_evidence_id, write_evidence_index",
    1,
)
if "from reporting.system_paths import system_evidence_root" not in s:
    marker = "from reporting.evidence import EvidenceRecord, create_evidence_id, write_evidence_index\n"
    s = s.replace(marker, marker + "from reporting.system_paths import system_evidence_root\n", 1)
s = s.replace(
    "path = capture_screenshot(session, name, system=system)",
    "path = capture_screenshot(session, name, output_dir=str(system_evidence_root(system) / 'screenshots'))",
    1,
)
COL.write_text(s,encoding="utf-8")
print("Milestone 8.8 production reporting applied.")
