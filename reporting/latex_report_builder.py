"""
Generates a PDF report via LaTeX (pdflatex), as an alternative to the
ReportLab-based pdf_builder.py. Produces a more formally typeset document.
Requires MiKTeX or another LaTeX distribution with pdflatex on PATH.

Screenshots are re-saved via Pillow before embedding, since SAP GUI
Scripting's hardCopy() PNG export occasionally produces files that
pdflatex's bundled libpng fails to parse (even though they open fine
in normal image viewers).
"""

import os
import subprocess
import shutil
from datetime import datetime

from PIL import Image

from core.models import MonitoringResult, Status
from utils.logger import get_logger
from utils.paths import BASE_DIR

log = get_logger(__name__, "application")

STATUS_COLOR_TEX = {
    Status.NORMAL: "statusgreen",
    Status.WARNING: "statusorange",
    Status.CRITICAL: "statusred",
    Status.UNKNOWN: "statusgray",
}


def _escape_tex(text: str) -> str:
    """Escapes LaTeX special characters in plain text content."""
    if text is None:
        return ""
    text = str(text)
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    for char, repl in replacements.items():
        text = text.replace(char, repl)
    return text


def _reports_dir_for_today() -> str:
    base = os.path.join(BASE_DIR, "reports")
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(base, today)
    os.makedirs(path, exist_ok=True)
    return path


def _sanitize_image_for_latex(image_path: str) -> str:
    """
    Re-saves the image via Pillow to strip any problematic PNG metadata/
    color profile that pdflatex's libpng sometimes fails to parse, even
    though the file opens fine in normal viewers. Returns a path to the
    sanitized copy (same folder, _latex suffix), or the original path
    if sanitization fails.
    """
    try:
        base, ext = os.path.splitext(image_path)
        safe_path = f"{base}_latex.png"
        if os.path.exists(safe_path):
            return safe_path
        img = Image.open(image_path).convert("RGB")
        img.save(safe_path, "PNG")
        return safe_path
    except Exception as e:
        log.warning(f"Could not sanitize image {image_path} for LaTeX, using original: {e}")
        return image_path


def _build_tex_source(result: MonitoringResult, gui_results: list = None) -> str:
    lines = []
    lines.append(r"\documentclass[11pt]{article}")
    lines.append(r"\usepackage[margin=2cm]{geometry}")
    lines.append(r"\usepackage{booktabs}")
    lines.append(r"\usepackage{xcolor}")
    lines.append(r"\usepackage{longtable}")
    lines.append(r"\usepackage{graphicx}")
    lines.append(r"\usepackage{hyperref}")
    lines.append(r"\usepackage{parskip}")
    lines.append(r"\definecolor{statusgreen}{HTML}{2E7D32}")
    lines.append(r"\definecolor{statusorange}{HTML}{E65100}")
    lines.append(r"\definecolor{statusred}{HTML}{C62828}")
    lines.append(r"\definecolor{statusgray}{HTML}{616161}")
    lines.append(r"\title{SAP BASIS Monitoring Report}")
    lines.append(r"\author{}")
    lines.append(r"\date{}")
    lines.append(r"\begin{document}")
    lines.append(r"\maketitle")

    lines.append(
        f"\\noindent System: \\textbf{{{_escape_tex(result.system)}}} \\quad "
        f"Client: \\textbf{{{_escape_tex(result.client)}}} \\quad "
        f"Generated: {_escape_tex(result.cycle_timestamp.strftime('%Y-%m-%d %H:%M:%S'))}"
    )
    lines.append(r"\vspace{0.5cm}")

    # --- Executive Summary ---
    lines.append(r"\section*{1. Executive Summary}")
    status_color = STATUS_COLOR_TEX.get(result.overall_status, "black")
    lines.append(
        f"\\noindent\\textcolor{{{status_color}}}{{\\Large\\textbf{{"
        f"Overall Status: {_escape_tex(result.overall_status.value)}}}}}"
    )
    lines.append(r"\vspace{0.3cm}")

    critical_count = len(result.critical_metrics())
    warning_count = len([m for m in result.metrics if m.status == Status.WARNING])
    lines.append(
        f"\\noindent {len(result.metrics)} metrics evaluated -- "
        f"{critical_count} critical, {warning_count} warning."
    )
    if result.errors:
        lines.append(f"\\\\ Collector errors encountered: {len(result.errors)}")
    lines.append(r"\vspace{0.5cm}")

    # --- Metrics Table ---
    lines.append(r"\section*{2. Monitoring Metrics}")
    lines.append(r"\begin{longtable}{p{4cm} p{2.5cm} p{2.5cm} p{6cm}}")
    lines.append(r"\toprule")
    lines.append(r"\textbf{Metric} & \textbf{Value} & \textbf{Status} & \textbf{Detail} \\")
    lines.append(r"\midrule")
    lines.append(r"\endhead")
    for m in result.metrics:
        color = STATUS_COLOR_TEX.get(m.status, "black")
        lines.append(
            f"{_escape_tex(m.name)} & {_escape_tex(m.display_value)} & "
            f"\\textcolor{{{color}}}{{{_escape_tex(m.status.value)}}} & "
            f"{_escape_tex(m.detail[:60])} \\\\"
        )
    lines.append(r"\bottomrule")
    lines.append(r"\end{longtable}")
    lines.append(r"\vspace{0.5cm}")

    # --- AI Analysis ---
    lines.append(r"\section*{3. AI Analysis}")
    ai = result.ai_analysis
    if ai and ai.raw_response:
        lines.append(f"\\textbf{{Severity:}} {_escape_tex(ai.severity)}\\\\")
        lines.append(f"\\textbf{{Likely Root Cause:}} {_escape_tex(ai.likely_root_cause)}")
        lines.append(r"\vspace{0.2cm}")

        if ai.evidence:
            lines.append(r"\textbf{Evidence:}")
            lines.append(r"\begin{itemize}")
            for ev in ai.evidence:
                lines.append(f"\\item {_escape_tex(ev)}")
            lines.append(r"\end{itemize}")

        if ai.recommended_actions:
            lines.append(r"\textbf{Recommended Actions:}")
            lines.append(r"\begin{enumerate}")
            for action in ai.recommended_actions:
                lines.append(f"\\item {_escape_tex(action)}")
            lines.append(r"\end{enumerate}")

        lines.append(f"\\textbf{{Confidence:}} {_escape_tex(ai.confidence)}")
    else:
        lines.append("No AI analysis available for this cycle.")
    lines.append(r"\vspace{0.5cm}")

    # --- T-code Evidence ---
    if gui_results:
        lines.append(r"\section*{4. T-code Evidence}")
        for m in gui_results:
            if m.extra_data:
                data_str = ", ".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in m.extra_data.items())
                lines.append(f"\\textbf{{{_escape_tex(m.tcode)}}} -- {_escape_tex(data_str)}")
            else:
                lines.append(f"\\textbf{{{_escape_tex(m.tcode)}}}")
            lines.append(r"\vspace{0.1cm}")
            for shot_path in (m.screenshot_paths or []):
                if shot_path and os.path.exists(shot_path):
                    safe_path = _sanitize_image_for_latex(shot_path)
                    tex_path = safe_path.replace("\\", "/")
                    lines.append(
                        f"\\begin{{center}}\\includegraphics[width=0.85\\textwidth]"
                        f"{{{tex_path}}}\\end{{center}}"
                    )
            lines.append(r"\vspace{0.4cm}")

    lines.append(
        r"\vfill {\footnotesize This report was generated automatically by the "
        r"SAP BASIS AI Monitoring Agent.}"
    )
    lines.append(r"\end{document}")

    return "\n".join(lines)


def generate_latex_pdf_report(result: MonitoringResult, gui_results: list = None,
                               output_path: str = None) -> str:
    """
    Writes a .tex file and compiles it with pdflatex.
    Returns the path to the generated PDF.
    Raises RuntimeError with pdflatex's log tail if compilation fails.
    """
    if shutil.which("pdflatex") is None:
        raise RuntimeError("pdflatex not found on PATH. Install MiKTeX or another LaTeX distribution.")

    work_dir = _reports_dir_for_today()
    tex_path = os.path.join(work_dir, "SAP_BASIS_Report_LaTeX.tex")
    pdf_path = os.path.join(work_dir, "SAP_BASIS_Report_LaTeX.pdf")

    tex_source = _build_tex_source(result, gui_results)
    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(tex_source)

    # Run twice: LaTeX sometimes needs a second pass for references/TOC to settle.
    for _ in range(2):
        proc = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
             os.path.basename(tex_path)],
            cwd=work_dir, capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            log_tail = proc.stdout[-3000:] if proc.stdout else proc.stderr[-3000:]
            log.error(f"pdflatex failed:\n{log_tail}")
            raise RuntimeError(f"pdflatex compilation failed. Log tail:\n{log_tail}")

    log.info(f"LaTeX PDF report generated: {pdf_path}")
    if output_path and output_path != pdf_path:
        shutil.copy(pdf_path, output_path)
        return output_path
    return pdf_path