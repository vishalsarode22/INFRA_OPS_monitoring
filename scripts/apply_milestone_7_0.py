from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'dashboard' / 'static' / 'index.html'


def patch(path: Path, old: str, new: str, marker: str):
    text = path.read_text(encoding='utf-8')
    if marker in text:
        return False
    if old not in text:
        raise RuntimeError(f'Safe insertion point not found in {path}: {old[:80]!r}')
    path.write_text(text.replace(old, new, 1), encoding='utf-8')
    return True

# 1) Intelligence UI styling.
patch(
    APP,
    '  .ai-box { font-size: 13px; line-height: 1.8; color: #d5d8dd; }\n',
    '''  .ai-box { font-size: 13px; line-height: 1.8; color: #d5d8dd; }\n'''
    '''  .intel-grid { display:grid; grid-template-columns: 170px 1fr; gap:16px; align-items:stretch; }\n'''
    '''  .intel-score { background:#0e0f12; border:1px solid var(--border); border-radius:10px; padding:16px; text-align:center; }\n'''
    '''  .intel-signal { font-size:22px; font-weight:900; letter-spacing:.5px; margin:8px 0 2px; }\n'''
    '''  .intel-score-label { color:var(--muted); font-size:10.5px; font-weight:800; letter-spacing:.8px; }\n'''
    '''  .intel-score-value { font-size:28px; font-weight:900; }\n'''
    '''  .intel-findings { display:grid; gap:8px; }\n'''
    '''  .intel-finding { background:#0e0f12; border:1px solid var(--border); border-radius:8px; padding:10px 12px; font-size:12px; }\n'''
    '''  .intel-signal-row { display:flex; justify-content:space-between; gap:12px; padding:8px 0; border-bottom:1px solid var(--border); font-size:11.5px; }\n'''
    '''  .intel-signal-row:last-child { border-bottom:none; }\n''',
    '.intel-grid { display:grid;'
)

# 2) Intelligence panel in system detail.
patch(
    APP,
    '''      <div class="list-card" style="margin-bottom:16px;">\n        <div class="section-title" style="font-size:13px; margin-bottom:12px;">CPU & Memory trend</div>\n''',
    '''      <div class="list-card" style="margin-bottom:16px;">\n        <div class="section-title" style="font-size:13px; margin-bottom:12px;">Operational intelligence</div>\n        <div id="detailIntelligence"><div class="empty">No intelligence available.</div></div>\n      </div>\n\n      <div class="list-card" style="margin-bottom:16px;">\n        <div class="section-title" style="font-size:13px; margin-bottom:12px;">CPU & Memory trend</div>\n''',
    'id="detailIntelligence"'
)

# 3) Consume the single overview contract rather than the legacy /api/status endpoint.
patch(
    APP,
    '''  await Promise.all(allSystems.map(async s => {\n    const r = await fetch("/api/status/" + encodeURIComponent(s.name));\n    systemStatuses[s.name] = await r.json();\n  }));\n''',
    '''  await Promise.all(allSystems.map(async s => {\n    try {\n      const r = await fetch("/api/systems/" + encodeURIComponent(s.name) + "/overview");\n      systemStatuses[s.name] = r.ok ? await r.json() : { available: false };\n      systemStatuses[s.name].available = r.ok;\n    } catch (e) {\n      systemStatuses[s.name] = { available: false };\n    }\n  }));\n''',
    'fetch("/api/systems/" + encodeURIComponent(s.name) + "/overview")'
)

# 4) Add intelligence rendering.
patch(
    APP,
    '''  const evEl = document.getElementById("detailEvidence");\n''',
    '''  renderIntelligence(data.operational_intelligence);\n\n  const evEl = document.getElementById("detailEvidence");\n''',
    'renderIntelligence(data.operational_intelligence);'
)

patch(
    APP,
    '''async function loadTrend(name) {\n''',
    '''function renderIntelligence(intel) {\n  const el = document.getElementById("detailIntelligence");\n  if (!intel) {\n    el.innerHTML = '<div class="empty">Operational intelligence is not available for this snapshot.</div>';\n    return;\n  }\n\n  const signal = String(intel.overall_signal || "UNKNOWN").toUpperCase();\n  const score = Math.max(0, Math.min(1, Number(intel.score || 0)));\n  const signalColor = signal === "HIGH" ? "var(--red)" : signal === "MEDIUM" ? "var(--amber)" : "var(--green)";\n  const findings = Array.isArray(intel.key_findings) ? intel.key_findings.slice(0, 5) : [];\n  const signals = Array.isArray(intel.signals) ? intel.signals.slice(0, 6) : [];\n\n  el.innerHTML = `\n    <div class="intel-grid">\n      <div class="intel-score">\n        <div class="intel-score-label">OVERALL SIGNAL</div>\n        <div class="intel-signal" style="color:${signalColor}">${signal}</div>\n        <div class="intel-score-value">${Math.round(score * 100)}%</div>\n        <div class="intel-score-label">confidence signal</div>\n      </div>\n      <div>\n        <div class="section-sub" style="margin-bottom:8px;">Evidence fusion · baseline · recurrence · change signals</div>\n        <div class="intel-findings">\n          ${findings.length ? findings.map(f => `<div class="intel-finding">${escapeHtml(String(f))}</div>`).join("") : '<div class="empty">No key findings.</div>'}\n        </div>\n      </div>\n    </div>\n    ${signals.length ? `<div style="margin-top:12px;">${signals.map(s => `\n      <div class="intel-signal-row">\n        <span><b>${escapeHtml(String(s.category || "SIGNAL"))}</b> · ${escapeHtml(String(s.metric || ""))}</span>\n        <span>${Math.round(Math.max(0, Math.min(1, Number(s.strength || 0))) * 100)}%</span>\n      </div>`).join("")}</div>` : ""}\n  `;\n}\n\nfunction escapeHtml(value) {\n  return value.replace(/[&<>\\\"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[ch]));\n}\n\nasync function loadTrend(name) {\n''',
    'function renderIntelligence(intel)'
)

# 5) Reset intelligence when there is no snapshot.
patch(
    APP,
    '''    document.getElementById("detailAi").innerHTML = "No analysis available.";\n    document.getElementById("detailEvidence").innerHTML = '<div class="empty">No data yet.</div>';\n''',
    '''    document.getElementById("detailAi").innerHTML = "No analysis available.";\n    document.getElementById("detailIntelligence").innerHTML = '<div class="empty">No intelligence available.</div>';\n    document.getElementById("detailEvidence").innerHTML = '<div class="empty">No data yet.</div>';\n''',
    'detailIntelligence\").innerHTML = \'<div class="empty">No intelligence available.\''
)

print('Milestone 7.0 applied safely.')
