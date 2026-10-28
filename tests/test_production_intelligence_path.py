from datetime import datetime

from core.intelligence_runtime import IntelligenceRuntime, attach_operational_intelligence, reset_intelligence_runtime
from core.models import MetricResult, MonitoringResult, Status
from evaluation.ai_analyzer import analyze_incidents
from evaluation.providers.base import AIProvider
from core.incidents import Incident, IncidentStatus


class CountingProvider(AIProvider):
    name = "counting"

    def __init__(self):
        self.calls = 0
        self.prompts = []

    def generate(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        return (
            '{"severity":"WARNING","root_cause":{"category":"TEST",'
            '"description":"Synthetic"},"confidence":"HIGH",'
            '"confidence_score":0.9,"supporting_evidence":[],"'
            'contradicting_evidence":[],"recommended_actions":[],"limitations":[]}'
        )


def _result():
    now=datetime(2026,8,18,15,0)
    r=MonitoringResult(system="INT-7B",client="100",cycle_timestamp=now)
    r.metrics=[MetricResult("sap.sm12.lock_count",23,"23",Status.WARNING,source="TEST")]
    r.overall_status=Status.WARNING
    r.incidents=[Incident("INC-7B","INT-7B","100","SAP_LOCK_CONTENTION",
        "Lock contention","WARNING",IncidentStatus.ACTIVE,now,now,
        evidence=["SM12=23"],affected_metrics=["sap.sm12.lock_count"],confidence=.8)]
    return r


def test_runtime_attaches_structured_intelligence_without_changing_severity():
    r=_result()
    oi=attach_operational_intelligence(r, IntelligenceRuntime())
    assert r.operational_intelligence is oi
    assert oi.overall_signal in {"NO_SIGNIFICANT_INTELLIGENCE","LOW","MEDIUM","HIGH"}
    assert r.incidents[0].severity == "WARNING"


def test_runtime_state_is_isolated_by_system():
    from core.intelligence_runtime import get_intelligence_runtime
    reset_intelligence_runtime()
    a=get_intelligence_runtime("SYS-A")
    b=get_intelligence_runtime("SYS-B")
    assert a is get_intelligence_runtime("SYS-A")
    assert a is not b
    reset_intelligence_runtime()


def test_operational_intelligence_reaches_persistent_incident_rca():
    r=_result()
    oi=attach_operational_intelligence(r, IntelligenceRuntime())
    provider=CountingProvider()
    analyses=analyze_incidents(r,provider=provider,intelligence=oi)
    assert len(analyses)==1
    assert provider.calls==1
    assert "operational_intelligence" in provider.prompts[0]
    assert r.incidents[0].ai_analysis is not None


def test_unchanged_incident_does_not_reinvoke_provider_after_integration():
    r=_result(); oi=attach_operational_intelligence(r, IntelligenceRuntime()); p=CountingProvider()
    analyze_incidents(r,provider=p,intelligence=oi)
    analyze_incidents(r,provider=p,intelligence=oi)
    assert p.calls==1


def test_changed_incident_reinvokes_provider_after_integration():
    r=_result(); oi=attach_operational_intelligence(r, IntelligenceRuntime()); p=CountingProvider()
    analyze_incidents(r,provider=p,intelligence=oi)
    r.incidents[0].evidence.append("ST03N=900ms")
    r.incidents[0].ai_reanalysis_required=True
    analyze_incidents(r,provider=p,intelligence=oi)
    assert p.calls==2

def test_resolved_incident_is_not_sent_to_provider():
    r=_result(); r.incidents[0].status=IncidentStatus.RESOLVED
    oi=attach_operational_intelligence(r, IntelligenceRuntime()); p=CountingProvider()
    assert analyze_incidents(r,provider=p,intelligence=oi)==[]
    assert p.calls==0


def test_intelligence_cannot_override_critical_incident():
    r=_result(); r.incidents[0].severity="CRITICAL"
    oi=attach_operational_intelligence(r, IntelligenceRuntime()); p=CountingProvider()
    analyses=analyze_incidents(r,provider=p,intelligence=oi)
    assert analyses[0].severity=="CRITICAL"


def test_snapshot_contains_structured_intelligence(tmp_path, monkeypatch):
    import core.status_snapshot as ss
    monkeypatch.setattr(ss,"SNAPSHOT_DIR",str(tmp_path))
    r=_result(); attach_operational_intelligence(r,IntelligenceRuntime())
    ss.save_snapshot(r,system_name=r.system)
    data=ss.load_snapshot(r.system)
    assert data["operational_intelligence"] is not None
    assert "score" in data["operational_intelligence"]
