from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
HTML=ROOT/'dashboard'/'static'/'index.html'
def source(): return HTML.read_text(encoding='utf-8')
def test_correlation_view_exists():
    s=source(); assert 'id="correlationView"' in s; assert 'id="correlationGraph"' in s
def test_correlation_view_is_read_only():
    s=source(); assert 'read-only relationship view' in s.lower(); assert 'authoritative' in s
def test_correlation_view_uses_incident_evidence():
    s=source()
    for x in ('affected_metrics','evidence','ai_analysis','operational_intelligence'): assert x in s
def test_correlation_view_distinguishes_ai_and_history():
    s=source(); assert 'advisory only' in s; assert 'reference only' in s
