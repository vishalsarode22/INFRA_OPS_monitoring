from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'dashboard/app.py'; SNAP=ROOT/'core/status_snapshot.py'
def patch(path, old, new, marker):
    t=path.read_text(encoding='utf-8')
    if marker in t: return
    if old not in t: raise RuntimeError(f'Safe insertion point not found: {path}')
    path.write_text(t.replace(old,new,1),encoding='utf-8')
patch(APP,'from core.status_snapshot import load_snapshot, list_snapshot_systems\n','from core.status_snapshot import load_snapshot, list_snapshot_systems\nfrom dashboard.intelligence_api import router as intelligence_router\n','from dashboard.intelligence_api import router as intelligence_router')
patch(APP,'app = FastAPI(title="InfraBeatOps Dashboard", version="1.0.0")\n','app = FastAPI(title="InfraBeatOps Dashboard", version="1.0.0")\napp.include_router(intelligence_router)\n','app.include_router(intelligence_router)')
patch(SNAP,'        "ai_analysis": None,\n','        "ai_analysis": None,\n        "operational_intelligence": None,\n','"operational_intelligence": None')
old='''    if gui_results:\n        data["gui_evidence"] = [\n'''
new='''    intelligence = getattr(result, "operational_intelligence", None)\n    if intelligence is not None:\n        data["operational_intelligence"] = {\n            "overall_signal": str(getattr(intelligence, "overall_signal", "UNKNOWN")),\n            "score": max(0.0, min(1.0, float(getattr(intelligence, "score", 0.0)))),\n            "signals": [\n                {"category": str(getattr(s, "category", "")), "metric": str(getattr(s, "metric", "")),\n                 "strength": float(getattr(s, "strength", 0.0)), "summary": str(getattr(s, "summary", ""))}\n                for s in (getattr(intelligence, "signals", ()) or ())[:20]\n            ],\n            "key_findings": list(getattr(intelligence, "key_findings", ()) or ())[:10],\n            "limitations": list(getattr(intelligence, "limitations", ()) or ())[:10],\n        }\n\n    if gui_results:\n        data["gui_evidence"] = [\n'''
patch(SNAP,old,new,'data["operational_intelligence"] = {')
print('Milestone 6.9 applied safely.')
