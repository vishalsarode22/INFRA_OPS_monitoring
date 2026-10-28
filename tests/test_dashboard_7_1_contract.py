from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'dashboard' / 'static' / 'index.html'
def source(): return HTML.read_text(encoding='utf-8')
def test_dashboard_has_theme_toggle():
    # The theme is persisted under localStorage key "ibo-theme" -- the same
    # key every other page under dashboard/static reads, so a choice made on
    # one page holds on all of them. (Was "infrabeatops-theme" originally.)
    s=source(); assert 'data-theme-toggle' in s; assert '"ibo-theme"' in s
def test_dashboard_is_responsive():
    s=source(); assert '@media (max-width: 720px)' in s; assert '@media (max-width: 1000px)' in s
def test_dashboard_consumes_unified_overview():
    s=source(); assert '/api/systems/' in s and '/overview' in s; assert '/api/status/' not in s
def test_operational_intelligence_is_rendered():
    s=source(); assert 'id="operationalIntelligence"' in s; assert 'renderOperationalIntelligence' in s; assert 'operational_intelligence' in s
def test_dashboard_has_safe_intelligence_fallback():
    s=source(); assert 'Insufficient or unavailable intelligence data.' in s; assert 'overall_status:"UNKNOWN"' in s
