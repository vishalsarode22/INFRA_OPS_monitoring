from datetime import datetime
from core.history import find_similar_incidents
from core.incidents import Incident, IncidentStatus
from core.incident_store import save_incidents
from core.resolutions import ResolutionCategory, confirm_resolution

def inc(iid,confidence=0.8):
    now=datetime.now()
    return Incident(iid,"REL-PRD","000","SAP_LOCK_CONTENTION","Lock contention","WARNING",
                    IncidentStatus.RESOLVED,now,now,evidence=["SM12 lock count = 20","blocking batch process"],
                    affected_metrics=["sap.sm12.lock_count"],confidence=confidence,resolved_at=now,
                    ai_analysis={"root_cause_category":"LOCK_CONTENTION","likely_root_cause":"lock"})

def test_verified_history_gets_relevance_bonus(tmp_path,monkeypatch):
    import core.incident_store as store
    monkeypatch.setattr(store,"INCIDENT_DIR",str(tmp_path))
    old=inc("OLD")
    confirm_resolution(old,category=ResolutionCategory.APPLICATION_PROCESS,summary="Stopped process",
                       confirmed_root_cause="batch process held lock",verified=True)
    save_incidents("REL-PRD",[old])
    current=inc("NEW"); current.status=IncidentStatus.ACTIVE; current.resolved_at=None
    m=find_similar_incidents("REL-PRD",current)[0]
    assert m.resolution_verified is True
    assert m.relevance_score > m.similarity*0.70

def test_unverified_history_has_no_verified_bonus(tmp_path,monkeypatch):
    import core.incident_store as store
    monkeypatch.setattr(store,"INCIDENT_DIR",str(tmp_path))
    old=inc("OLD")
    save_incidents("REL-PRD",[old])
    current=inc("NEW"); current.status=IncidentStatus.ACTIVE; current.resolved_at=None
    m=find_similar_incidents("REL-PRD",current)[0]
    assert m.resolution_verified is False
    assert m.relevance_score == round(0.70*m.similarity+0.20*old.confidence,4)
