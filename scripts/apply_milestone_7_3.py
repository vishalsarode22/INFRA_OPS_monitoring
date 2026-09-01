from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
HTML=ROOT/'dashboard'/'static'/'index.html'
s=HTML.read_text(encoding='utf-8')
if 'id="incidentTimeline"' in s:
    print('Milestone 7.3 already applied.')
    raise SystemExit
if 'id="rcaDrawer"' not in s or 'async function loadIncidentRca' not in s:
    raise RuntimeError('Milestone 7.2 Incident RCA UI not found. Apply 7.2 first.')
css='''\n  .incident-timeline{margin-top:14px;border-top:1px solid var(--border);padding-top:14px}\n  .timeline-item{display:grid;grid-template-columns:86px 18px minmax(0,1fr);gap:10px;padding:9px 0}\n  .timeline-time{color:var(--muted);font-size:10px;white-space:nowrap}\n  .timeline-marker{position:relative;width:18px}\n  .timeline-marker:before{content:"";position:absolute;top:4px;left:5px;width:8px;height:8px;border-radius:50%;background:var(--muted)}\n  .timeline-marker:after{content:"";position:absolute;top:12px;bottom:-13px;left:8px;width:1px;background:var(--border)}\n  .timeline-item:last-child .timeline-marker:after{display:none}\n  .timeline-title{font-size:11px;font-weight:800;color:var(--text)}\n  .timeline-detail{color:var(--muted);font-size:11px;line-height:1.45;margin-top:2px}\n  @media(max-width:760px){.timeline-item{grid-template-columns:66px 18px minmax(0,1fr)}}\n'''
pos=s.find('</style>')
s=s[:pos]+css+s[pos:]
marker='          <div id="rcaContent"><div class="empty">Select an incident.</div></div>'
if marker not in s: raise RuntimeError('RCA content marker not found.')
timeline='''\n          <div class="incident-timeline" id="incidentTimeline">\n            <div class="rca-label">INCIDENT TIMELINE</div>\n            <div id="timelineList"><div class="empty">Select an incident.</div></div>\n          </div>'''
s=s.replace(marker,marker+timeline,1)
anchor='async function loadIncidentRca(incidentId) {'
js='''\nfunction renderIncidentTimeline(incident, ai, intelligence){\n  const list=document.getElementById("timelineList"); if(!list) return;\n  const items=[]; const first=incident.first_seen||incident.created_at; const last=incident.last_seen||incident.updated_at;\n  if(first) items.push({time:first,title:"Incident first detected",detail:"Deterministic monitoring recorded this occurrence."});\n  if((incident.affected_metrics||[]).length) items.push({time:last||first,title:"Affected metrics",detail:incident.affected_metrics.slice(0,6).join(", ")});\n  if((incident.evidence||[]).length) items.push({time:last||first,title:"Evidence updated",detail:incident.evidence.slice(0,4).join(" · ")});\n  if(ai) items.push({time:last||first,title:"AI RCA generated",detail:ai.root_cause_category||ai.root_cause||ai.likely_root_cause||"Advisory hypothesis available."});\n  if(intelligence) items.push({time:last||first,title:"Operational intelligence evaluated",detail:(intelligence.overall_signal||"NO_SIGNIFICANT_INTELLIGENCE")+" · score "+Number(intelligence.score||0).toFixed(2)});\n  if(String(incident.status||"").toUpperCase()==="RESOLVED"||incident.resolved===true) items.push({time:last||first,title:"Incident resolved",detail:"Resolution state is authoritative; AI hypotheses are not treated as confirmed cause."});\n  if(!items.length){list.innerHTML='<div class="empty">No timeline events recorded.</div>';return;}\n  list.innerHTML=items.map(x=>`<div class="timeline-item"><div class="timeline-time">${x.time?timeAgo(x.time):"--"}</div><div class="timeline-marker"></div><div><div class="timeline-title">${x.title}</div><div class="timeline-detail">${x.detail}</div></div></div>`).join("");\n}\n\n'''
s=s.replace(anchor,js+anchor,1)
needle='    const limitations = ai && Array.isArray(ai.limitations) ? ai.limitations : [];'
if needle not in s: raise RuntimeError('RCA parsing marker not found.')
s=s.replace(needle,needle+'\n    renderIncidentTimeline(incident, ai, intel);',1)
HTML.write_text(s,encoding='utf-8')
print('Milestone 7.3 applied safely.')
