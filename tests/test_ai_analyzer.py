import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.orchestrator import run_monitoring_cycle

if __name__ == "__main__":
    result = run_monitoring_cycle(system="TST", client="000")

    print(f"\nOverall status: {result.overall_status.value}")
    if result.ai_analysis:
        ai = result.ai_analysis
        print(f"\nAI Severity: {ai.severity}")
        print(f"Root Cause: {ai.likely_root_cause}")
        print(f"Evidence: {ai.evidence}")
        print(f"Recommended Actions: {ai.recommended_actions}")
        print(f"Confidence: {ai.confidence}")