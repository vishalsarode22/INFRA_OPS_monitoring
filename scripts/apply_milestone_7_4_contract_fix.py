from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'dashboard' / 'static' / 'index.html'

if not HTML.exists():
    raise SystemExit(f'Dashboard HTML not found: {HTML}')

s = HTML.read_text(encoding='utf-8')
if 'id="correlationView"' in s:
    print('Milestone 7.4 correlation contract already present.')
    raise SystemExit(0)

css = '''
  .correlation-view { margin-top:14px; border-top:1px solid var(--border); padding-top:14px; }
  .correlation-graph { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }
  .correlation-node { border:1px solid var(--border); border-radius:9px; padding:11px; background:var(--card); }
  .correlation-label { color:var(--muted); font-size:9px; font-weight:900; letter-spacing:.8px; }
  .correlation-title { margin-top:4px; font-size:12px; font-weight:850; }
  .correlation-detail { margin-top:4px; color:var(--muted); font-size:10.5px; line-height:1.45; }
  .correlation-note { margin-top:10px; color:var(--muted); font-size:10.5px; line-height:1.45; }
  @media (max-width:760px) { .correlation-graph { grid-template-columns:1fr; } }
'''
if '</style>' not in s:
    raise RuntimeError('Dashboard style boundary not found.')
s = s.replace('</style>', css + '\n</style>', 1)

html = '''
      <section class="list-card correlation-view" id="correlationView">
        <div class="section-title" style="font-size:13px;">System Correlation</div>
        <div class="section-sub">Read-only relationship view of incident evidence and operational intelligence.</div>
        <div class="correlation-graph" id="correlationGraph">
          <div class="correlation-node">
            <div class="correlation-label">INCIDENT</div>
            <div class="correlation-title" id="correlationIncident">No incident selected</div>
            <div class="correlation-detail" id="correlationIncidentDetail">Deterministic monitoring severity is authoritative.</div>
          </div>
          <div class="correlation-node">
            <div class="correlation-label">SAP EVIDENCE</div>
            <div class="correlation-title" id="correlationEvidence">No evidence</div>
            <div class="correlation-detail" id="correlationEvidenceDetail">No correlated metrics recorded.</div>
          </div>
          <div class="correlation-node">
            <div class="correlation-label">INTELLIGENCE</div>
            <div class="correlation-title" id="correlationIntelligence">Unavailable</div>
            <div class="correlation-detail" id="correlationIntelligenceDetail">AI is advisory only; historical matches are reference only.</div>
          </div>
        </div>
        <div class="correlation-note">This is a read-only relationship view. It cannot change incident severity, acknowledge incidents, resolve incidents, or modify monitoring evidence. AI is advisory only. Historical intelligence is reference only.</div>
      </section>
'''
if '</body>' not in s:
    raise RuntimeError('Dashboard body boundary not found.')
s = s.replace('</body>', html + '\n</body>', 1)

js = '''
function renderCorrelationView(incident, ai, intelligence) {
  const incidentEl = document.getElementById("correlationIncident");
  const incidentDetailEl = document.getElementById("correlationIncidentDetail");
  const evidenceEl = document.getElementById("correlationEvidence");
  const evidenceDetailEl = document.getElementById("correlationEvidenceDetail");
  const intelligenceEl = document.getElementById("correlationIntelligence");
  const intelligenceDetailEl = document.getElementById("correlationIntelligenceDetail");
  if (!incidentEl) return;
  const severity = String((incident && incident.severity) || "UNKNOWN").toUpperCase();
  const evidence = Array.isArray(incident && incident.evidence) ? incident.evidence : [];
  const metrics = Array.isArray(incident && incident.affected_metrics) ? incident.affected_metrics : [];
  incidentEl.textContent = (incident && (incident.title || incident.rule_id)) || "Incident";
  incidentDetailEl.textContent = severity + " · " + ((incident && incident.status) || "UNKNOWN") + " · authoritative";
  evidenceEl.textContent = metrics.length ? metrics.length + " affected metric(s)" : "SAP evidence";
  evidenceDetailEl.textContent = metrics.concat(evidence).slice(0, 5).join(" · ") || "No correlated metrics recorded.";
  const signal = intelligence && (intelligence.overall_signal || intelligence.signal || intelligence.overall_status);
  const score = intelligence && Number(intelligence.score);
  intelligenceEl.textContent = signal || (ai ? "AI RCA available" : "No intelligence");
  intelligenceDetailEl.textContent = Number.isFinite(score) ? "Score " + score.toFixed(2) + " · advisory only · reference only" : "AI is advisory only; historical matches are reference only.";
}
'''
if 'function renderCorrelationView(' not in s:
    s = s.replace('</script>', js + '\n</script>', 1)

call = 'renderCorrelationView(incident, ai, intel);'
if call not in s:
    for marker in [
        'const limitations = ai && Array.isArray(ai.limitations) ? ai.limitations : [];',
        'const actions = ai && Array.isArray(ai.recommended_actions) ? ai.recommended_actions : [];',
        'const evidence = Array.isArray(incident.evidence) ? incident.evidence : [];',
    ]:
        if marker in s:
            s = s.replace(marker, marker + '\n    ' + call, 1)
            break

HTML.write_text(s, encoding='utf-8')
print('Milestone 7.4 contract fix applied safely.')
