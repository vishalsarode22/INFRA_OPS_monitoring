from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
HTML=ROOT/'dashboard'/'static'/'index.html'
s=HTML.read_text(encoding='utf-8')
if 'id="correlationView"' in s:
    print('Milestone 7.4 already applied.')
    raise SystemExit
if 'id="incidentCommandCenter"' not in s:
    raise RuntimeError('Milestone 7.2 Incident Command Center not found. Apply 7.2 first.')
css=r'''
  .correlation-view{margin-top:14px;border-top:1px solid var(--border);padding-top:14px}
  .correlation-toolbar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
  .correlation-graph{display:grid;grid-template-columns:1fr;gap:8px;min-height:120px}
  .corr-node{border:1px solid var(--border);background:var(--card);border-radius:9px;padding:10px;display:flex;align-items:center;gap:10px}
  .corr-node.root{border-color:var(--red);background:var(--red-dim)}
  .corr-node .kind{font-size:9px;font-weight:900;letter-spacing:.7px;color:var(--muted);min-width:72px}
  .corr-node .label{font-size:11px;font-weight:800;color:var(--text)}
  .corr-node .detail{font-size:10px;color:var(--muted);margin-left:auto;text-align:right}
  .corr-children{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-left:28px}
  .corr-note{font-size:10px;color:var(--muted);line-height:1.45;padding-top:5px}
  @media(max-width:760px){.corr-children{grid-template-columns:1fr;margin-left:0}.corr-node .detail{display:none}}
'''
pos=s.find('</style>')
if pos<0: raise RuntimeError('Style boundary not found')
s=s[:pos]+css+s[pos:]
html=r'''
          <div class="correlation-view" id="correlationView">
            <div class="correlation-toolbar">
              <div>
                <div class="rca-label">SYSTEM CORRELATION</div>
                <div class="corr-note">Read-only relationship view. Deterministic incident severity remains authoritative.</div>
              </div>
              <button class="incident-action" onclick="renderCorrelationView(lastRcaPayload)">↻ Refresh</button>
            </div>
            <div id="correlationGraph"><div class="empty">Select an incident.</div></div>
          </div>
'''
marker='          <div class="incident-timeline" id="incidentTimeline">'
if marker not in s: raise RuntimeError('Incident timeline marker not found')
s=s.replace(marker,html+marker,1)
s=s.replace('let incidentFilter = "ALL";','let incidentFilter = "ALL";\nlet lastRcaPayload = null;',1)
anchor='function renderIncidentTimeline(incident, ai, intelligence) {'
if anchor not in s: raise RuntimeError('Timeline renderer not found')
fn=r'''
function renderCorrelationView(payload) {
  const graph=document.getElementById("correlationGraph");
  if(!graph) return;
  if(!payload || !payload.incident){graph.innerHTML='<div class="empty">No correlation evidence available.</div>';return;}
  const incident=payload.incident||{};
  const ai=payload.ai_analysis||null;
  const intel=payload.operational_intelligence||null;
  const sev=String(incident.severity||"UNKNOWN").toUpperCase();
  const evidence=Array.isArray(incident.evidence)?incident.evidence:[];
  const metrics=Array.isArray(incident.affected_metrics)?incident.affected_metrics:[];
  const historical=intel && Array.isArray(intel.historical_matches)?intel.historical_matches:[];
  const children=[];
  metrics.slice(0,6).forEach(x=>children.push(`<div class="corr-node"><span class="kind">METRIC</span><span class="label">${x}</span><span class="detail">observed</span></div>`));
  evidence.slice(0,6).forEach(x=>children.push(`<div class="corr-node"><span class="kind">EVIDENCE</span><span class="label">${x}</span><span class="detail">incident evidence</span></div>`));
  if(ai) children.push(`<div class="corr-node"><span class="kind">AI RCA</span><span class="label">${ai.root_cause_category||ai.root_cause||"Advisory hypothesis"}</span><span class="detail">advisory only</span></div>`);
  if(intel) children.push(`<div class="corr-node"><span class="kind">INTEL</span><span class="label">${intel.overall_signal||"NO_SIGNIFICANT_INTELLIGENCE"}</span><span class="detail">score ${Number(intel.score||0).toFixed(2)}</span></div>`);
  if(historical.length) children.push(`<div class="corr-node"><span class="kind">HISTORY</span><span class="label">${historical.length} relevant match(es)</span><span class="detail">reference only</span></div>`);
  graph.innerHTML=`<div class="corr-node root"><span class="kind">INCIDENT</span><span class="label">${incident.title||incident.rule_id||incident.incident_id||"Incident"}</span><span class="detail">${sev} · authoritative</span></div><div class="corr-children">${children.length?children.join(""): '<div class="corr-note">No contributing signals were recorded.</div>'}</div>`;
}

'''
s=s.replace(anchor,fn+anchor,1)
needle='    const intel = payload.operational_intelligence;'
if needle not in s: raise RuntimeError('RCA intelligence parser not found')
s=s.replace(needle,needle+'\n    lastRcaPayload = payload;\n    renderCorrelationView(payload);',1)
HTML.write_text(s,encoding='utf-8')
print('Milestone 7.4 applied safely.')
