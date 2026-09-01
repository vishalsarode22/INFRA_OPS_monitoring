from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
HTML=ROOT/'dashboard'/'static'/'index.html'
def source(): return HTML.read_text(encoding='utf-8')
def test_incident_timeline_exists():
 s=source(); assert 'id="incidentTimeline"' in s; assert 'id="timelineList"' in s; assert 'INCIDENT TIMELINE' in s
def test_timeline_uses_incident_lifecycle_fields():
 s=source()
 for x in ('first_seen','last_seen','affected_metrics','evidence','status'): assert x in s
def test_timeline_separates_ai_from_authoritative_resolution():
 s=source(); assert 'AI RCA generated' in s; assert 'AI hypotheses are not treated as confirmed cause.' in s
def test_timeline_includes_operational_intelligence():
 s=source(); assert 'Operational intelligence evaluated' in s; assert 'overall_signal' in s
