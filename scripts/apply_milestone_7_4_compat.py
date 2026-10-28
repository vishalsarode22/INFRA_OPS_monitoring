
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "dashboard" / "static" / "index.html"

if not HTML.exists():
    raise SystemExit(f"Dashboard HTML not found: {HTML}")

s = HTML.read_text(encoding="utf-8")

if 'id="systemCorrelationView"' in s:
    print("Milestone 7.4 correlation view already present.")
    raise SystemExit(0)

style = r'''
  .correlation-view { margin-top:14px; border-top:1px solid var(--border); padding-top:14px; }
  .correlation-map { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; align-items:stretch; }
  .corr-node { border:1px solid var(--border); border-radius:9px; padding:11px; background:var(--card); }
  .corr-node.root { border-width:2px; }
  .corr-label { color:var(--muted); font-size:9px; font-weight:900; letter-spacing:.8px; }
  .corr-title { margin-top:4px; font-size:12px; font-weight:850; }
  .corr-detail { margin-top:4px; color:var(--muted); font-size:10.5px; line-height:1.45; }
  .corr-note { margin-top:10px; color:var(--muted); font-size:10.5px; line-height:1.45; }
  @media (max-width:760px) {
    .correlation-map { grid-template-columns:1fr; }
    .corr-arrow { min-height:12px; transform:rotate(90deg); }
  }
'''
if "</style>" not in s:
    raise RuntimeError("Could not find </style> in dashboard HTML.")
s = s.replace("</style>", style + "\n</style>", 1)

container = r'''
          <div class="correlation-view" id="systemCorrelationView">
            <div class="rca-label">SYSTEM CORRELATION</div>
            <div id="correlationMap" class="correlation-map">
              <div class="corr-node root">
                <div class="corr-label">INCIDENT</div>
                <div class="corr-title" id="corrIncidentTitle">No incident selected</div>
                <div class="corr-detail" id="corrIncidentDetail">Authoritative monitoring state.</div>
              </div>
              <div class="corr-node">
                <div class="corr-label">SAP EVIDENCE</div>
                <div class="corr-title" id="corrEvidenceTitle">No evidence</div>
                <div class="corr-detail" id="corrEvidenceDetail">No correlated metrics recorded.</div>
              </div>
              <div class="corr-node">
                <div class="corr-label">INTELLIGENCE</div>
                <div class="corr-title" id="corrIntelTitle">Unavailable</div>
                <div class="corr-detail" id="corrIntelDetail">Intelligence is advisory and cannot change severity.</div>
              </div>
            </div>
            <div class="corr-note">
              Read-only correlation view. Deterministic incident severity remains authoritative;
              AI and historical intelligence are supporting evidence only.
            </div>
          </div>
'''
markers = [
    '          <div id="rcaContent"><div class="empty">Select an incident.</div></div>',
    '          <div class="incident-timeline" id="incidentTimeline">',
]
inserted = False
for marker in markers:
    if marker in s:
        if "incident-timeline" in marker:
            s = s.replace(marker, container + "\n" + marker, 1)
        else:
            s = s.replace(marker, marker + "\n" + container, 1)
        inserted = True
        break

if not inserted:
    raise RuntimeError(
        "Could not find the existing RCA/timeline section. "
        "Apply Milestone 7.2/7.3 to dashboard/static/index.html first."
    )

renderer = r'''
function renderSystemCorrelation(incident, ai, intelligence) {
  const title = document.getElementById("corrIncidentTitle");
  const detail = document.getElementById("corrIncidentDetail");
  const evidenceTitle = document.getElementById("corrEvidenceTitle");
  const evidenceDetail = document.getElementById("corrEvidenceDetail");
  const intelTitle = document.getElementById("corrIntelTitle");
  const intelDetail = document.getElementById("corrIntelDetail");
  if (!title) return;

  const severity = String(incident && incident.severity || "UNKNOWN").toUpperCase();
  const evidence = Array.isArray(incident && incident.evidence) ? incident.evidence : [];
  const metrics = Array.isArray(incident && incident.affected_metrics) ? incident.affected_metrics : [];

  title.textContent = incident && (incident.title || incident.rule_id) || "Incident";
  detail.textContent = `${severity} · ${incident && incident.status || "UNKNOWN"} · authoritative`;
  evidenceTitle.textContent = metrics.length ? `${metrics.length} affected metric(s)` : "SAP evidence";
  evidenceDetail.textContent =
    metrics.concat(evidence).slice(0, 5).join(" · ") ||
    "No correlated metrics recorded.";

  const signal = intelligence && (intelligence.overall_signal || intelligence.signal);
  const score = intelligence && Number(intelligence.score);
  intelTitle.textContent = signal || (ai ? "AI RCA available" : "No intelligence");
  intelDetail.textContent =
    Number.isFinite(score) ? `Score ${score.toFixed(2)} · advisory only` :
    "AI and historical intelligence are supporting evidence only.";
}
'''
anchor = "async function loadIncidentRca(incidentId)"
if anchor in s and "function renderSystemCorrelation(" not in s:
    s = s.replace(anchor, renderer + "\n" + anchor, 1)
elif "function renderSystemCorrelation(" not in s:
    s = s.replace("</script>", renderer + "\n</script>", 1)

call = "renderSystemCorrelation(incident, ai, intel);"
if call not in s:
    candidates = [
        '    const limitations = ai && Array.isArray(ai.limitations) ? ai.limitations : [];',
        '    const actions = ai && Array.isArray(ai.recommended_actions) ? ai.recommended_actions : [];',
        '    const evidence = Array.isArray(incident.evidence) ? incident.evidence : [];',
    ]
    for marker in candidates:
        if marker in s:
            s = s.replace(marker, marker + "\n    " + call, 1)
            break
    else:
        raise RuntimeError("Could not attach correlation renderer to the existing RCA loader.")

HTML.write_text(s, encoding="utf-8")
print("Milestone 7.4 compatibility fix applied safely.")
