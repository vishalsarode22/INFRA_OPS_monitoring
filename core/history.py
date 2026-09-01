"""Historical incident retrieval with relevance scoring."""
from __future__ import annotations
import re
from dataclasses import dataclass
from core.incident_store import load_incidents

@dataclass(frozen=True)
class HistoricalMatch:
    incident_id: str
    rule_id: str
    severity: str
    status: str
    first_seen: str
    resolved_at: str | None
    confidence: float
    similarity: float
    relevance_score: float
    root_cause_category: str
    root_cause: str
    recommended_actions: tuple[str, ...]
    evidence: tuple[str, ...]
    confirmed_root_cause: str = ""
    resolution_summary: str = ""
    resolution_actions: tuple[str, ...] = ()
    resolution_verified: bool = False

def _tokens(value: str) -> set[str]:
    words=re.findall(r"[a-z0-9_]{3,}",str(value).lower())
    stop={"the","and","for","with","from","status","warning","critical","normal","value","count","ms","sap"}
    return {w for w in words if w not in stop}

def _incident_tokens(incident)->set[str]:
    fields=[incident.rule_id,incident.title,incident.description,*incident.affected_metrics,*incident.evidence]
    return set().union(*(_tokens(x) for x in fields))

def _similarity(current,historical)->float:
    if current.rule_id != historical.rule_id: return 0.0
    a,b=_incident_tokens(current),_incident_tokens(historical)
    if not a or not b: return 1.0
    return len(a & b)/len(a | b)

def _relevance(similarity:float, confidence:float, verified:bool)->float:
    # Similarity is dominant; verified outcomes and historical correlation confidence
    # increase trust without allowing weak matches to dominate.
    score=(0.70*similarity)+(0.20*max(0.0,min(1.0,confidence)))+(0.10*(1.0 if verified else 0.0))
    return round(score,4)

def find_similar_incidents(system:str, incident, limit:int=3, min_similarity:float=0.25)->list[HistoricalMatch]:
    matches=[]
    for old in load_incidents(system):
        if old.incident_id==incident.incident_id or old.status.value!="RESOLVED": continue
        similarity=_similarity(incident,old)
        if similarity<min_similarity: continue
        ai=old.ai_analysis or {}
        resolution=old.resolution
        verified=bool(resolution and resolution.verified)
        matches.append(HistoricalMatch(
            incident_id=old.incident_id, rule_id=old.rule_id, severity=old.severity,
            status=old.status.value, first_seen=old.first_seen.isoformat(),
            resolved_at=old.resolved_at.isoformat() if old.resolved_at else None,
            confidence=float(old.confidence), similarity=round(similarity,4),
            relevance_score=_relevance(similarity,float(old.confidence),verified),
            root_cause_category=str(ai.get("root_cause_category","")),
            root_cause=str(ai.get("likely_root_cause","")),
            recommended_actions=tuple(str(x) for x in ai.get("recommended_actions",[])[:5]),
            evidence=tuple(str(x) for x in old.evidence[:5]),
            confirmed_root_cause=resolution.confirmed_root_cause if resolution else "",
            resolution_summary=resolution.summary if resolution else "",
            resolution_actions=tuple(resolution.actions_taken[:5]) if resolution else (),
            resolution_verified=verified,
        ))
    matches.sort(key=lambda m:(-m.relevance_score,-m.similarity,-m.confidence,m.first_seen))
    return matches[:max(0,limit)]
