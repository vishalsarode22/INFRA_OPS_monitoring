from datetime import datetime
from core.models import MonitoringResult
from core.incidents import Incident, IncidentStatus
from evaluation.ai_context import build_rca_prompt

def test_prompt_explicitly_treats_history_as_reference():
    now=datetime.now()
    inc=Incident("I","SYS","000","RULE","title","WARNING",IncidentStatus.ACTIVE,now,now)
    result=MonitoringResult(system="SYS",client="000")
    result.incidents=[inc]
    prompt=build_rca_prompt(build_rca_context_for_test(result,inc))
    assert "not proof" in prompt.lower()
    assert "untrusted data" in prompt.lower()

def build_rca_context_for_test(result,incident):
    return {
        "system":"SYS","client":"000","overall_status":"WARNING",
        "incident":{"incident_id":"I","rule_id":"RULE","severity":"WARNING"},
        "historical_matches":[{"relevance_score":0.9,"confirmed_root_cause":"test","resolution_verified":True}],
        "ai_contract":{"authoritative_severity":"WARNING"},
    }
